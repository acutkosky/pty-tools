"""Async PTY server — one process per session, communicates via Unix domain socket."""

import asyncio
import json
import os
import signal
import time

import pexpect

from pty_tools.common import (
    register_session,
    socket_path_for,
    unregister_session,
)
from pty_tools.screen import ScreenTracker


def get_response(program, total_timeout, stable_timeout, pattern, screen_tracker, strip_ansi):
    """Read from pexpect child using expect(). Closely follows the design doc."""
    start = time.monotonic()
    got_output = False
    buffer = []
    ms = 1.0 / 1000
    exited = False
    total_timeout_sec = total_timeout * ms
    stable_timeout_sec = stable_timeout * ms

    # When no pattern is given, use "(?!)" as the expect pattern.
    # This pattern cannot match anything, so pexpect will never find a match
    # and we'll be forced to hit the TIMEOUT or EOF exceptions.
    expect_pattern = pattern if pattern is not None else "(?!)"

    while True:
        elapsed = time.monotonic() - start
        time_left = total_timeout_sec - elapsed
        if time_left < 0:
            break

        if not got_output:
            timeout = time_left
        else:
            timeout = min(stable_timeout_sec, time_left) if stable_timeout >= 0 else time_left

        matched = False
        try:
            program.expect(expect_pattern, timeout=timeout)
            raw = program.before + program.after
            matched = True
        except pexpect.TIMEOUT:
            raw = program.before
        except pexpect.EOF:
            raw = program.before
            exited = True

        screen_tracker.update_state(raw)
        if raw:
            buffer.append(raw)
            got_output = True

        if matched or exited:
            break
        if got_output and not raw:
            break  # output was stable for stable_timeout

    # If the child exited, wait for it so we can get the exit code
    exit_code = None
    exit_signal = None
    if exited:
        try:
            program.close()
            exit_code = program.exitstatus
            exit_signal = program.signalstatus
        except Exception:
            pass

    collected = b"".join(buffer)
    result = screen_tracker.process_output(collected, strip_ansi=strip_ansi)
    response = {
        "status": "ok",
        "exited": exited,
        "response": result["text"],
        "mode": result["mode"],
    }
    if exited:
        response["exit_code"] = exit_code
        response["signal"] = exit_signal
    return response


class PTYServer:
    """Manages a single PTY session with an async Unix socket interface."""

    def __init__(self, session_id: str, command: str, rows: int = 24, cols: int = 80):
        self.session_id = session_id
        self.command = command
        self.rows = rows
        self.cols = cols
        self.sock_path = str(socket_path_for(session_id))

        self.child: pexpect.spawn | None = None
        self.exited = False
        self.screen_tracker = ScreenTracker(rows=rows, cols=cols)
        self.read_lock = asyncio.Lock()
        self._server: asyncio.Server | None = None

    async def start(self):
        self.child = pexpect.spawn(
            self.command, encoding=None, dimensions=(self.rows, self.cols),
        )
        register_session(self.session_id, self.command, os.getpid(), self.sock_path)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.sock_path
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))

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
        self.child.send(text.encode("utf-8") if isinstance(text, str) else text)
        return {"status": "ok"}

    async def _do_read(self, msg: dict) -> dict:
        total_timeout = msg.get("total_timeout", 5000)
        stable_timeout = msg.get("stable_timeout", 500)
        pattern = msg.get("pattern")
        strip_ansi = msg.get("strip_ansi", True)

        async with self.read_lock:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, get_response, self.child, total_timeout, stable_timeout,
                pattern, self.screen_tracker, strip_ansi,
            )
        self.exited = result["exited"]
        return result

    async def _shutdown(self):
        if self.child and self.child.isalive():
            try:
                self.child.terminate(force=True)
            except Exception:
                pass

        if self._server:
            self._server.close()

        sock = socket_path_for(self.session_id)
        if sock.exists():
            sock.unlink()

        unregister_session(self.session_id)
        asyncio.get_running_loop().stop()


def run_server(session_id: str, command: str, rows: int = 24, cols: int = 80):
    """Entry point for the daemonized server process."""
    server = PTYServer(session_id, command, rows=rows, cols=cols)
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
    """Fork a daemon process to run the PTY server. Returns status dict."""
    import tempfile

    from pty_tools.common import ensure_socket_dir

    ensure_socket_dir()
    sock_path = socket_path_for(session_id)

    # Temp file for the child to report startup errors back to the parent
    err_fd, err_path = tempfile.mkstemp(prefix="pty_err_")
    os.close(err_fd)

    pid = os.fork()
    if pid == 0:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        try:
            run_server(session_id, command, rows=rows, cols=cols)
        except Exception as e:
            with open(err_path, "w") as f:
                f.write(str(e))
        os._exit(0)
    else:
        for _ in range(20):
            if sock_path.exists():
                os.unlink(err_path)
                return {
                    "status": "ok",
                    "session_id": session_id,
                    "command": command,
                    "pid": pid,
                    "socket_path": str(sock_path),
                }
            time.sleep(0.1)

        # Socket never appeared — check if the child wrote an error
        error_detail = ""
        try:
            with open(err_path) as f:
                error_detail = f.read().strip()
        except Exception:
            pass
        finally:
            try:
                os.unlink(err_path)
            except Exception:
                pass

        msg = f"Server for session '{session_id}' did not start"
        if error_detail:
            msg += f": {error_detail}"
        return {"status": "error", "error": msg}
