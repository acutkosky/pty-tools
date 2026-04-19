"""Async PTY server — one process per session, communicates via Unix domain socket."""

import argparse
import asyncio
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
    is_server_alive,
    register_session,
    send_request_async,
    socket_path_for,
    unregister_session,
)

# Regex to strip ANSI escape sequences from raw output
_ANSI_RE = re.compile(r"\x1b\[[\x20-\x3f]*[0-9;]*[\x20-\x7e]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[>=<]")


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
                 foreground: bool = False, time_limit: float | None = None):
        self.session_id = session_id
        self.command = command
        self.rows = rows
        self.cols = cols
        self.sock_path = str(socket_path_for(session_id))
        self.foreground = foreground
        self.time_limit = time_limit

        self._master_fd: int | None = None
        self._proc: subprocess.Popen | None = None
        self.exited = False
        self.exit_code = None
        self.exit_signal = None
        self.read_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.read_buffer = bytearray()
        self._server: asyncio.Server | None = None
        self._new_data = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutting_down = False
        # One asyncio task per tap target, each fed by its own queue.
        self._tap_senders: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}

        # pyte virtual terminal for screen capture
        self._pyte_screen = pyte.Screen(cols, rows)
        self._pyte_stream = pyte.Stream(self._pyte_screen)

    async def start(self):
        self._loop = asyncio.get_running_loop()

        master_fd, slave_fd = _open_pty(self.rows, self.cols)
        self._proc = subprocess.Popen(
            shlex.split(self.command),
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, start_new_session=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd

        register_session(self.session_id, self.command, os.getpid(), self.sock_path)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.sock_path
        )
        # Signal handlers only work on the main thread
        try:
            if self.foreground:
                # Foreground: forward SIGTERM/SIGHUP to child then shutdown
                for sig in (signal.SIGTERM, signal.SIGHUP):
                    self._loop.add_signal_handler(sig, self._on_forward_signal, sig)
                # SIGINT: just shutdown (don't forward — child gets it from the terminal)
                self._loop.add_signal_handler(
                    signal.SIGINT, lambda: asyncio.create_task(self._shutdown()))
                # SIGWINCH: propagate terminal size changes to child
                self._loop.add_signal_handler(signal.SIGWINCH, self._on_sigwinch)
            else:
                # Background: SIGTERM/SIGINT → shutdown
                for sig in (signal.SIGTERM, signal.SIGINT):
                    self._loop.add_signal_handler(
                        sig, lambda: asyncio.create_task(self._shutdown()))
        except RuntimeError:
            pass

        # Schedule time limit if set
        if self.time_limit is not None:
            self._loop.call_later(self.time_limit, self._on_time_limit)

        # Drive PTY reads off the event loop — no background thread needed.
        self._loop.add_reader(master_fd, self._on_readable)

        # Start stdin forwarder if foreground (stdin is inherently blocking,
        # so this stays in a thread and dispatches into the loop).
        if self.foreground:
            threading.Thread(target=self._stdin_loop, daemon=True).start()

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
            # EIO typically means the slave side closed (child exited)
            raw = b""
        if not raw:
            # Stop reading and collect exit status asynchronously.
            try:
                self._loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            asyncio.create_task(self._on_child_exit())
            return
        self._on_data(raw)

    async def _on_child_exit(self):
        """Reap the child and record its exit status, then wake readers."""
        def _wait():
            try:
                return self._proc.wait(timeout=5.0)
            except Exception:
                return None

        rc = await self._loop.run_in_executor(None, _wait)
        if rc is not None:
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

        Writes go through the asyncio loop so they share write_lock with
        socket-originated writes — otherwise a large stdin line could
        interleave with a concurrent write.
        """
        try:
            for line in sys.stdin:
                if self.exited or self._loop is None:
                    break
                data = line.encode("utf-8")
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        self._locked_write(data), self._loop
                    )
                    fut.result(timeout=30.0)
                except Exception:
                    break
        except (EOFError, OSError):
            pass

    async def _locked_write(self, data: bytes):
        if self.exited:
            return
        async with self.write_lock:
            try:
                await self._write_bytes(data)
            except OSError:
                pass

    def _on_data(self, raw: bytes):
        """Called on the event loop when PTY produces output."""
        self.read_buffer.extend(raw)
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
            msg_type = msg.get("type")

            if msg_type == "write":
                response = await self._do_write(msg)
            elif msg_type == "read":
                response = await self._do_read(msg)
                if self.exited and not self.read_buffer and not msg.get("peek", False):
                    shutdown_after = True
            elif msg_type == "interact":
                response = await self._do_interact(msg)
                if self.exited and not self.read_buffer and not msg.get("peek", False):
                    shutdown_after = True
            elif msg_type == "tap":
                response = self._do_tap(msg)
            elif msg_type == "untap":
                response = self._do_untap(msg)
            elif msg_type == "screen":
                response = self._do_screen()
            elif msg_type == "resize":
                response = self._do_resize(msg)
            elif msg_type == "signal":
                response = self._do_signal(msg)
            elif msg_type == "exit":
                response = {"status": "ok", "message": "Shutting down"}
                shutdown_after = True
            else:
                response = {"status": "error", "error": f"Unknown message type: {msg_type}"}

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

    def _do_tap(self, msg: dict) -> dict:
        target_id = msg.get("target")
        if not target_id:
            return {"status": "error", "error": "Missing 'target' session ID"}
        if not is_server_alive(target_id):
            return {"status": "error", "error": f"Target session '{target_id}' is not running"}
        if target_id not in self._tap_senders:
            q: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(self._tap_sender(target_id, q))
            self._tap_senders[target_id] = (q, task)
        return {"status": "ok", "message": f"Tapping output to '{target_id}'"}

    def _do_untap(self, msg: dict) -> dict:
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

    async def _do_write(self, msg: dict) -> dict:
        if self.exited:
            return {"status": "error", "error": "Session has exited"}
        text = msg.get("text", "")
        data = text.encode("utf-8") if isinstance(text, str) else text
        async with self.write_lock:
            try:
                await self._write_bytes(data)
            except OSError as e:
                return {"status": "error", "error": str(e)}
        return {"status": "ok"}

    async def _do_interact(self, msg: dict) -> dict:
        if self.exited:
            return {"status": "error", "error": "Session has exited"}
        text = msg.get("text", "")
        data = text.encode("utf-8") if isinstance(text, str) else text
        async with self.write_lock:
            try:
                await self._write_bytes(data)
            except OSError as e:
                return {"status": "error", "error": str(e)}
            return await self._do_read(msg)

    async def _do_read(self, msg: dict) -> dict:
        total_timeout = msg.get("total_timeout", 5000) / 1000.0
        stable_timeout = msg.get("stable_timeout", 500) / 1000.0
        pattern = msg.get("pattern")
        strip_ansi = msg.get("strip_ansi", True)
        peek = msg.get("peek", False)

        async with self.read_lock:
            start = time.monotonic()
            got_output = False
            match_end = None
            prev_buf_len = len(self.read_buffer)

            while True:
                # Check pattern match against entire unconsumed buffer
                if pattern:
                    text = bytes(self.read_buffer).decode("utf-8", errors="replace")
                    m = re.search(pattern, text)
                    if m:
                        match_end = m.end()
                        break

                if self.exited:
                    break

                elapsed = time.monotonic() - start
                remaining = total_timeout - elapsed
                if remaining <= 0:
                    break

                if got_output:
                    wait_time = min(stable_timeout, remaining)
                else:
                    wait_time = remaining

                # Wait for the background reader to deliver new data
                self._new_data.clear()
                try:
                    await asyncio.wait_for(self._new_data.wait(), timeout=wait_time)
                except asyncio.TimeoutError:
                    pass

                # If buffer grew, we got output — loop to re-check pattern/exit.
                # If it didn't grow and we already had output, that's stable silence.
                new_buf_len = len(self.read_buffer)
                if new_buf_len > prev_buf_len:
                    got_output = True
                    prev_buf_len = new_buf_len
                elif got_output:
                    break

            # Build result
            result = {
                "status": "ok",
                "exited": self.exited,
            }
            if self.exited:
                result["exit_code"] = self.exit_code
                result["signal"] = self.exit_signal

            # Consume buffer (pattern match consumes up to match; otherwise all)
            if match_end is not None:
                show = bytes(self.read_buffer[:match_end])
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

    def _on_forward_signal(self, sig):
        """Forward a signal to the child process group, then shut down."""
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        asyncio.create_task(self._shutdown())

    def _on_sigwinch(self):
        """Handle SIGWINCH in foreground mode — propagate terminal size to child."""
        size = shutil.get_terminal_size()
        self._do_resize({"rows": size.lines, "cols": size.columns})

    def _do_screen(self) -> dict:
        """Return the current virtual terminal screen contents."""
        lines = [line.rstrip() for line in self._pyte_screen.display]
        return {
            "status": "ok",
            "response": "\n".join(lines),
            "rows": self._pyte_screen.lines,
            "cols": self._pyte_screen.columns,
        }

    def _do_resize(self, msg: dict) -> dict:
        """Resize the PTY and update the virtual terminal."""
        rows = msg.get("rows")
        cols = msg.get("cols")
        if rows is None or cols is None:
            return {"status": "error", "error": "Missing 'rows' or 'cols'"}
        self.rows = rows
        self.cols = cols
        # Update the actual PTY size
        if self._master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        # Signal the child process group
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGWINCH)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        # Resize the virtual terminal
        self._pyte_screen.resize(rows, cols)
        return {"status": "ok", "rows": rows, "cols": cols}

    def _do_signal(self, msg: dict) -> dict:
        """Send a signal to the child process group."""
        sig_value = msg.get("signal")
        if sig_value is None:
            return {"status": "error", "error": "Missing 'signal'"}
        # Resolve signal name or number
        if isinstance(sig_value, int):
            try:
                sig = signal.Signals(sig_value)
            except ValueError:
                return {"status": "error", "error": f"Unknown signal number: {sig_value}"}
        else:
            sig_name = sig_value.upper()
            if not sig_name.startswith("SIG"):
                sig_name = "SIG" + sig_name
            try:
                sig = signal.Signals[sig_name]
            except KeyError:
                return {"status": "error", "error": f"Unknown signal: {sig_value}"}
        if self._proc is None or self._proc.poll() is not None:
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

    async def _shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True

        # Stop reading PTY output.
        if self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (ValueError, OSError):
                pass

        # Cancel tap senders so they don't block on pending sends.
        for _, task in self._tap_senders.values():
            task.cancel()
        self._tap_senders.clear()

        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        if self._server:
            self._server.close()

        sock = socket_path_for(self.session_id)
        if sock.exists():
            sock.unlink()

        unregister_session(self.session_id)
        asyncio.get_running_loop().stop()


def run_server(session_id: str, command: str, rows: int = 24, cols: int = 80,
               foreground: bool = False, time_limit: float | None = None) -> int:
    """Entry point for the server process. Returns the child's exit code (0 if unknown)."""
    server = PTYServer(session_id, command, rows=rows, cols=cols, foreground=foreground,
                       time_limit=time_limit)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.start())
    try:
        loop.run_forever()
    except Exception:
        pass
    finally:
        loop.close()
    if server.exit_code is not None:
        return server.exit_code
    if server.exit_signal is not None:
        return 128 + server.exit_signal
    return 0


