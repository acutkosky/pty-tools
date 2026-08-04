"""Tests for common.py — registry read/write with locking."""

import os
import tempfile
import threading
from pathlib import Path

import pytest

from pty_tools.common import (
    PTYClientError,
    atomic_reserve_session,
    cleanup_session_if_owner,
    is_session_ready,
    mark_session_ready,
    read_registry,
    register_session,
    send_request,
    unregister_session,
)


class TestRegistry:
    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_env = os.environ.get("PTY_SOCKET_DIR")
        os.environ["PTY_SOCKET_DIR"] = self._tmpdir.name

    def teardown_method(self):
        if self._prev_env is None:
            os.environ.pop("PTY_SOCKET_DIR", None)
        else:
            os.environ["PTY_SOCKET_DIR"] = self._prev_env
        self._tmpdir.cleanup()

    def test_register_and_read(self):
        register_session("s1", "bash", 12345, "/tmp/test.sock")
        reg = read_registry()
        assert "s1" in reg
        assert reg["s1"]["command"] == "bash"
        assert reg["s1"]["pid"] == 12345
        assert reg["s1"]["state"] == "ready"
        assert is_session_ready("s1")

    def test_unregister(self):
        register_session("s1", "bash", 12345, "/tmp/test.sock")
        unregister_session("s1")
        reg = read_registry()
        assert "s1" not in reg

    def test_concurrent_writes(self):
        """Multiple threads registering sessions should not corrupt the file."""
        errors = []

        def register_n(n):
            try:
                register_session(f"session_{n}", f"cmd_{n}", 1000 + n, f"/tmp/s_{n}.sock")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_n, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        reg = read_registry()
        assert len(reg) == 10

    def test_reservation_transitions_from_starting_to_ready(self):
        sock_path = str(Path(self._tmpdir.name) / "session_s1.sock")
        owner_pid = os.getpid()

        assert atomic_reserve_session("s1", "bash", owner_pid, sock_path)
        assert read_registry()["s1"]["state"] == "starting"
        assert not is_session_ready("s1")
        with pytest.raises(PTYClientError, match="still starting"):
            send_request("s1", {"type": "screen"})

        assert not mark_session_ready("s1", owner_pid + 1)
        assert read_registry()["s1"]["state"] == "starting"

        assert mark_session_ready("s1", owner_pid)
        assert read_registry()["s1"]["state"] == "ready"
        assert is_session_ready("s1")

    def test_owner_checked_cleanup_cannot_remove_replacement(self):
        sock_path = Path(self._tmpdir.name) / "session_s1.sock"
        owner_pid = os.getpid()
        assert atomic_reserve_session("s1", "bash", owner_pid, str(sock_path))
        sock_path.write_text("placeholder")

        assert not cleanup_session_if_owner("s1", owner_pid + 1)
        assert "s1" in read_registry()
        assert sock_path.exists()

        assert cleanup_session_if_owner("s1", owner_pid)
        assert "s1" not in read_registry()
        assert not sock_path.exists()
