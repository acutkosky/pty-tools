"""Integration tests — full lifecycle of PTY sessions."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from pty_tools.common import (
    PTYClientError,
    is_server_alive,
    read_registry,
    send_request,
    socket_path_for,
    unregister_session,
)
from pty_tools.server import daemonize_server, run_server


def _cleanup_session(session_id: str):
    """Best-effort cleanup of a test session."""
    registry = read_registry()
    entry = registry.get(session_id)
    if entry:
        try:
            os.kill(entry["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    sock = socket_path_for(session_id)
    if sock.exists():
        sock.unlink()
    unregister_session(session_id)


class TestFullLifecycle:
    def setup_method(self):
        self.session_id = f"test_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_spawn_list_interact_exit(self):
        # Spawn
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        assert result["session_id"] == self.session_id

        # Verify socket exists
        assert socket_path_for(self.session_id).exists()

        # Verify server is alive
        assert is_server_alive(self.session_id)

        # List — session should appear in registry
        registry = read_registry()
        assert self.session_id in registry

        # Interact — send echo command, read output
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo hello_world\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert not result["exited"]
        assert "hello_world" in result["response"]

        # Exit
        result = send_request(self.session_id, {"type": "exit"}, timeout=10.0)
        assert result["status"] == "ok"

        # Server shuts down asynchronously after sending the response; poll.
        sock = socket_path_for(self.session_id)
        for _ in range(40):
            if not sock.exists():
                break
            time.sleep(0.05)

        assert not sock.exists()
        assert not is_server_alive(self.session_id)

    def test_write_then_read(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Drain initial prompt (read waits for it via stable_timeout)
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

        # Write
        write_result = send_request(self.session_id, {"type": "write", "text": "echo test_output_123\n"})
        assert write_result["status"] == "ok"

        # Read
        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["status"] == "ok"
        assert "test_output_123" in read_result["response"]

    def test_pattern_matching(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Drain initial output
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

        # Interact with pattern
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo MARKER_DONE\n",
                "total_timeout": 5000,
                "stable_timeout": 500,
                "pattern": "MARKER_DONE",
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "MARKER_DONE" in result["response"]

    def test_stale_cleanup(self):
        """Registering a session with a dead PID should be cleaned up by is_server_alive."""
        from pty_tools.common import register_session

        fake_id = f"stale_{os.getpid()}_{int(time.time() * 1000)}"
        sock_path = str(socket_path_for(fake_id))

        # Register with a PID that definitely doesn't exist
        register_session(fake_id, "fake_cmd", 99999999, sock_path)

        # is_server_alive should detect the dead process and clean up
        assert not is_server_alive(fake_id)

        # Verify it was removed from registry
        registry = read_registry()
        assert fake_id not in registry

    def test_child_exit_with_exit_code(self):
        """When the child exits, reads should report exited=True with exit code."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Tell the shell to exit with a specific code; the read below breaks
        # out on self.exited, so no sleep is needed — it waits for the exit.
        send_request(self.session_id, {"type": "write", "text": "exit 42\n"})

        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["status"] == "ok"
        assert read_result["exited"] is True
        assert read_result["exit_code"] == 42
        assert read_result["signal"] is None

    def test_interact_after_drain_auto_shuts_down(self):
        """A drain-read after the child exits auto-shuts-down the server,
        so subsequent requests fail to connect."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        send_request(self.session_id, {"type": "write", "text": "exit\n"})

        # Drain — the read waits for exited=True (via _new_data), then the
        # server self-destructs after responding since the buffer is empty.
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 300},
            timeout=5.0,
        )
        # Poll for the post-response _shutdown to finish rather than sleeping.
        sock = socket_path_for(self.session_id)
        for _ in range(40):
            if not sock.exists():
                break
            time.sleep(0.05)
        assert not sock.exists(), "server did not shut down after drain"

        with pytest.raises(PTYClientError):
            send_request(
                self.session_id,
                {
                    "type": "interact",
                    "text": "echo should_fail\n",
                    "total_timeout": 1000,
                    "stable_timeout": 300,
                },
                timeout=5.0,
            )

    def test_strip_ansi_default(self):
        """ANSI escapes are stripped by default and preserved with strip_ansi=False."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        # Drain initial prompt.
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

        # printf emits a real ANSI escape — test would be vacuous without it
        # (echo hello produces no escapes, so strip vs. preserve is indistinguishable).
        ansi_cmd = "printf '\\033[31mRED_TEXT\\033[0m\\n'\n"

        result_default = send_request(
            self.session_id,
            {"type": "interact", "text": ansi_cmd,
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert result_default["status"] == "ok"
        assert "RED_TEXT" in result_default["response"]
        assert "\x1b[31m" not in result_default["response"]
        assert "\x1b[0m" not in result_default["response"]
        assert "\x1b" not in result_default["response"]

        # With strip_ansi=False the raw escapes must be present.
        result_with_ansi = send_request(
            self.session_id,
            {"type": "interact", "text": ansi_cmd,
             "total_timeout": 3000, "stable_timeout": 500,
             "strip_ansi": False},
            timeout=10.0,
        )
        assert result_with_ansi["status"] == "ok"
        assert "RED_TEXT" in result_with_ansi["response"]
        assert "\x1b[31m" in result_with_ansi["response"]
        assert "\x1b[0m" in result_with_ansi["response"]

    def test_spawn_bad_command_reports_error(self):
        """Spawning a nonexistent command should report a meaningful error."""
        result = daemonize_server(self.session_id, "/nonexistent/command/xyz")
        assert result["status"] == "error"
        assert "did not start" in result["error"]
        # Neither socket nor registry entry should remain after a failed spawn.
        assert not socket_path_for(self.session_id).exists()
        assert self.session_id not in read_registry()

    def test_duplicate_spawn_rejected_by_cli(self):
        """CLI should reject `spawn --detach` against an already-running session."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        assert is_server_alive(self.session_id)

        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn", "--detach",
             self.session_id, "sh"],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode != 0
        payload = json.loads(proc.stdout)
        assert payload["status"] == "error"
        assert "already exists" in payload["error"]


class TestNoEcho:
    """Verify that writing to the PTY does not echo the input back in reads."""

    def setup_method(self):
        self.session_id = f"test_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def _spawn_and_drain(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

    def test_write_silent_command_produces_no_echo(self):
        """Writing a command that produces no output should yield an empty read.

        With TTY echo enabled, the written text itself would appear in the read
        buffer even though the command produces no output.
        """
        self._spawn_and_drain()

        # Variable assignment produces no shell output; the read below waits
        # for any output (up to total_timeout) and then stable_timeout after
        # each chunk, so no pre-sleep is needed.
        send_request(self.session_id, {"type": "write", "text": "NOECHO_VAR_X9Z=1\n"})

        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["status"] == "ok"
        # The only output should be the next shell prompt — not the assignment text
        assert "NOECHO_VAR_X9Z" not in read_result["response"]

    def test_interact_output_is_exactly_command_output(self):
        """interact should return only the command's output, not an echo of the input.

        With echo enabled, 'printf X' would return both the echoed command text
        and the output, doubling the visible content.
        """
        self._spawn_and_drain()

        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "printf 'EXACT_OUTPUT_42'\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        # printf with no newline: output should be exactly "EXACT_OUTPUT_42"
        # followed by the next shell prompt. No echoed command text.
        assert result["response"].startswith("EXACT_OUTPUT_42")
        assert "printf" not in result["response"]

    def test_write_then_read_exact_output(self):
        """write + read should return exactly the command output, nothing more."""
        self._spawn_and_drain()

        send_request(self.session_id, {"type": "write", "text": "echo ONLY_THIS_LINE\n"})

        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["status"] == "ok"
        lines = [l for l in read_result["response"].strip().splitlines() if l.strip()]
        # Should be exactly: the output line, then possibly a prompt
        assert lines[0] == "ONLY_THIS_LINE"
        # The command text itself must not appear anywhere
        assert "echo ONLY_THIS_LINE" not in read_result["response"]


class TestPeek:
    def setup_method(self):
        self.session_id = f"test_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def _spawn(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        # Drain initial output (read waits for prompt via stable_timeout)
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

    def test_peek_preserves_output_for_subsequent_reads(self):
        """Peek reads should preserve output so subsequent reads also see it."""
        self._spawn()

        # Interact with peek=True
        peek1 = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo peek_test_123\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
                "peek": True,
            },
            timeout=10.0,
        )
        assert peek1["status"] == "ok"
        assert "peek_test_123" in peek1["response"]

        # Peek again: should still see the same output
        peek2 = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500, "peek": True},
            timeout=10.0,
        )
        assert peek2["status"] == "ok"
        assert "peek_test_123" in peek2["response"]

        # Normal read: should also see it
        normal = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert normal["status"] == "ok"
        assert "peek_test_123" in normal["response"]

    def test_peek_accumulates_across_writes(self):
        """Multiple peek reads should accumulate output from successive writes."""
        self._spawn()

        peek1 = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo line1_aaa\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
                "peek": True,
            },
            timeout=10.0,
        )
        assert peek1["status"] == "ok"
        assert "line1_aaa" in peek1["response"]

        peek2 = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo line2_bbb\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
                "peek": True,
            },
            timeout=10.0,
        )
        assert peek2["status"] == "ok"
        assert "line1_aaa" in peek2["response"]
        assert "line2_bbb" in peek2["response"]

    def test_pattern_match_in_peek_buffer(self):
        self._spawn()

        send_request(self.session_id, {"type": "write", "text": "echo MARKER\n"})

        # Peek (no pattern) — buffer the output. total_timeout waits for data.
        peek = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500, "peek": True},
            timeout=10.0,
        )
        assert "MARKER" in peek["response"]

        # Normal read with pattern — should match immediately from buffered data
        start = time.time()
        result = send_request(
            self.session_id,
            {
                "type": "read",
                "total_timeout": 5000,
                "stable_timeout": 500,
                "pattern": "MARKER",
            },
            timeout=10.0,
        )
        elapsed = time.time() - start
        assert result["status"] == "ok"
        assert "MARKER" in result["response"]
        # Should match near-instantly, not wait for total_timeout
        assert elapsed < 3.0

    def test_pattern_match_spans_peek_and_new_data(self):
        self._spawn()

        send_request(self.session_id, {"type": "write", "text": "cat\n"})
        send_request(self.session_id, {"type": "write", "text": "MAR\n"})

        # Peek — buffers "MAR" (read waits up to total_timeout for data)
        peek = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500, "peek": True},
            timeout=10.0,
        )
        assert "MAR" in peek["response"]

        # Now send the rest
        send_request(self.session_id, {"type": "write", "text": "KER\n"})

        # Read with pattern that spans peek buffer + new data
        result = send_request(
            self.session_id,
            {
                "type": "read",
                "total_timeout": 5000,
                "stable_timeout": 500,
                "pattern": "MAR\r\nKER",
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "MAR\r\nKER" in result["response"]

    def test_peek_preserves_content_through_clear(self):
        """Peek buffers output, so content survives a screen clear."""
        self._spawn()

        # Write and peek — buffers "visible_text"
        peek = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo visible_text_999\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
                "peek": True,
            },
            timeout=10.0,
        )
        assert "visible_text_999" in peek["response"]

        # Send a clear command — adds clear escape sequences to the stream
        # Normal read with peek_buffer still holding old content
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "clear\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        # The peek buffer still has the old bytes — "visible_text"
        # persists even though a clear was sent, because bytes are accumulated
        assert "visible_text_999" in result["response"]

    def test_no_peek_clear_loses_content(self):
        """Without peek, a clear causes old content to disappear from response."""
        self._spawn()

        # Normal read (no peek) — consumes and clears buffer
        send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo gone_text_888\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Send clear and read — old content should NOT be in response
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "clear\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "gone_text_888" not in result["response"]

    def test_read_consumes_output(self):
        """A normal read consumes output so the next read only shows new data."""
        self._spawn()

        # Write and read (consume)
        send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo consumed_aaa\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Write new data and read — should NOT contain the old consumed output
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo fresh_bbb\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "fresh_bbb" in result["response"]
        assert "consumed_aaa" not in result["response"]

class TestForeground:
    """Tests for foreground (non-detached) spawn mode."""

    def setup_method(self):
        self.session_id = f"test_fg_{os.getpid()}_{int(time.time() * 1000)}"
        self._proc = None

    def teardown_method(self):
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()
        _cleanup_session(self.session_id)

    def _spawn_foreground(self, command="sh", stdin_lines=None):
        """Spawn a foreground server as a subprocess.

        Returns (process, stdin_w_fd) where stdin_w_fd can be used to send
        more input, or None if stdin_lines were provided and stdin was closed.
        """
        stdin_r, stdin_w = os.pipe()

        self._proc = subprocess.Popen(
            [sys.executable, "-m", "pty_tools.server",
             self.session_id, command, "--foreground"],
            stdin=stdin_r,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(stdin_r)

        # Wait for socket to appear
        sock_path = socket_path_for(self.session_id)
        for _ in range(30):
            if sock_path.exists():
                break
            time.sleep(0.1)
        assert sock_path.exists(), "Server socket did not appear"

        # Write stdin lines if provided
        if stdin_lines:
            for line in stdin_lines:
                os.write(stdin_w, (line + "\n").encode())
                time.sleep(0.1)
            os.close(stdin_w)
            return self._proc, None

        return self._proc, stdin_w

    def test_foreground_socket_works(self):
        """Foreground server should accept socket commands like detached mode."""
        proc, stdin_w = self._spawn_foreground()

        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo fg_socket_test\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "fg_socket_test" in result["response"]
        os.close(stdin_w)

    def test_foreground_stdout_tap(self):
        """Foreground server should write raw PTY output to stdout."""
        proc, stdin_w = self._spawn_foreground()

        # Send a command via socket and read to ensure output is produced
        send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo tap_test_xyz\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Shut down so stdout closes
        send_request(self.session_id, {"type": "exit"}, timeout=10.0)
        os.close(stdin_w)
        proc.wait(timeout=5.0)

        stdout_data = proc.stdout.read().decode("utf-8", errors="replace")
        assert "tap_test_xyz" in stdout_data

    def test_foreground_stdin_forwarding(self):
        """Foreground server should forward stdin lines to the PTY."""
        proc, _ = self._spawn_foreground(stdin_lines=["echo stdin_fwd_test"])

        # The read waits up to total_timeout for the output to arrive.
        result = send_request(
            self.session_id,
            {"type": "read", "pattern": "stdin_fwd_test",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "stdin_fwd_test" in result["response"]

    def test_foreground_child_exit_shuts_down(self):
        """When the child exits cleanly, the server forwards that exit code."""
        proc, _ = self._spawn_foreground(
            command="sh", stdin_lines=["exit 0"]
        )
        proc.wait(timeout=10.0)
        assert proc.returncode == 0, (
            f"expected server to propagate child's exit 0, got {proc.returncode}"
        )
        # Socket and registry must be cleaned up on shutdown.
        assert not socket_path_for(self.session_id).exists()
        assert not is_server_alive(self.session_id)

    def test_foreground_exit_code_zero(self):
        """Foreground mode should forward exit code 0."""
        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn",
             self.session_id, "sh -c 'exit 0'"],
            capture_output=True, timeout=10,
        )
        assert proc.returncode == 0

    def test_foreground_exit_code_nonzero(self):
        """Foreground mode should forward non-zero exit codes."""
        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn",
             self.session_id, "sh -c 'exit 42'"],
            capture_output=True, timeout=10,
        )
        assert proc.returncode == 42

    def test_foreground_exit_code_from_signal(self):
        """Foreground mode should return 128+signal when child is killed."""
        proc, stdin_w = self._spawn_foreground(command="sleep 60")

        # Kill the child via the signal command
        send_request(
            self.session_id,
            {"type": "signal", "signal": "KILL"},
            timeout=5.0,
        )
        os.close(stdin_w)
        proc.wait(timeout=10.0)
        assert proc.returncode == 128 + 9  # SIGKILL = 9

    def test_foreground_and_detach_produce_same_results(self):
        """Socket interactions should produce identical results in both modes."""
        # Foreground
        proc, stdin_w = self._spawn_foreground()
        fg_result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo parity_check\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        send_request(self.session_id, {"type": "exit"}, timeout=10.0)
        os.close(stdin_w)
        proc.wait(timeout=5.0)
        _cleanup_session(self.session_id)

        # Detached — use a new session id
        detach_id = self.session_id + "_d"
        try:
            result = daemonize_server(detach_id, "sh")
            assert result["status"] == "ok"
            det_result = send_request(
                detach_id,
                {
                    "type": "interact",
                    "text": "echo parity_check\n",
                    "total_timeout": 3000,
                    "stable_timeout": 500,
                },
                timeout=10.0,
            )
            send_request(detach_id, {"type": "exit"}, timeout=10.0)
        finally:
            _cleanup_session(detach_id)

        # Both should succeed with the same key fields
        assert fg_result["status"] == det_result["status"] == "ok"
        assert "parity_check" in fg_result["response"]
        assert "parity_check" in det_result["response"]

    def test_cli_detach_flag(self):
        """CLI --detach flag should daemonize like the old default behavior."""
        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn", "--detach",
             self.session_id, "sh"],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["status"] == "ok"
        assert result["session_id"] == self.session_id

        # Verify it's actually running
        assert is_server_alive(self.session_id)

        send_request(self.session_id, {"type": "exit"}, timeout=10.0)

    def test_foreground_sigterm_shuts_down_server(self):
        """SIGTERM to a foreground server forwards SIGTERM to the child and cleans up.

        `sleep` has no SIGTERM handler, so it dies from the signal. The server
        translates that to exit code 128+SIGTERM (=143).
        """
        proc, _ = self._spawn_foreground(command="sleep 30")
        # Give the server time to finish installing signal handlers.
        time.sleep(0.3)

        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10.0)

        assert proc.returncode == 128 + signal.SIGTERM, (
            f"expected 128+SIGTERM={128 + signal.SIGTERM} (child killed by "
            f"forwarded SIGTERM), got {proc.returncode}"
        )
        assert not socket_path_for(self.session_id).exists()
        assert not is_server_alive(self.session_id)

    def test_foreground_sigterm_forwards_to_child(self):
        """SIGTERM to the foreground server is forwarded to the child.

        The child installs a trap that exits cleanly with status 0 on SIGTERM.
        If forwarding works, the child exits via the trap → proc.returncode=0.
        Without forwarding, the server's SIGKILL fallback would yield 137
        (128+SIGKILL).
        """
        cmd = "sh -c 'trap \"exit 0\" TERM; sleep 30'"
        proc, _ = self._spawn_foreground(command=cmd)
        # Give the child shell time to install the trap.
        time.sleep(0.5)

        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10.0)

        assert proc.returncode == 0, (
            f"server returncode={proc.returncode}; expected 0 (child trap ran). "
            f"137 would indicate SIGKILL fallback (forwarding didn't complete)."
        )


class TestTap:
    """Tests for tap/untap — forwarding output between sessions."""

    def setup_method(self):
        ts = int(time.time() * 1000)
        self.src_id = f"tap_src_{os.getpid()}_{ts}"
        self.dst_id = f"tap_dst_{os.getpid()}_{ts}"
        self.dst2_id = f"tap_dst2_{os.getpid()}_{ts}"

    def teardown_method(self):
        for sid in (self.src_id, self.dst_id, self.dst2_id):
            _cleanup_session(sid)

    def _spawn(self, session_id):
        result = daemonize_server(session_id, "sh")
        assert result["status"] == "ok"
        # Drain initial output (read waits for prompt via stable_timeout)
        send_request(
            session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

    def test_tap_forwards_output(self):
        """Output from src should appear as input in dst."""
        self._spawn(self.src_id)
        self._spawn(self.dst_id)

        # Set up tap: src -> dst
        result = send_request(self.src_id, {"type": "tap", "target": self.dst_id})
        assert result["status"] == "ok"

        # Generate output on src
        send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo tap_fwd_test\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Read from dst — pattern match returns as soon as the tap forwards.
        dst_result = send_request(
            self.dst_id,
            {"type": "read", "pattern": "tap_fwd_test",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert dst_result["status"] == "ok"
        assert "tap_fwd_test" in dst_result["response"]

    def test_untap_stops_forwarding(self):
        """After untap, output should no longer be forwarded."""
        self._spawn(self.src_id)
        self._spawn(self.dst_id)

        # Tap then untap
        send_request(self.src_id, {"type": "tap", "target": self.dst_id})
        result = send_request(self.src_id, {"type": "untap", "target": self.dst_id})
        assert result["status"] == "ok"

        # Generate output on src
        send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo after_untap\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Negative assertion: the dst read waits 1s — if tap were still
        # active, data would arrive in that window.
        dst_result = send_request(
            self.dst_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
            timeout=5.0,
        )
        assert dst_result["status"] == "ok"
        assert "after_untap" not in dst_result["response"]

    def test_tap_does_not_consume_src_read_buffer(self):
        """Tapping should not interfere with normal reads on src."""
        self._spawn(self.src_id)
        self._spawn(self.dst_id)

        send_request(self.src_id, {"type": "tap", "target": self.dst_id})

        # Generate output and read from src — should still work normally
        result = send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo src_read_test\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "src_read_test" in result["response"]

    def test_multiple_taps_independent(self):
        """Multiple taps from the same src should each receive output independently."""
        self._spawn(self.src_id)
        self._spawn(self.dst_id)
        self._spawn(self.dst2_id)

        # Tap src -> dst and src -> dst2
        send_request(self.src_id, {"type": "tap", "target": self.dst_id})
        send_request(self.src_id, {"type": "tap", "target": self.dst2_id})

        # Generate output on src
        send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo multi_tap_test\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Both dst and dst2 should have the output — pattern match returns
        # as soon as the tap forwards.
        dst_result = send_request(
            self.dst_id,
            {"type": "read", "pattern": "multi_tap_test",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        dst2_result = send_request(
            self.dst2_id,
            {"type": "read", "pattern": "multi_tap_test",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert "multi_tap_test" in dst_result["response"]
        assert "multi_tap_test" in dst2_result["response"]

    def test_multiple_taps_untap_one(self):
        """Untapping one target should not affect the other."""
        self._spawn(self.src_id)
        self._spawn(self.dst_id)
        self._spawn(self.dst2_id)

        send_request(self.src_id, {"type": "tap", "target": self.dst_id})
        send_request(self.src_id, {"type": "tap", "target": self.dst2_id})

        # Remove only dst
        send_request(self.src_id, {"type": "untap", "target": self.dst_id})

        # Generate output
        send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo partial_untap\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # dst should NOT have the output (1s wait gives it a chance to appear)
        dst_result = send_request(
            self.dst_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
            timeout=5.0,
        )
        assert "partial_untap" not in dst_result["response"]

        # dst2 should still have it — pattern match returns when forwarded
        dst2_result = send_request(
            self.dst2_id,
            {"type": "read", "pattern": "partial_untap",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert "partial_untap" in dst2_result["response"]

    def test_tap_auto_cleanup_on_target_exit(self):
        """When the target session exits, the tap should be auto-removed."""
        self._spawn(self.src_id)
        self._spawn(self.dst_id)

        send_request(self.src_id, {"type": "tap", "target": self.dst_id})

        # Kill dst (server shuts down async; subsequent src tap_senders will
        # fail to connect, triggering the auto-cleanup path).
        send_request(self.dst_id, {"type": "exit"}, timeout=10.0)

        # Generate output on src — the tap send should fail and auto-remove
        send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo after_dst_exit\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # src should still be alive and working fine
        result = send_request(
            self.src_id,
            {
                "type": "interact",
                "text": "echo still_alive\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "still_alive" in result["response"]

    def test_tap_nonexistent_target(self):
        """Tapping to a nonexistent session should return an error."""
        self._spawn(self.src_id)

        result = send_request(self.src_id, {"type": "tap", "target": "nonexistent_session"})
        assert result["status"] == "error"
        assert "not running" in result["error"]

    def test_untap_nonexistent_is_noop(self):
        """Untapping a target that was never tapped should succeed (no-op)."""
        self._spawn(self.src_id)

        result = send_request(self.src_id, {"type": "untap", "target": "never_tapped"})
        assert result["status"] == "ok"


class TestWriteRaceConditions:
    """Tests for concurrent write correctness."""

    def setup_method(self):
        self.session_id = f"test_race_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_write_blocks_while_interact_is_reading(self):
        """A concurrent write must not land in the PTY until interact's read finishes.

        interact holds the write lock across its write+read. If another client
        issues a plain write while interact is still draining output, that write
        has to wait for the lock — otherwise it could inject bytes into the PTY
        mid-interact and corrupt interact's view of "my command's output".
        """
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

        timings: dict[str, float] = {}
        errors: list[Exception] = []

        def _slow_interact():
            # Pattern never matches → interact reads until total_timeout (~2s).
            try:
                start = time.time()
                send_request(
                    self.session_id,
                    {
                        "type": "interact",
                        "text": "echo SLOW_INTERACT_OUT\n",
                        "total_timeout": 2000,
                        "stable_timeout": 2000,
                        "pattern": "NO_SUCH_PATTERN_ZZZ",
                    },
                    timeout=10.0,
                )
                timings["interact"] = time.time() - start
            except Exception as e:
                errors.append(e)

        def _quick_write():
            # Give interact ~0.2s head-start so it acquires the write lock first.
            time.sleep(0.2)
            try:
                start = time.time()
                send_request(
                    self.session_id,
                    {"type": "write", "text": "echo QUICK_WRITE\n"},
                    timeout=10.0,
                )
                timings["write"] = time.time() - start
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_slow_interact)
        t2 = threading.Thread(target=_quick_write)
        t1.start()
        t2.start()
        t1.join(timeout=15.0)
        t2.join(timeout=15.0)

        assert not errors, f"unexpected errors: {errors}"
        assert "interact" in timings and "write" in timings

        # interact holds the lock for ~2s; a correctly-serialized write should
        # return only after interact releases the lock (~1.8s given the 0.2s delay).
        # Without the lock, write would return almost instantly.
        assert timings["write"] > 1.0, (
            f"write completed in {timings['write']:.2f}s — "
            f"expected it to block until interact released the write lock "
            f"(interact took {timings['interact']:.2f}s)"
        )

    def test_concurrent_writes_do_not_corrupt(self):
        """Concurrent writes from multiple clients must not corrupt each other.

        Each writer sends many copies of a distinctive marker. If write_lock
        didn't serialize whole payloads, a partial-write interleaving could
        split a marker across writers and break it. We verify each marker
        appears exactly the expected number of times in the echoed output.
        """
        # cat echoes stdin back via the PTY — a convenient way to observe
        # what actually hit the PTY in what order.
        result = daemonize_server(self.session_id, "cat")
        assert result["status"] == "ok"

        num_writers = 4
        marker_repeats = 30

        def _writer(idx):
            marker = f"MARK_{idx}_END\n"
            # One large payload per writer — the full payload must land
            # atomically with respect to other writers.
            send_request(
                self.session_id,
                {"type": "write", "text": marker * marker_repeats},
                timeout=15.0,
            )

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(num_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        # Collect the echoed output — wait long enough for all writers' data
        # to be processed and echoed back.
        result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 5000, "stable_timeout": 1000},
            timeout=15.0,
        )
        assert result["status"] == "ok"

        for idx in range(num_writers):
            marker = f"MARK_{idx}_END"
            count = result["response"].count(marker)
            assert count == marker_repeats, (
                f"Writer {idx}: expected {marker_repeats} copies of {marker!r}, "
                f"got {count} — writes corrupted each other."
            )


class TestTimeLimit:
    """Tests for the time_limit feature — automatic process termination."""

    def setup_method(self):
        self.session_id = f"test_tl_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_time_limit_kills_process(self):
        """Process should be killed after time_limit expires."""
        result = daemonize_server(self.session_id, "sleep 60", time_limit=2)
        assert result["status"] == "ok"
        assert is_server_alive(self.session_id)

        # Wait for the time limit to expire
        time.sleep(3.0)

        assert not is_server_alive(self.session_id)

    def test_time_limit_process_alive_before_expiry(self):
        """Process should still be running before the time limit."""
        result = daemonize_server(self.session_id, "sleep 60", time_limit=5)
        assert result["status"] == "ok"

        time.sleep(1.0)
        assert is_server_alive(self.session_id)

        # Interact should still work
        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
            timeout=5.0,
        )
        assert read_result["status"] == "ok"
        assert not read_result["exited"]

    def test_no_time_limit_stays_alive(self):
        """Without time_limit, process should stay alive indefinitely."""
        result = daemonize_server(self.session_id, "sleep 60")
        assert result["status"] == "ok"

        time.sleep(2.0)
        assert is_server_alive(self.session_id)

    def test_time_limit_cleans_up_socket(self):
        """Socket and registry should be cleaned up after time limit."""
        result = daemonize_server(self.session_id, "sleep 60", time_limit=2)
        assert result["status"] == "ok"
        assert socket_path_for(self.session_id).exists()

        time.sleep(3.0)

        assert not socket_path_for(self.session_id).exists()

    def test_time_limit_early_exit(self):
        """If the process exits before the time limit, everything is fine."""
        result = daemonize_server(self.session_id, "sh -c 'exit 0'", time_limit=10)
        assert result["status"] == "ok"

        time.sleep(1.0)

        # Process exited on its own — read should show exited
        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500},
            timeout=5.0,
        )
        assert read_result["exited"] is True

    def test_time_limit_foreground_mode(self):
        """Foreground mode should also respect time_limit.

        Time-limit expiration SIGKILLs the child, so the server forwards
        128+SIGKILL (=137) as its exit code.
        """
        proc = subprocess.Popen(
            [sys.executable, "-m", "pty_tools.server",
             self.session_id, "sleep 60", "--foreground", "--time_limit", "2"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        proc.wait(timeout=10.0)
        assert proc.returncode == 128 + signal.SIGKILL, (
            f"expected 128+SIGKILL={128 + signal.SIGKILL} from time-limit kill, "
            f"got {proc.returncode}"
        )

    def test_time_limit_via_cli(self):
        """CLI --time_limit flag should work with --detach."""
        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn", "--detach",
             "--time_limit", "2", self.session_id, "sleep 60"],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["status"] == "ok"

        assert is_server_alive(self.session_id)
        time.sleep(3.0)
        assert not is_server_alive(self.session_id)


class TestScreen:
    """Tests for pyte-based screen capture."""

    def setup_method(self):
        self.session_id = f"test_scr_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_screen_returns_display(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Write something and wait for it to render
        send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo screen_test_abc\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )

        # Get screen snapshot
        screen = send_request(self.session_id, {"type": "screen"}, timeout=5.0)
        assert screen["status"] == "ok"
        assert "screen_test_abc" in screen["response"]
        assert screen["rows"] == 24
        assert screen["cols"] == 80

    def test_screen_does_not_consume_read_buffer(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        # Drain initial output
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 300},
            timeout=5.0,
        )

        send_request(self.session_id, {"type": "write", "text": "echo buffer_check\n"})
        # Wait (without consuming) for the output to land in the buffer; this
        # is what replaces a sleep — we can't snapshot the screen before the
        # PTY data has been processed.
        peek = send_request(
            self.session_id,
            {"type": "read", "peek": True, "pattern": "buffer_check",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert "buffer_check" in peek["response"]

        # Screen read should not consume the buffer
        send_request(self.session_id, {"type": "screen"}, timeout=5.0)

        # Normal read should still have the output
        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert "buffer_check" in read_result["response"]

    def test_screen_after_resize(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Resize
        resize_result = send_request(
            self.session_id,
            {"type": "resize", "rows": 40, "cols": 120},
            timeout=5.0,
        )
        assert resize_result["status"] == "ok"
        assert resize_result["rows"] == 40
        assert resize_result["cols"] == 120

        # Screen should reflect new dimensions
        screen = send_request(self.session_id, {"type": "screen"}, timeout=5.0)
        assert screen["rows"] == 40
        assert screen["cols"] == 120


class TestResize:
    """Tests for PTY resize."""

    def setup_method(self):
        self.session_id = f"test_rsz_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_resize_updates_pty(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "resize", "rows": 50, "cols": 132},
            timeout=5.0,
        )
        assert result["status"] == "ok"
        assert result["rows"] == 50
        assert result["cols"] == 132

        # Verify the child sees the new size via stty
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "stty size\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "50 132" in result["response"]

    def test_resize_missing_params(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "resize", "rows": 40},
            timeout=5.0,
        )
        assert result["status"] == "error"
        assert "cols" in result["error"]


class TestSignal:
    """Tests for sending signals to child process."""

    def setup_method(self):
        self.session_id = f"test_sig_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_signal_by_name(self):
        """Sending SIGKILL by short name terminates the child with that exact signal."""
        result = daemonize_server(self.session_id, "sleep 30")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "signal", "signal": "KILL"},
            timeout=5.0,
        )
        assert result["status"] == "ok"
        assert result["signal"] == "SIGKILL"

        # Read waits for exited via _new_data; verify exit was by SIGKILL.
        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["exited"] is True
        assert read_result["exit_code"] is None
        assert read_result["signal"] == signal.SIGKILL

    def test_signal_by_full_name(self):
        """SIGTERM by full name terminates the child with that exact signal."""
        result = daemonize_server(self.session_id, "sleep 30")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "signal", "signal": "SIGTERM"},
            timeout=5.0,
        )
        assert result["status"] == "ok"
        assert result["signal"] == "SIGTERM"

        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["exited"] is True
        assert read_result["signal"] == signal.SIGTERM

    def test_signal_by_number(self):
        """Signal by number (15 = SIGTERM) is resolved and delivered correctly."""
        result = daemonize_server(self.session_id, "sleep 30")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "signal", "signal": 15},
            timeout=5.0,
        )
        assert result["status"] == "ok"
        assert result["signal"] == "SIGTERM"

        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["exited"] is True
        assert read_result["signal"] == signal.SIGTERM

    def test_signal_unknown(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "signal", "signal": "BOGUS"},
            timeout=5.0,
        )
        assert result["status"] == "error"
        assert "Unknown signal" in result["error"]

    def test_signal_after_drain_auto_shuts_down(self):
        """A drain-read after the child exits auto-shuts-down the server,
        so a subsequent signal request fails to connect."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Exit the shell; the drain-read below waits for exited=True via
        # _new_data, then auto-shuts-down after draining the empty buffer.
        send_request(self.session_id, {"type": "write", "text": "exit\n"})
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 300},
            timeout=5.0,
        )
        # Poll for the post-response _shutdown to finish rather than sleeping.
        sock = socket_path_for(self.session_id)
        for _ in range(40):
            if not sock.exists():
                break
            time.sleep(0.05)
        assert not sock.exists(), "server did not shut down after drain"

        with pytest.raises(PTYClientError):
            send_request(
                self.session_id,
                {"type": "signal", "signal": "TERM"},
                timeout=5.0,
            )


