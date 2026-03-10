"""pty_write — send input to one or more PTY sessions."""

import argparse
import json
import sys

from pty_tools.client import PTYClientError, send_request


def main():
    parser = argparse.ArgumentParser(description="Write text to PTY session(s)")
    parser.add_argument("id", nargs="+", help="Session identifier(s)")
    parser.add_argument("--input", dest="input_text", help="Text to send (default: read from stdin)")
    parser.add_argument("--stream", action="store_true", help="Send stdin line by line")
    args = parser.parse_args()

    if args.input_text is not None:
        text = args.input_text.encode("utf-8").decode("unicode_escape")
        _send_to_all(args.id, text)
    elif args.stream:
        for line in sys.stdin:
            _send_to_all(args.id, line)
    else:
        text = sys.stdin.read()
        _send_to_all(args.id, text)


def _send_to_all(session_ids: list[str], text: str):
    for sid in session_ids:
        try:
            result = send_request(sid, {"type": "write", "text": text})
            print(json.dumps({"session_id": sid, **result}))
        except PTYClientError as e:
            print(json.dumps({"session_id": sid, "status": "error", "error": str(e)}))
            sys.exit(1)


if __name__ == "__main__":
    main()
