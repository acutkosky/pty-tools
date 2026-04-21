"""Async PTY server — one process per session, communicates via Unix domain socket."""

import argparse
import asyncio
import difflib
import errno
import fcntl
import json
import os
import pty as pty_mod
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

import pyte

from pty_tools.common import (
    atomic_reserve_session,
    is_server_alive,
    read_registry,
    send_request_async,
    socket_path_for,
    unregister_session,
)

# Exit code the server uses when it loses a duplicate-session race. The
# daemonizer translates this back to a "session already exists" error.
EXIT_SESSION_EXISTS = 75

# Regex to strip ANSI escape sequences from raw output
_ANSI_RE = re.compile(r"\x1b\[[\x20-\x3f]*[0-9;]*[\x20-\x7e]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[>=<]")

DEFAULT_BUFFER_LIMIT = 256 * 1024 * 1024  # 256 MiB

_BUFFER_LIMIT_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}


def parse_buffer_limit(value: str) -> int:
    """Parse a size string like '64M', '256MiB', '1G', or '4096' into bytes.

    Accepts an optional suffix K/M/G (case-insensitive, optional 'iB' tolerated).
    Units are binary (K=1024). Must be a positive integer number of bytes.
    """
    s = str(value).strip()
    if not s:
        raise ValueError("buffer limit must not be empty")
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] == "."):
        i += 1
    num_part, unit_part = s[:i], s[i:].strip().upper()
    if not num_part:
        raise ValueError(f"buffer limit missing number: {value!r}")
    if unit_part.endswith("IB"):
        unit_part = unit_part[:-2]
    elif unit_part.endswith("B") and unit_part not in ("B",):
        unit_part = unit_part[:-1]
    if unit_part not in _BUFFER_LIMIT_UNITS:
        raise ValueError(f"unknown buffer limit unit: {value!r}")
    try:
        n = float(num_part)
    except ValueError as e:
        raise ValueError(f"invalid buffer limit number: {value!r}") from e
    result = int(n * _BUFFER_LIMIT_UNITS[unit_part])
    if result <= 0:
        raise ValueError(f"buffer limit must be positive: {value!r}")
    return result


def _open_pty(rows: int, cols: int):
    """Create a PTY pair and set the terminal size. Returns (master_fd, slave_fd)."""
    master_fd, slave_fd = pty_mod.openpty()
    settings = termios.tcgetattr(master_fd)
    settings[3] = settings[3] & ~termios.ECHO
    termios.tcsetattr(master_fd, termios.TCSADRAIN, settings)
    # Non-blocking master fd: writes go through asyncio's add_writer when the
    # slave can't keep up, so a stalled child can't freeze the event loop.
    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    return master_fd, slave_fd