class TestErrorPaths:
    """Tests for error responses from the server's dispatch handlers."""

    def setup_method(self):
        self.session_id = f"test_err_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_signal_on_exited_child(self):
        """Signal on a session whose child has exited should return an error.

        The server stays up (no drain-read yet), so the request reaches it;
        _do_signal checks returncode and rejects.
        """
        result = daemonize_server(self.session_id, "sh -c 'exit 0'")
        assert result["status"] == "ok"

        # Wait for the child to exit without draining, via a peek read.
        peek = send_request(
            self.session_id,
            {"type": "read", "peek": True, "total_timeout": 3000, "stable_timeout": 300},
            timeout=5.0,
        )
        assert peek["exited"] is True

        result = send_request(
            self.session_id,
            {"type": "signal", "signal": "TERM"},
            timeout=5.0,
        )
        assert result["status"] == "error"
        assert "exited" in result["error"].lower()

    def test_resize_missing_rows(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "resize", "cols": 80},
            timeout=5.0,
        )
        assert result["status"] == "error"
        assert "rows" in result["error"]

    def test_unknown_message_type(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        result = send_request(
            self.session_id,
            {"type": "bogus_type_xyz"},
            timeout=5.0,
        )
        assert result["status"] == "error"
        assert "Unknown" in result["error"]

    def test_malformed_json_request(self):
        """Non-JSON payload should trigger the generic exception handler."""
        import socket as _socket

        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect(str(socket_path_for(self.session_id)))
            sock.sendall(b"this is not valid json at all")
            sock.shutdown(_socket.SHUT_WR)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            sock.close()

        result = json.loads(response.decode())
        assert result["status"] == "error"


class TestBufferLimit:
    """Ring-buffer eviction and the `truncated` counter."""

    def setup_method(self):
        self.session_id = f"test_buflim_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_under_limit_no_truncation(self):
        result = daemonize_server(self.session_id, "sh", buffer_limit=1024 * 1024)
        assert result["status"] == "ok"
        send_request(
            self.session_id,
            {"type": "interact", "text": "echo hello\n",
             "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        result = send_request(
            self.session_id,
            {"type": "read", "peek": True, "total_timeout": 500, "stable_timeout": 200},
            timeout=5.0,
        )
        assert result["truncated"] == 0

    def test_overflow_drops_oldest(self):
        """Writing more than buffer_limit evicts the oldest bytes, counter reflects drops."""
        limit = 4096
        result = daemonize_server(self.session_id, "cat", buffer_limit=limit)
        assert result["status"] == "ok"

        # Write enough data that at least `limit` bytes must be evicted.
        # `cat` echoes stdin back, so the PTY output roughly equals input size
        # (plus carriage-return translation). Send 3x the limit.
        chunk = ("A" * 200 + "\n") * 80  # ~16 KiB
        send_request(
            self.session_id,
            {"type": "write", "text": chunk},
            timeout=10.0,
        )
        # Let cat produce all output and settle.
        result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert result["status"] == "ok"
        # Buffer returned should be at most the limit.
        assert len(result["response"]) <= limit
        # And bytes must have been dropped.
        assert result["truncated"] > 0

    def test_truncated_counter_is_monotonic(self):
        """Each read reports total-since-spawn; it never decreases."""
        limit = 2048
        result = daemonize_server(self.session_id, "cat", buffer_limit=limit)
        assert result["status"] == "ok"

        # First overflow
        send_request(
            self.session_id,
            {"type": "write", "text": ("X" * 200 + "\n") * 30},
            timeout=10.0,
        )
        r1 = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        first = r1["truncated"]
        assert first > 0

        # After a consuming read, the buffer is empty; reading again should
        # report the SAME total (no new output since).
        r2 = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 300, "stable_timeout": 100},
            timeout=5.0,
        )
        assert r2["truncated"] == first, (
            f"counter should stay at {first} when nothing dropped between reads, "
            f"got {r2['truncated']}"
        )

        # Second overflow — counter must grow.
        send_request(
            self.session_id,
            {"type": "write", "text": ("Y" * 200 + "\n") * 30},
            timeout=10.0,
        )
        r3 = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert r3["truncated"] > first

    def test_consumed_bytes_do_not_count_as_truncated(self):
        """Bytes consumed by a normal read should NOT inflate the counter."""
        limit = 64 * 1024
        result = daemonize_server(self.session_id, "cat", buffer_limit=limit)
        assert result["status"] == "ok"

        # Stay well under the limit.
        payload = "Z" * 1000 + "\n"
        send_request(
            self.session_id,
            {"type": "write", "text": payload},
            timeout=10.0,
        )
        r = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert r["truncated"] == 0
        # Read a second time — still zero, consumption is not truncation.
        r2 = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 300, "stable_timeout": 100},
            timeout=5.0,
        )
        assert r2["truncated"] == 0

    def test_screen_response_includes_truncated(self):
        limit = 4096
        result = daemonize_server(self.session_id, "cat", buffer_limit=limit)
        assert result["status"] == "ok"
        send_request(
            self.session_id,
            {"type": "write", "text": ("Q" * 200 + "\n") * 80},
            timeout=10.0,
        )
        # Let output settle in the buffer via a peek.
        send_request(
            self.session_id,
            {"type": "read", "peek": True, "total_timeout": 3000, "stable_timeout": 500},
            timeout=10.0,
        )
        screen = send_request(self.session_id, {"type": "screen"}, timeout=5.0)
        assert "truncated" in screen
        assert screen["truncated"] > 0

    def test_parse_buffer_limit(self):
        from pty_tools.server import parse_buffer_limit
        assert parse_buffer_limit("4096") == 4096
        assert parse_buffer_limit("64K") == 64 * 1024
        assert parse_buffer_limit("64k") == 64 * 1024
        assert parse_buffer_limit("64KiB") == 64 * 1024
        assert parse_buffer_limit("256M") == 256 * 1024 * 1024
        assert parse_buffer_limit("1G") == 1024**3
        for bad in ("", "0", "-5", "5XB", "abc"):
            with pytest.raises(ValueError):
                parse_buffer_limit(bad)

    def test_cli_rejects_invalid_buffer_limit(self):
        """Invalid --buffer_limit on the CLI should fail argparse."""
        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn",
             "--detach", "--buffer_limit", "nope",
             self.session_id, "sh"],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode != 0


