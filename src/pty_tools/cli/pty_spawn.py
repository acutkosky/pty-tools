"""pty_spawn — spawn a new PTY session."""

import argparse
import json
import sys

from pty_tools.common import is_server_alive
from pty_tools.server import daemonize_server


def main():
    parser = argparse.ArgumentParser(description="Spawn a new PTY session")
    parser.add_argument("id", help="Session identifier")
    parser.add_argument("command", help="Command to run in the PTY")
    parser.add_argument("--cols", type=int, default=80, help="Terminal columns (default: 80)")
    parser.add_argument("--rows", type=int, default=24, help="Terminal rows (default: 24)")
    args = parser.parse_args()

    if is_server_alive(args.id):
        result = {"status": "error", "error": f"Session '{args.id}' already exists"}
        print(json.dumps(result))
        sys.exit(1)

    result = daemonize_server(args.id, args.command, rows=args.rows, cols=args.cols)
    print(json.dumps(result))
    if result["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
