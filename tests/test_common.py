"""Tests for common.py — registry read/write with locking."""

import os
import tempfile
import threading

import pytest

from pty_tools.common import (
    read_registry,
    register_session,
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
