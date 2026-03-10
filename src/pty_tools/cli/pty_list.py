"""pty_list — list active PTY sessions."""

import json

from pty_tools.common import is_server_alive, read_registry


def main():
    registry = read_registry()
    active = []
    for session_id, entry in list(registry.items()):
        if is_server_alive(session_id):
            active.append({
                "session_id": session_id,
                **entry,
            })
    print(json.dumps(active, indent=2))


if __name__ == "__main__":
    main()