class PTYServer:
    """Manages a single PTY session with an async Unix socket interface."""

    def __init__(self, session_id: str, command: str, rows: int = 24, cols: int = 80,
                 foreground: bool = False, time_limit: float | None = None,
                 buffer_limit: int = DEFAULT_BUFFER_LIMIT):
        self.session_id = session_id
        self.command = command
        self.rows = rows
        self.cols = cols
        self.sock_path = str(socket_path_for(session_id))
        self.foreground = foreground
        self.time_limit = time_limit
        self.buffer_limit = buffer_limit

        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self.exited = False
        self.exit_code = None
        self.exit_signal = None
        self.read_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.read_buffer = bytearray()
        # Total bytes that were appended to read_buffer and then evicted by
        # the ring-buffer limit before any reader could consume them.
        self._dropped_bytes = 0
        # Monotonic count of bytes ever appended to read_buffer. Used by
        # readers to detect new output even when the ring buffer is at its
        # limit (where length can't grow further).
        self._total_appended = 0
        self._server: asyncio.Server | None = None
        self._new_data = asyncio.Event()
        self._stopped = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutting_down = False
        self._child_exit_task: asyncio.Task | None = None
        # One asyncio task per tap target, each fed by its own queue.
        self._tap_senders: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}

        # pyte virtual terminal for screen capture
        self._pyte_screen = pyte.Screen(cols, rows)
        self._pyte_stream = pyte.Stream(self._pyte_screen)

    async def start(self):
        self._loop = asyncio.get_running_loop()

        # Atomically reserve this session_id. If a live process already owns
        # it we exit with a dedicated code that the daemonizer decodes into a
        # "session already exists" error. Done before any resources are
        # acquired so a loser never leaks a PTY child or socket.
        if not atomic_reserve_session(
            self.session_id, self.command, os.getpid(), self.sock_path
        ):
            sys.exit(EXIT_SESSION_EXISTS)

        # Past the reservation: any failure must roll back registry + socket
        # file so subsequent spawns aren't blocked by our dead entry.
        try:
            # Bind next. The reservation cleared any stale socket file under
            # flock, so this should succeed; if EADDRINUSE still fires, another
            # non-cooperating process has the path.
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle_client, path=self.sock_path
                )
            except OSError as e:
                unregister_session(self.session_id)
                if e.errno == errno.EADDRINUSE:
                    sys.exit(EXIT_SESSION_EXISTS)
                raise

            # Only now fork the PTY child. If exec fails (bad command), we
            # fall into the outer except and the rollback unlinks the socket
            # file and registry entry we just created.
            master_fd, slave_fd = _open_pty(self.rows, self.cols)
            self._proc = await asyncio.create_subprocess_exec(
                *shlex.split(self.command),
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
            )
            os.close(slave_fd)
            self._master_fd = master_fd
        except BaseException:
            # SystemExit included — we want its code to propagate, but cleanup first.
            self._rollback_startup()
            raise

        def _request_shutdown():
            asyncio.create_task(self._shutdown())

        # Signal handlers only work on the main thread
        try:
            if self.foreground:
                # Foreground: forward SIGTERM/SIGHUP to child then shutdown
                for sig in (signal.SIGTERM, signal.SIGHUP):
                    self._loop.add_signal_handler(sig, self._on_forward_signal, sig)
                # SIGINT: just shutdown (don't forward — child gets it from the terminal)
                self._loop.add_signal_handler(signal.SIGINT, _request_shutdown)
                # SIGWINCH: propagate terminal size changes to child
                self._loop.add_signal_handler(signal.SIGWINCH, self._on_sigwinch)
            else:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    self._loop.add_signal_handler(sig, _request_shutdown)
        except RuntimeError:
            pass

        if self.time_limit is not None:
            self._loop.call_later(self.time_limit, self._on_time_limit)

        # Drive PTY reads off the event loop — no background thread needed.
        self._loop.add_reader(master_fd, self._on_readable)

        # Reap the child when it exits, record status, and wake readers.
        self._child_exit_task = asyncio.create_task(self._on_child_exit())

        # Foreground stdin is inherently blocking, so it stays in a thread
        # and dispatches writes into the loop.
        if self.foreground:
            threading.Thread(target=self._stdin_loop, daemon=True).start()

    async def serve(self):
        """Run start() and block until shutdown completes."""
        await self.start()
        await self._stopped.wait()

    def _on_readable(self):
        """Called by asyncio when the PTY master fd has data to read."""
        fd = self._master_fd
        if fd is None:
            return
        try:
            raw = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            # EIO typically means the slave side closed (child exited).
            raw = b""
        if not raw:
            try:
                self._loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            return
        self._on_data(raw)

    def _drain_pty_final(self):
        """Synchronously pull any remaining bytes from the PTY master.

        Used by exit --drain after killing the child: the reader callback
        may not have run yet for the final burst, so we do the reads
        directly. The fd is non-blocking, so EAGAIN/EIO both cleanly end.
        """
        fd = self._master_fd
        if fd is None:
            return
        while True:
            try:
                raw = os.read(fd, 4096)
            except (BlockingIOError, OSError):
                return
            if not raw:
                return
            self._on_data(raw)

    async def _on_child_exit(self):
        """Wait for the child process to exit, then record status and wake readers."""
        rc = await self._proc.wait()
        if rc >= 0:
            self.exit_code = rc
        else:
            self.exit_signal = -rc
        self.exited = True
        self._new_data.set()
        if self.foreground:
            await self._shutdown()

    def _stdin_loop(self):
        """Background thread: reads stdin line-by-line, forwards to PTY.

        Dispatches writes through the event loop so they share write_lock with
        socket-originated writes — otherwise a large stdin line could
        interleave with a concurrent write.
        """
        try:
            for line in sys.stdin:
                if self.exited or self._loop is None:
                    break
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        self._do_write({"text": line}), self._loop
                    )
                    # No timeout: writes block on write_lock / PTY backpressure
                    # the same way a real terminal does. A 30s cap would
                    # silently drop stdin lines when the child was slow to
                    # drain, with no way for the user to notice.
                    fut.result()
                except BaseException:
                    # Loop gone, cancelled, or write failed — nothing to do
                    # beyond exiting the stdin-forwarding thread.
                    break
        except (EOFError, OSError):
            pass

    def _on_data(self, raw: bytes):
        """Called on the event loop when PTY produces output."""
        self.read_buffer.extend(raw)
        self._total_appended += len(raw)
        overflow = len(self.read_buffer) - self.buffer_limit
        if overflow > 0:
            del self.read_buffer[:overflow]
            self._dropped_bytes += overflow
        self._pyte_stream.feed(raw.decode("utf-8", errors="replace"))
        if self.foreground:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
        if self._tap_senders:
            text = raw.decode("utf-8", errors="replace")
            for q, _ in self._tap_senders.values():
                q.put_nowait(text)
        self._new_data.set()

    async def _tap_sender(self, target_id: str, q: asyncio.Queue):
        """Drain this target's queue, forwarding each chunk over a socket.

        One task per target means a slow target can't block delivery to others;
        per-target FIFO is preserved because each queue has a single consumer.
        """
        try:
            while True:
                text = await q.get()
                try:
                    await send_request_async(
                        target_id,
                        {"type": "write", "text": text},
                        timeout=5.0,
                    )
                except Exception:
                    # Target unreachable — drop the tap.
                    self._tap_senders.pop(target_id, None)
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a single client connection."""
        shutdown_after = False
        try:
            data = await asyncio.wait_for(reader.read(), timeout=5.0)
            msg = json.loads(data.decode())
            response, shutdown_after = await self._dispatch(msg)
            writer.write(json.dumps(response).encode())
            await writer.drain()
        except Exception as e:
            try:
                writer.write(json.dumps({"status": "error", "error": str(e)}).encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if shutdown_after:
                await self._shutdown()

    async def _dispatch(self, msg: dict):
        """Route msg to its handler. Returns (response, shutdown_after)."""
        mtype = msg.get("type")
        if mtype == "exit":
            if msg.get("drain"):
                return await self._do_exit_drain(msg), True
            return {"status": "ok", "message": "Shutting down"}, True
        handlers = {
            "write": self._do_write,
            "read": self._do_read,
            "interact": self._do_interact,
            "tap": self._do_tap,
            "untap": self._do_untap,
            "screen": self._do_screen,
            "resize": self._do_resize,
            "signal": self._do_signal,
        }
        handler = handlers.get(mtype)
        if handler is None:
            return {"status": "error", "error": f"Unknown message type: {mtype}"}, False
        response = await handler(msg)
        # After a consuming read drains the buffer of an exited session, no one
        # can reconnect usefully — shut down and clean up.
        shutdown = (
            mtype in ("read", "interact")
            and self.exited
            and not self.read_buffer
            and not msg.get("peek", False)
        )
        return response, shutdown

    async def _do_tap(self, msg: dict) -> dict:
        target_id = msg.get("target")
        if not target_id:
            return {"status": "error", "error": "Missing 'target' session ID"}
        if target_id == self.session_id:
            # Self-tap feeds every output byte back as input, causing
            # unbounded amplification through the PTY's echo loop.
            return {"status": "error", "error": "Cannot tap a session to itself"}
        if not is_server_alive(target_id):
            return {"status": "error", "error": f"Target session '{target_id}' is not running"}
        if target_id not in self._tap_senders:
            q: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(self._tap_sender(target_id, q))
            self._tap_senders[target_id] = (q, task)
        return {"status": "ok", "message": f"Tapping output to '{target_id}'"}

    async def _do_untap(self, msg: dict) -> dict:
        target_id = msg.get("target")
        if not target_id:
            return {"status": "error", "error": "Missing 'target' session ID"}
        entry = self._tap_senders.pop(target_id, None)
        if entry is not None:
            _, task = entry
            task.cancel()
        return {"status": "ok", "message": f"Removed tap to '{target_id}'"}

    async def _write_bytes(self, data: bytes):
        """Write all bytes to the master fd, yielding on EAGAIN.

        Caller must hold self.write_lock so concurrent writers can't interleave
        or race across a partial-write boundary.
        """
        remaining = memoryview(data)
        while remaining:
            fd = self._master_fd
            if fd is None:
                raise OSError("PTY is closed")
            try:
                n = os.write(fd, remaining)
            except BlockingIOError:
                fut = self._loop.create_future()

                def _wake():
                    if not fut.done():
                        fut.set_result(None)

                self._loop.add_writer(fd, _wake)
                try:
                    await fut
                finally:
                    try:
                        self._loop.remove_writer(fd)
                    except (ValueError, OSError):
                        pass
                continue
            if n <= 0:
                raise OSError("short write returned 0")
            remaining = remaining[n:]

    async def _write_under_lock(self, text) -> str | None:
        """Caller must hold write_lock. Returns None on success or error string."""
        data = text.encode("utf-8") if isinstance(text, str) else text
        try:
            await self._write_bytes(data)
            return None
        except OSError as e:
            return str(e)

    async def _do_write(self, msg: dict) -> dict:
        if self.exited:
            return {"status": "error", "error": "Session has exited"}
        async with self.write_lock:
            err = await self._write_under_lock(msg.get("text", ""))
        return {"status": "error", "error": err} if err else {"status": "ok"}

    async def _do_interact(self, msg: dict) -> dict:
        if self.exited:
            return {"status": "error", "error": "Session has exited"}
        # Acquire read_lock FIRST so no concurrent read can steal our response.
        # Write_lock nested inside prevents concurrent writes from interleaving
        # output with ours. Lock order (read then write) is consistent across
        # every call site that takes both, so no deadlock is possible.
        screen_mode = msg.get("screen", False)
        diff_mode = msg.get("diff", False)
        async with self.read_lock:
            async with self.write_lock:
                # Snapshot the pre-write display before any bytes go out, so
                # the diff boundary is exactly "what my write caused."
                pre_display = None
                if diff_mode:
                    pre_display = [line.rstrip() for line in self._pyte_screen.display]
                err = await self._write_under_lock(msg.get("text", ""))
                if err:
                    return {"status": "error", "error": err}
                if screen_mode:
                    await self._wait_for_output(msg)
                    return self._build_screen_response(pre_display=pre_display)
                return await self._read_body(msg)

    async def _do_read(self, msg: dict) -> dict:
        async with self.read_lock:
            return await self._read_body(msg)

    async def _wait_for_output(self, msg: dict):
        """Wait for output to appear and stabilize, or timeout.

        Caller must hold self.read_lock. Returns the match_end position if
        --pattern was provided and matched, else None.
        """
        total_timeout = msg.get("total_timeout", 5000) / 1000.0
        stable_timeout = msg.get("stable_timeout", 500) / 1000.0
        pattern = msg.get("pattern")

        start = time.monotonic()
        got_output = False
        match_end = None
        # Track the monotonic "bytes appended" counter rather than buffer
        # length — length can't grow once the ring buffer is at its limit, so
        # a length-based check misses bursts that arrive during overflow.
        prev_appended = self._total_appended

        while True:
            # Check pattern match against entire unconsumed buffer
            if pattern:
                text = bytes(self.read_buffer).decode("utf-8", errors="replace")
                m = re.search(pattern, text)
                if m:
                    match_end = m.end()
                    break

            if self.exited or self._shutting_down:
                break

            elapsed = time.monotonic() - start
            remaining = total_timeout - elapsed
            if remaining <= 0:
                break

            wait_time = min(stable_timeout, remaining) if got_output else remaining

            # Wait for the background reader to deliver new data
            self._new_data.clear()
            try:
                await asyncio.wait_for(self._new_data.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                pass

            if self._total_appended > prev_appended:
                got_output = True
                prev_appended = self._total_appended
            elif got_output:
                break

        return match_end

    async def _read_body(self, msg: dict) -> dict:
        """Read buffer/pattern/timeout loop. Caller must hold self.read_lock."""
        strip_ansi = msg.get("strip_ansi", True)
        peek = msg.get("peek", False)
        match_end = await self._wait_for_output(msg)

        result = {"status": "ok", "exited": self.exited,
                  "truncated": self._dropped_bytes}
        if self.exited:
            result["exit_code"] = self.exit_code
            result["signal"] = self.exit_signal

        # Consume buffer (pattern match consumes up to match; otherwise all).
        # peek=True always leaves the buffer untouched.
        if match_end is not None:
            show = bytes(self.read_buffer[:match_end])
            if not peek:
                self.read_buffer = bytearray(self.read_buffer[match_end:])
        else:
            show = bytes(self.read_buffer)
            if not peek:
                self.read_buffer.clear()

        text = show.decode("utf-8", errors="replace")
        if strip_ansi:
            text = _ANSI_RE.sub("", text)
        result["response"] = text

        return result

    async def _do_exit_drain(self, msg: dict) -> dict:
        """Kill child, flush remaining PTY output, return it with exit info.

        Used when the client wants a guarantee that no trailing output is
        lost on shutdown. After this returns, _handle_client will send the
        response and then call _shutdown normally. We take ownership of the
        child reap here (cancelling _on_child_exit) so it can't race with
        the response write by triggering foreground-mode _shutdown mid-send.
        """
        t = self._child_exit_task
        # Null the handle first so _shutdown won't re-await it after we
        # cancel — its await of a cancelled task would raise in cleanup.
        self._child_exit_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except BaseException:
                pass

        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        if self._proc is not None and self._proc.returncode is not None and not self.exited:
            rc = self._proc.returncode
            if rc >= 0:
                self.exit_code = rc
            else:
                self.exit_signal = -rc
            self.exited = True

        # Pull any final bytes that the reader callback hadn't consumed.
        self._drain_pty_final()

        async with self.read_lock:
            strip_ansi = msg.get("strip_ansi", True)
            peek = msg.get("peek", False)
            show = bytes(self.read_buffer)
            if not peek:
                self.read_buffer.clear()
            text = show.decode("utf-8", errors="replace")
            if strip_ansi:
                text = _ANSI_RE.sub("", text)
            result = {
                "status": "ok",
                "exited": self.exited,
                "truncated": self._dropped_bytes,
                "response": text,
            }
            if self.exited:
                result["exit_code"] = self.exit_code
                result["signal"] = self.exit_signal
            return result

    def _on_forward_signal(self, sig):
        """Forward a signal to the child process group, then shut down."""
        asyncio.create_task(self._forward_and_shutdown(sig))

    async def _forward_and_shutdown(self, sig):
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            # Give the child a moment to exit from the forwarded signal before
            # _shutdown's SIGKILL fallback — otherwise the forward is pointless.
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        await self._shutdown()

    def _on_sigwinch(self):
        """Handle SIGWINCH in foreground mode — propagate terminal size to child."""
        size = shutil.get_terminal_size()
        asyncio.create_task(self._do_resize({"rows": size.lines, "cols": size.columns}))

    async def _do_screen(self, msg: dict) -> dict:
        """Return the current virtual terminal screen contents."""
        return self._build_screen_response()

    def _build_screen_response(self, pre_display: list[str] | None = None) -> dict:
        """Build a screen-snapshot or screen-diff response.

        If pre_display is provided, returns a unified-diff response against the
        current display; otherwise returns the full current display.
        """
        lines = [line.rstrip() for line in self._pyte_screen.display]
        result = {
            "status": "ok",
            "exited": self.exited,
            "truncated": self._dropped_bytes,
            "rows": self._pyte_screen.lines,
            "cols": self._pyte_screen.columns,
            "cursor": [self._pyte_screen.cursor.y, self._pyte_screen.cursor.x],
        }
        if self.exited:
            result["exit_code"] = self.exit_code
            result["signal"] = self.exit_signal
        if pre_display is not None:
            result["type"] = "screen_diff"
            result["diff"] = "\n".join(
                difflib.unified_diff(pre_display, lines, lineterm="")
            )
        else:
            result["response"] = "\n".join(lines)
        return result

    async def _do_resize(self, msg: dict) -> dict:
        """Resize the PTY and update the virtual terminal."""
        rows = msg.get("rows")
        cols = msg.get("cols")
        if rows is None or cols is None:
            return {"status": "error", "error": "Missing 'rows' or 'cols'"}
        self.rows = rows
        self.cols = cols
        if self._master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGWINCH)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        self._pyte_screen.resize(rows, cols)
        return {"status": "ok", "rows": rows, "cols": cols}

    async def _do_signal(self, msg: dict) -> dict:
        """Send a signal to the child process group."""
        sig_value = msg.get("signal")
        if sig_value is None:
            return {"status": "error", "error": "Missing 'signal'"}
        try:
            if isinstance(sig_value, int):
                sig = signal.Signals(sig_value)
            else:
                name = sig_value.upper()
                sig = signal.Signals[name if name.startswith("SIG") else "SIG" + name]
        except (KeyError, ValueError):
            return {"status": "error", "error": f"Unknown signal: {sig_value}"}
        if self._proc is None or self._proc.returncode is not None:
            return {"status": "error", "error": "Session has exited"}
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError) as e:
            return {"status": "error", "error": str(e)}
        return {"status": "ok", "signal": sig.name}

    def _on_time_limit(self):
        """Called when the time limit expires — kill the child and shut down."""
        if not self.exited:
            asyncio.create_task(self._shutdown())

    def _rollback_startup(self):
        """Undo reservation on failed startup so subsequent spawns aren't blocked."""
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        sock = socket_path_for(self.session_id)
        if sock.exists():
            try:
                sock.unlink()
            except OSError:
                pass
        try:
            unregister_session(self.session_id)
        except Exception:
            pass

    async def _shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True

        # Wake any readers blocked on _new_data.wait() so they can observe
        # the _shutting_down flag and return rather than sitting on their
        # total_timeout while the server tears down around them.
        self._new_data.set()

        # Cancel tap senders so they don't block on pending sends.
        for _, task in self._tap_senders.values():
            task.cancel()
        self._tap_senders.clear()

        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

        # Wait for _on_child_exit to record exit_code/exit_signal before we
        # tear the loop down — otherwise run_server can't propagate them.
        # (_on_child_exit itself calls _shutdown in foreground mode; skip the
        # await when we're already running inside it to avoid self-deadlock.
        # Skip too if the task has been cleared/cancelled by _do_exit_drain.)
        t = self._child_exit_task
        if t is not None and t is not asyncio.current_task() and not t.done():
            try:
                await t
            except BaseException:
                pass

        if self._master_fd is not None:
            fd = self._master_fd
            self._master_fd = None
            try:
                self._loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            try:
                os.close(fd)
            except OSError:
                pass

        if self._server:
            self._server.close()

        sock = socket_path_for(self.session_id)
        if sock.exists():
            sock.unlink()

        unregister_session(self.session_id)
        self._stopped.set()


