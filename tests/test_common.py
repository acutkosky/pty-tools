"""Tests for common.py — registry read/write with locking."""

import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from pty_tools.common import (
    read_registry,
    register_session,
    unregister_session,
)


class TestRegistry:
    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher_dir = mock.patch("pty_tools.common.SOCKET_DIR", Path(self._tmpdir.name))
        self._patcher_reg = mock.patch("pty_tools.common.REGISTRY_PATH", Path(self._tmpdir.name) / "registry.json")
        self._patcher_dir.start()
        self._patcher_reg.start()

    def teardown_method(self):
        self._patcher_dir.stop()
        self._patcher_reg.stop()
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
