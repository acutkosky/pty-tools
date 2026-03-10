"""pty_interact — atomic write-then-read for a PTY session."""

import argparse
import json
import sys

from pty_tools.client import PTYClientError, send_request


def main():
    parser = argparse.ArgumentParser(description="Write input and read output atomically")
    parser.add_argument("id", help="Session identifier")
    parser.add_argument("--input", dest="input_text", required=True, help="Text to send")
    parser.add_argument("--total_timeout", type=int, default=5000, help="Total timeout in ms (default: 5000)")
    parser.add_argument("--stable_timeout", type=int, default=500, help="Stable timeout in ms (default: 500)")
    parser.add_argument("--pattern", help="Regex pattern to wait for")
    parser.add_argument("--no_strip_ansi", action="store_true", help="Preserve ANSI escape sequences in output")
    args = parser.parse_args()

    # Process escape sequences like \n, \t, etc.
    text = args.input_text.encode("utf-8").decode("unicode_escape")

    msg = {
        "type": "interact",
        "text": text,
        "total_timeout": args.total_timeout,
        "stable_timeout": args.stable_timeout,
        "strip_ansi": not args.no_strip_ansi,
    }
    if args.pattern:
        msg["pattern"] = args.pattern

    timeout = (args.total_timeout / 1000.0) + 5.0

    try:
        result = send_request(args.id, msg, timeout=timeout)
        print(json.dumps(result))
    except PTYClientError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
