"""Shared infrastructure: socket paths, registry with flock."""

import fcntl
import json
import os
import time
from pathlib import Path

SOCKET_DIR = Path("/tmp/pty_sessions")
REGISTRY_PATH = SOCKET_DIR / "registry.json"


def ensure_socket_dir():
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)


def socket_path_for(session_id: str) -> Path:
    return SOCKET_DIR / f"session_{session_id}.sock"


# ── Registry (flock-protected JSON file) ──────────────────────────────


def _read_registry_unlocked(f) -> dict:
    f.seek(0)
    data = f.read()
    if not data:
        return {}
    return json.loads(data)


def _write_registry_unlocked(f, registry: dict):
    f.seek(0)
    f.truncate()
    json.dump(registry, f, indent=2)
    f.flush()


def read_registry() -> dict:
    ensure_socket_dir()
    if not REGISTRY_PATH.exists():
        return {}
    with open(REGISTRY_PATH, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return _read_registry_unlocked(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update_registry(func):
    """Call func(registry_dict) under an exclusive flock, then write back."""
    ensure_socket_dir()
    with open(REGISTRY_PATH, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            registry = _read_registry_unlocked(f)
            func(registry)
            _write_registry_unlocked(f, registry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def register_session(session_id: str, command: str, pid: int, socket_path: str):
    def _add(reg):
        reg[session_id] = {
            "command": command,
            "pid": pid,
            "socket_path": socket_path,
            "created_at": time.time(),
        }
    update_registry(_add)


def unregister_session(session_id: str):
    def _remove(reg):
        reg.pop(session_id, None)
    update_registry(_remove)


def is_server_alive(session_id: str) -> bool:
    registry = read_registry()
    entry = registry.get(session_id)
    if entry is None:
        return False
    pid = entry["pid"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        sock = Path(entry["socket_path"])
        if sock.exists():
            sock.unlink()
        unregister_session(session_id)
        return False
    except PermissionError:
        return True
    return True