def run_server(session_id: str, command: str, rows: int = 24, cols: int = 80,
               foreground: bool = False, time_limit: float | None = None,
               buffer_limit: int = DEFAULT_BUFFER_LIMIT) -> int:
    """Entry point for the server process. Returns the child's exit code (0 if unknown)."""
    server = PTYServer(session_id, command, rows=rows, cols=cols, foreground=foreground,
                       time_limit=time_limit, buffer_limit=buffer_limit)
    try:
        asyncio.run(server.serve())
    except Exception:
        pass
    if server.exit_code is not None:
        return server.exit_code
    if server.exit_signal is not None:
        return 128 + server.exit_signal
    return 0


def daemonize_server(session_id: str, command: str, rows: int = 24, cols: int = 80,
                     time_limit: float | None = None,
                     buffer_limit: int = DEFAULT_BUFFER_LIMIT) -> dict:
    """Launch a detached subprocess to run the PTY server. Returns status dict."""
    from pty_tools.common import ensure_socket_dir

    ensure_socket_dir()
    sock_path = socket_path_for(session_id)

    # Stderr goes to a temp file so we can capture startup errors without
    # leaving a pipe open (a broken pipe would SIGPIPE the long-lived server).
    err_fd, err_path = tempfile.mkstemp(prefix="pty_err_")
    err_file = os.fdopen(err_fd, "w")

    cmd_args = [sys.executable, "-m", "pty_tools.server", session_id, command,
                "--rows", str(rows), "--cols", str(cols),
                "--buffer-limit", str(buffer_limit)]
    if time_limit is not None:
        cmd_args.extend(["--time-limit", str(time_limit)])

    proc = subprocess.Popen(
        cmd_args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=err_file,
        start_new_session=True,
    )
    err_file.close()

    def _read_and_cleanup_err():
        try:
            with open(err_path) as f:
                return f.read().strip()
        except Exception:
            return ""
        finally:
            try:
                os.unlink(err_path)
            except Exception:
                pass

    for _ in range(20):
        rc = proc.poll()
        if rc is not None:
            # Check exit code FIRST: in a race, the winner's socket may already
            # exist at sock_path, so polling sock_path.exists() would misreport
            # the loser as successful.
            if rc == EXIT_SESSION_EXISTS:
                _read_and_cleanup_err()
                return {
                    "status": "error",
                    "error": f"Session '{session_id}' already exists",
                }
            stderr = _read_and_cleanup_err()
            msg = f"Server for session '{session_id}' did not start"
            if stderr:
                msg += f": {stderr}"
            return {"status": "error", "error": msg}

        # Server still running — consider the spawn successful only when the
        # registry shows OUR subprocess PID as the owner (proves we won the
        # race and finished the atomic reservation).
        entry = read_registry().get(session_id)
        if entry is not None and entry.get("pid") == proc.pid:
            _read_and_cleanup_err()
            return {
                "status": "ok",
                "session_id": session_id,
                "command": command,
                "pid": proc.pid,
                "socket_path": str(sock_path),
            }
        time.sleep(0.1)

    # Timeout — kill the stalled process
    try:
        proc.kill()
    except Exception:
        pass
    stderr = _read_and_cleanup_err()
    msg = f"Server for session '{session_id}' did not start"
    if stderr:
        msg += f": {stderr}"
    return {"status": "error", "error": msg}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("command")
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--time-limit", type=float, default=None)
    parser.add_argument("--buffer-limit", type=parse_buffer_limit, default=DEFAULT_BUFFER_LIMIT)
    args = parser.parse_args()

    try:
        rc = run_server(args.session_id, args.command, rows=args.rows, cols=args.cols,
                        foreground=args.foreground, time_limit=args.time_limit,
                        buffer_limit=args.buffer_limit)
        sys.exit(rc)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
