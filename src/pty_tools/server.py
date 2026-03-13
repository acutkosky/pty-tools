"""Async PTY server — one process per session, communicates via Unix domain socket."""

import argparse
import asyncio
import fcntl
import json
import os
import pty as pty_mod
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

from pty_tools.common import (
    register_session,
    socket_path_for,
    unregister_session,
)

# Regex to strip ANSI escape sequences from raw output
_ANSI_RE = re.compile(r"\x1b\[[\x20-\x3f]*[0-9;]*[\x20-\x7e]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[>=<]")


def _open_pty(rows: int, cols: int):
    """Create a PTY pair and set the terminal size. Returns (master_fd, slave_fd)."""
    master_fd, slave_fd = pty_mod.openpty()
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    return master_fd, slave_fd


class PTYServer:
    """Manages a single PTY session with an async Unix socket interface."""

    def __init__(self, session_id: str, command: str, rows: int = 24, cols: int = 80,
                 foreground: bool = False):
        self.session_id = session_id
        self.command = command
        self.rows = rows
        self.cols = cols
        self.sock_path = str(socket_path_for(session_id))
        self.foreground = foreground

        self._master_fd: int | None = None
        self._proc: subprocess.Popen | None = None
        self.exited = False
        self.exit_code = None
        self.exit_signal = None
        self.read_lock = asyncio.Lock()
        self.read_buffer = bytearray()
        self._server: asyncio.Server | None = None
        self._new_data = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

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
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))
        except RuntimeError:
            pass

        # Start background PTY reader thread
        threading.Thread(target=self._read_loop, daemon=True).start()

        # Start stdin forwarder if foreground
        if self.foreground:
            threading.Thread(target=self._stdin_loop, daemon=True).start()

    def _read_loop(self):
        """Background thread: continuously reads from PTY and dispatches to asyncio."""
        while True:
            try:
                ready, _, _ = select.select([self._master_fd], [], [], 1.0)
            except (ValueError, OSError):
                # fd closed
                break
            if not ready:
                continue
            try:
                raw = os.read(self._master_fd, 4096)
            except OSError:
                # EIO means the slave side closed (child exited)
                raw = b""
            if not raw:
                self._collect_exit_status()
                return
            self._loop.call_soon_threadsafe(self._on_data, raw)

    def _collect_exit_status(self):
        """Wait for the child to finish and dispatch exit info to asyncio."""
        exit_code = None
        exit_signal = None
        try:
            self._proc.wait(timeout=5.0)
            rc = self._proc.returncode
            if rc >= 0:
                exit_code = rc
            else:
                exit_signal = -rc
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._on_eof, exit_code, exit_signal)

    def _stdin_loop(self):
        """Background thread: reads stdin line-by-line, forwards to PTY."""
        try:
            for line in sys.stdin:
                if self.exited:
                    break
                os.write(self._master_fd, line.encode("utf-8"))
        except (EOFError, OSError):
            pass

    def _on_data(self, raw: bytes):
        """Called on asyncio thread when PTY produces output."""
        self.read_buffer.extend(raw)
        if self.foreground:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
        self._new_data.set()

    def _on_eof(self, exit_code, exit_signal):
        """Called on asyncio thread when PTY child exits."""
        self.exited = True
        self.exit_code = exit_code
        self.exit_signal = exit_signal
        self._new_data.set()
        if self.foreground:
            asyncio.create_task(self._shutdown())

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a single client connection."""
        shutdown_after = False
        try:
            data = await asyncio.wait_for(reader.read(), timeout=5.0)
            msg = json.loads(data.decode())
            msg_type = msg.get("type")

            if msg_type == "write":
                response = self._do_write(msg)
            elif msg_type == "read":
                response = await self._do_read(msg)
            elif msg_type == "interact":
                write_result = self._do_write(msg)
                if write_result.get("status") == "error":
                    response = write_result
                else:
                    response = await self._do_read(msg)
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

    def _do_write(self, msg: dict) -> dict:
        text = msg.get("text", "")
        if self.exited:
            return {"status": "error", "error": "Session has exited"}
        data = text.encode("utf-8") if isinstance(text, str) else text
        os.write(self._master_fd, data)
        return {"status": "ok"}

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

    async def _shutdown(self):
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
               foreground: bool = False):
    """Entry point for the server process."""
    server = PTYServer(session_id, command, rows=rows, cols=cols, foreground=foreground)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.start())
    try:
        loop.run_forever()
    except Exception:
        pass
    finally:
        loop.close()


def daemonize_server(session_id: str, command: str, rows: int = 24, cols: int = 80) -> dict:
    """Launch a detached subprocess to run the PTY server. Returns status dict."""
    from pty_tools.common import ensure_socket_dir

    ensure_socket_dir()
    sock_path = socket_path_for(session_id)

    # Stderr goes to a temp file so we can capture startup errors without
    # leaving a pipe open (a broken pipe would SIGPIPE the long-lived server).
    err_fd, err_path = tempfile.mkstemp(prefix="pty_err_")
    err_file = os.fdopen(err_fd, "w")

    proc = subprocess.Popen(
        [sys.executable, "-m", "pty_tools.server", session_id, command,
         "--rows", str(rows), "--cols", str(cols)],
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
    args = parser.parse_args()

    try:
        run_server(args.session_id, args.command, rows=args.rows, cols=args.cols,
                   foreground=args.foreground)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
