"""pty_exit — terminate a PTY session."""

import argparse
import json
import os
import signal
import sys

from pty_tools.client import send_request
from pty_tools.common import read_registry, socket_path_for, unregister_session


def main():
    parser = argparse.ArgumentParser(description="Exit a PTY session")
    parser.add_argument("id", help="Session identifier")
    args = parser.parse_args()

    try:
        result = send_request(args.id, {"type": "exit"}, timeout=10.0)
        print(json.dumps(result))
    except Exception:
        _force_cleanup(args.id)


def _force_cleanup(session_id: str):
    """Force-kill the server process and clean up resources."""
    registry = read_registry()
    entry = registry.get(session_id)

    if entry:
        pid = entry["pid"]
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    sock = socket_path_for(session_id)
    if sock.exists():
        sock.unlink()

    unregister_session(session_id)
    print(json.dumps({"status": "ok", "message": f"Force-cleaned session '{session_id}'"}))


if __name__ == "__main__":
    main()