def daemonize_server(session_id: str, command: str, rows: int = 24, cols: int = 80,
                     time_limit: float | None = None) -> dict:
    """Launch a detached subprocess to run the PTY server. Returns status dict."""
    from pty_tools.common import ensure_socket_dir

    ensure_socket_dir()
    sock_path = socket_path_for(session_id)

    # Stderr goes to a temp file so we can capture startup errors without
    # leaving a pipe open (a broken pipe would SIGPIPE the long-lived server).
    err_fd, err_path = tempfile.mkstemp(prefix="pty_err_")
    err_file = os.fdopen(err_fd, "w")

    cmd_args = [sys.executable, "-m", "pty_tools.server", session_id, command,
                "--rows", str(rows), "--cols", str(cols)]
    if time_limit is not None:
        cmd_args.extend(["--time_limit", str(time_limit)])

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
        if sock_path.exists():
            _read_and_cleanup_err()
            return {
                "status": "ok",
                "session_id": session_id,
                "command": command,
                "pid": proc.pid,
                "socket_path": str(sock_path),
            }
        if proc.poll() is not None:
            stderr = _read_and_cleanup_err()
            msg = f"Server for session '{session_id}' did not start"
            if stderr:
                msg += f": {stderr}"
            return {"status": "error", "error": msg}
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
    parser.add_argument("--time_limit", type=float, default=None)
    args = parser.parse_args()

    try:
        rc = run_server(args.session_id, args.command, rows=args.rows, cols=args.cols,
                        foreground=args.foreground, time_limit=args.time_limit)
        sys.exit(rc)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
