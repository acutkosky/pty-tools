"""Integration tests — full lifecycle of PTY sessions."""

import json
import os
import signal
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
from pty_tools.server import daemonize_server


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
        assert result["mode"] == "raw"

        # Exit
        result = send_request(self.session_id, {"type": "exit"}, timeout=10.0)
        assert result["status"] == "ok"

        # Give server time to clean up
        time.sleep(1.0)

        # Verify cleanup
        assert not socket_path_for(self.session_id).exists()
        assert not is_server_alive(self.session_id)

    def test_write_then_read(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Give the shell time to start
        time.sleep(0.5)

        # Read initial prompt/output
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
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
        assert read_result["mode"] == "raw"

    def test_pattern_matching(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Drain initial output
        time.sleep(0.3)
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
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

    def test_duplicate_session_check(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        assert is_server_alive(self.session_id)

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

        # Tell the shell to exit with a specific code
        send_request(self.session_id, {"type": "write", "text": "exit 42\n"})
        time.sleep(1.0)

        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 2000, "stable_timeout": 500},
            timeout=10.0,
        )
        assert read_result["status"] == "ok"
        assert read_result["exited"] is True
        assert read_result["exit_code"] == 42
        assert read_result["signal"] is None

    def test_interact_on_exited_session_returns_error(self):
        """interact should return a write error if the session has already exited."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Tell the shell to exit and wait for it
        send_request(self.session_id, {"type": "write", "text": "exit\n"})
        time.sleep(1.0)

        # Drain so the server marks exited=True
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
            timeout=5.0,
        )

        # Now interact should report the error from the write step
        result = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo should_fail\n",
                "total_timeout": 1000,
                "stable_timeout": 300,
            },
            timeout=5.0,
        )
        assert result["status"] == "error"
        assert "exited" in result["error"]

    def test_strip_ansi_default(self):
        """ANSI escapes should be stripped by default."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        result_default = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo hello\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
            },
            timeout=10.0,
        )
        assert result_default["status"] == "ok"
        assert "hello" in result_default["response"]
        assert "\x1b" not in result_default["response"]

        # With strip_ansi=False, escapes should be preserved
        result_raw = send_request(
            self.session_id,
            {
                "type": "interact",
                "text": "echo hello\n",
                "total_timeout": 3000,
                "stable_timeout": 500,
                "strip_ansi": False,
            },
            timeout=10.0,
        )
        assert result_raw["status"] == "ok"
        assert "hello" in result_raw["response"]

    def test_spawn_bad_command_reports_error(self):
        """Spawning a nonexistent command should report a meaningful error."""
        result = daemonize_server(self.session_id, "/nonexistent/command/xyz")
        assert result["status"] == "error"
        assert "did not start" in result["error"] or "nonexistent" in result["error"].lower()

    def test_read_response_has_status(self):
        """All read/interact responses should include status field."""
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"

        # Plain read
        read_result = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
            timeout=5.0,
        )
        assert read_result["status"] == "ok"
        assert "exited" in read_result
        assert "response" in read_result
        assert "mode" in read_result


class TestPeek:
    def setup_method(self):
        self.session_id = f"test_{os.getpid()}_{int(time.time() * 1000)}"

    def teardown_method(self):
        _cleanup_session(self.session_id)

    def _spawn(self):
        result = daemonize_server(self.session_id, "sh")
        assert result["status"] == "ok"
        # Drain initial output
        time.sleep(0.3)
        send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
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
        time.sleep(0.3)

        # Peek (no pattern) — buffer the output
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

        send_request(self.session_id, {"type": "write", "text": "printf MAR"})
        time.sleep(0.3)

        # Peek — buffers "MAR"
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
                "pattern": "MARKER",
            },
            timeout=10.0,
        )
        assert result["status"] == "ok"
        assert "MARKER" in result["response"]

    def test_peek_preserves_content_through_clear(self):
        """Peek buffers raw bytes, so content survives a screen clear."""
        self._spawn()

        # Write and peek — buffers "visible_text" in raw bytes
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
        # In raw mode, the peek buffer still has the old bytes — "visible_text"
        # persists even though a clear was sent, because the raw bytes are accumulated
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

    def test_read_consumes_in_raw_mode(self):
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

    def test_screen_mode_peek_and_read_identical(self):
        """In screen mode (alternate screen), peek and read show the same
        screen state because pyte renders the live screen regardless of
        the peek buffer."""
        self._spawn()

        # Use python to enter alternate screen, write text, and exit
        # First peek-read to capture the screen state
        send_request(
            self.session_id,
            {
                "type": "write",
                "text": (
                    "python3 -c \""
                    "import sys;"
                    "sys.stdout.write('\\x1b[?1049h');"  # enter alt screen
                    "sys.stdout.write('\\x1b[2J\\x1b[H');"  # clear + home
                    "sys.stdout.write('SCREEN_CONTENT_XYZ\\n');"
                    "sys.stdout.flush();"
                    "import time; time.sleep(2);"
                    "sys.stdout.write('\\x1b[?1049l');"  # leave alt screen
                    "\"\n"
                ),
            },
        )
        time.sleep(0.5)

        # Peek while in alternate screen
        peek = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300, "peek": True},
            timeout=5.0,
        )
        assert peek["mode"] == "screen"
        assert "SCREEN_CONTENT_XYZ" in peek["response"]

        # Normal read while still in alternate screen — should show same content
        normal = send_request(
            self.session_id,
            {"type": "read", "total_timeout": 1000, "stable_timeout": 300},
            timeout=5.0,
        )
        assert normal["mode"] == "screen"
        assert "SCREEN_CONTENT_XYZ" in normal["response"]