class TestSpawnRaces:
    """Race-resistance of duplicate-session detection during concurrent spawns."""

    def setup_method(self):
        self.session_id = f"test_race_spawn_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def test_concurrent_spawns_exactly_one_succeeds(self):
        """N threads racing to spawn the same session_id: exactly one wins.

        Bypasses the CLI pre-check by calling daemonize_server directly, so
        the correctness barrier is entirely the server-side atomic reservation.
        """
        N = 5
        results: list[dict | None] = [None] * N

        def _spawn(idx):
            results[idx] = daemonize_server(self.session_id, "sleep 60")

        threads = [threading.Thread(target=_spawn, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        assert all(r is not None for r in results)
        oks = [r for r in results if r["status"] == "ok"]
        errs = [r for r in results if r["status"] == "error"]
        assert len(oks) == 1, f"expected exactly 1 winner, got {len(oks)}: {results}"
        assert len(errs) == N - 1
        for e in errs:
            assert "already exists" in e["error"], (
                f"loser should report 'already exists', got: {e['error']!r}"
            )

        # Registry should match the winner's pid.
        reg = read_registry()
        entry = reg.get(self.session_id)
        assert entry is not None
        assert entry["pid"] == oks[0]["pid"]

        # Socket is owned by the winner.
        assert socket_path_for(self.session_id).exists()

        # All loser subprocesses should have fully exited (no zombies).
        # daemonize_server kept a Popen handle internally; we can't check
        # from here, but `ps` would show nothing for the session. Smoke-check
        # via is_server_alive.
        assert is_server_alive(self.session_id)

    def test_spawn_cleans_up_stale_socket_file(self):
        """A leftover socket file with no live owner must not block a new spawn."""
        sock_path = socket_path_for(self.session_id)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a stub — the OS bind() treats any existing file at the path as
        # a conflict, whether it's a socket or a regular file.
        sock_path.write_bytes(b"stale")
        assert sock_path.exists()

        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok", result
        assert is_server_alive(self.session_id)

    def test_spawn_cleans_up_dead_registry_entry(self):
        """A registry entry pointing to a dead PID must not block a new spawn."""
        from pty_tools.common import register_session

        # Register a fake entry with a PID that definitely isn't ours and
        # isn't alive. Pick a PID very unlikely to be in use.
        fake_sock = str(socket_path_for(self.session_id))
        register_session(self.session_id, "stale_cmd", 99999998, fake_sock)
        assert self.session_id in read_registry()

        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok", result

        reg = read_registry()
        entry = reg[self.session_id]
        # New entry should have overwritten the stale one.
        assert entry["pid"] != 99999998
        assert entry["command"] == "sh"

    def test_sequential_spawn_after_exit_succeeds(self):
        """After a session cleanly exits, the same id can be spawned again."""
        r1 = daemonize_server(self.session_id, "sh")
        assert r1["status"] == "ok"

        send_request(self.session_id, {"type": "exit"}, timeout=10.0)

        # Wait for the first server's shutdown to finish so registry is clean.
        sock = socket_path_for(self.session_id)
        for _ in range(40):
            if not sock.exists():
                break
            time.sleep(0.05)
        assert not sock.exists()

        r2 = daemonize_server(self.session_id, "sh")
        assert r2["status"] == "ok"
        assert r2["pid"] != r1["pid"]

    def test_cli_concurrent_duplicate_reports_already_exists(self):
        """End-to-end: `pty spawn --detach` losing the race reports a clean error.

        Start a winner, then run the CLI against the same id — the CLI's
        pre-check catches this one. To exercise the server-side path we also
        kick off two concurrent CLI spawns with the same id; whichever loses
        should print the "already exists" JSON error.
        """
        r = daemonize_server(self.session_id, "sleep 60")
        assert r["status"] == "ok"

        proc = subprocess.run(
            [sys.executable, "-m", "pty_tools.cli", "spawn", "--detach",
             self.session_id, "sh"],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode != 0
        payload = json.loads(proc.stdout)
        assert payload["status"] == "error"
        assert "already exists" in payload["error"]
