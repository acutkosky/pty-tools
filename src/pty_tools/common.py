"""Shared infrastructure: socket paths, registry with flock, client."""

import asyncio
import fcntl
import json
import os
import socket
import time
from pathlib import Path

DEFAULT_SOCKET_DIR = "/tmp/pty_sessions"


def get_socket_dir() -> Path:
    """Resolve the socket directory from $PTY_SOCKET_DIR each call."""
    return Path(os.environ.get("PTY_SOCKET_DIR", DEFAULT_SOCKET_DIR))


def get_registry_path() -> Path:
    return get_socket_dir() / "registry.json"


def ensure_socket_dir():
    get_socket_dir().mkdir(parents=True, exist_ok=True)


def socket_path_for(session_id: str) -> Path:
    return get_socket_dir() / f"session_{session_id}.sock"


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
    registry_path = get_registry_path()
    if not registry_path.exists():
        return {}
    with open(registry_path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            return _read_registry_unlocked(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update_registry(func):
    """Call func(registry_dict) under an exclusive flock, then write back."""
    ensure_socket_dir()
    with open(get_registry_path(), "a+") as f:
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


def atomic_reserve_session(session_id: str, command: str, pid: int, socket_path: str) -> bool:
    """Under a single registry flock, claim the session_id for this process.

    Returns True if the slot was free (or stale) and we've registered ourselves.
    Returns False if a live process already owns it — caller must bail out.

    This is the only correctness primitive for duplicate-session protection.
    Callers racing on the same session_id serialize on the registry flock; at
    most one sees a free slot and wins. Stale registry entries (dead PID) and
    orphaned socket files from the previous owner are cleaned up here.
    """
    outcome = {"reserved": False}

    def _reserve(reg):
        entry = reg.get(session_id)
        if entry is not None:
            other_pid = entry["pid"]
            alive = True
            try:
                os.kill(other_pid, 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                # Can't signal it, but it exists — assume alive (safer default).
                alive = True
            if alive:
                return  # slot is taken; outcome stays False
            # Dead owner — unlink its socket file, fall through to register.
            old_sock = Path(entry["socket_path"])
            if old_sock.exists():
                old_sock.unlink()
        # Remove any lingering socket file at our path too (stale from a crash
        # that left a socket without a registry entry).
        sp = Path(socket_path)
        if sp.exists():
            sp.unlink()
        reg[session_id] = {
            "command": command,
            "pid": pid,
            "socket_path": socket_path,
            "created_at": time.time(),
        }
        outcome["reserved"] = True

    update_registry(_reserve)
    return outcome["reserved"]


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


# ── Client (synchronous socket communication) ────────────────────────


class PTYClientError(Exception):
    pass


def send_request(session_id: str, message: dict, timeout: float = 30.0) -> dict:
    """Send a request to a PTY server and receive its response."""
    sock_path = str(socket_path_for(session_id))

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect(sock_path)
    except (ConnectionRefusedError, FileNotFoundError):
        if not is_server_alive(session_id):
            raise PTYClientError(
                f"Session '{session_id}' is not running. It may have exited. "
                f"Use pty list to see active sessions."
            )
        raise PTYClientError(
            f"Cannot connect to session '{session_id}' — connection refused."
        )

    try:
        sock.sendall(json.dumps(message).encode())
        sock.shutdown(socket.SHUT_WR)

        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

        return json.loads(response.decode())
    finally:
        sock.close()


async def send_request_async(session_id: str, message: dict, timeout: float = 30.0) -> dict:
    """Async version of send_request — for use inside the asyncio server."""
    sock_path = str(socket_path_for(session_id))
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(sock_path), timeout=timeout
        )
    except (ConnectionRefusedError, FileNotFoundError):
        if not is_server_alive(session_id):
            raise PTYClientError(
                f"Session '{session_id}' is not running. It may have exited. "
                f"Use pty list to see active sessions."
            )
        raise PTYClientError(
            f"Cannot connect to session '{session_id}' — connection refused."
        )
    try:
        writer.write(json.dumps(message).encode())
        if writer.can_write_eof():
            writer.write_eof()
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=timeout)
        return json.loads(response.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
