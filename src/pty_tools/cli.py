"""Unified CLI for pty-tools."""

import argparse
import json
import os
import signal
import sys

from pty_tools.common import (
    PTYClientError,
    is_server_alive,
    read_registry,
    send_request,
    socket_path_for,
    unregister_session,
)
from pty_tools.server import daemonize_server, run_server


def cmd_spawn(args):
    if is_server_alive(args.id):
        result = {"status": "error", "error": f"Session '{args.id}' already exists"}
        print(json.dumps(result))
        sys.exit(1)

    if args.detach:
        result = daemonize_server(args.id, args.cmd, rows=args.rows, cols=args.cols)
        print(json.dumps(result))
        if result["status"] != "ok":
            sys.exit(1)
    else:
        status = {
            "status": "ok",
            "session_id": args.id,
            "command": args.cmd,
            "pid": os.getpid(),
        }
        print(json.dumps(status), file=sys.stderr)
        run_server(args.id, args.cmd, rows=args.rows, cols=args.cols, foreground=True)


def cmd_write(args):
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


def _build_read_msg(args, msg_type):
    """Build the message dict common to read and interact."""
    msg = {
        "type": msg_type,
        "total_timeout": args.total_timeout,
        "stable_timeout": args.stable_timeout,
        "strip_ansi": not args.no_strip_ansi,
        "peek": args.peek,
    }
    if args.pattern:
        msg["pattern"] = args.pattern
    return msg


def _send_read(session_id, msg):
    """Send a read/interact message and print the result."""
    timeout = (msg["total_timeout"] / 1000.0) + 5.0
    try:
        result = send_request(session_id, msg, timeout=timeout)
        print(json.dumps(result))
    except PTYClientError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_read(args):
    if args.screen:
        try:
            result = send_request(args.id, {"type": "screen"}, timeout=5.0)
            print(json.dumps(result))
        except PTYClientError as e:
            print(json.dumps({"status": "error", "error": str(e)}))
            sys.exit(1)
        return
    _send_read(args.id, _build_read_msg(args, "read"))


def cmd_interact(args):
    msg = _build_read_msg(args, "interact")
    msg["text"] = args.input_text.encode("utf-8").decode("unicode_escape")
    _send_read(args.id, msg)


def cmd_resize(args):
    try:
        result = send_request(
            args.id,
            {"type": "resize", "rows": args.rows, "cols": args.cols},
            timeout=5.0,
        )
        print(json.dumps(result))
        if result.get("status") != "ok":
            sys.exit(1)
    except PTYClientError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_signal(args):
    try:
        result = send_request(
            args.id,
            {"type": "signal", "signal": args.signal},
            timeout=5.0,
        )
        print(json.dumps(result))
        if result.get("status") != "ok":
            sys.exit(1)
    except PTYClientError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_tap(args):
    try:
        result = send_request(args.out_id, {"type": "tap", "target": args.in_id})
        print(json.dumps(result))
        if result.get("status") != "ok":
            sys.exit(1)
    except PTYClientError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_untap(args):
    try:
        result = send_request(args.out_id, {"type": "untap", "target": args.in_id})
        print(json.dumps(result))
        if result.get("status") != "ok":
            sys.exit(1)
    except PTYClientError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_list(args):
    registry = read_registry()
    active = []
    for session_id, entry in list(registry.items()):
        if is_server_alive(session_id):
            active.append({"session_id": session_id, **entry})
    print(json.dumps(active, indent=2))


def cmd_exit(args):
    try:
        result = send_request(args.id, {"type": "exit"}, timeout=10.0)
        print(json.dumps(result))
    except Exception:
        _force_cleanup(args.id)


def _force_cleanup(session_id: str):
    registry = read_registry()
    entry = registry.get(session_id)

    if entry:
        try:
            os.kill(entry["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    sock = socket_path_for(session_id)
    if sock.exists():
        sock.unlink()

    unregister_session(session_id)
    print(json.dumps({"status": "ok", "message": f"Force-cleaned session '{session_id}'"}))


def _add_read_args(parser):
    """Add common read/interact arguments."""
    parser.add_argument("--total_timeout", type=int, default=5000, help="Total timeout in ms (default: 5000)")
    parser.add_argument("--stable_timeout", type=int, default=500, help="Stable timeout in ms (default: 500)")
    parser.add_argument("--pattern", help="Regex pattern to wait for")
    parser.add_argument("--no_strip_ansi", action="store_true", help="Preserve ANSI escape sequences")
    parser.add_argument("--peek", action="store_true", help="Read without consuming output")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pty", description="PTY session management")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # spawn
    p = sub.add_parser("spawn", help="Spawn a new PTY session",
                        description="Spawn a process in a new PTY session. Runs in foreground by default (stdin forwarded, PTY output streamed to stdout). Use --detach to daemonize.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("cmd", help="Command to run in the PTY")
    p.add_argument("--rows", type=int, default=24, help="Terminal rows (default: 24)")
    p.add_argument("--cols", type=int, default=80, help="Terminal columns (default: 80)")
    p.add_argument("--detach", action="store_true",
                   help="Daemonize the server (default: run in foreground)")
    p.set_defaults(func=cmd_spawn)

    # write
    p = sub.add_parser("write", help="Send input to PTY session(s)",
                        description="Send input to one or more PTY sessions. Reads from stdin by default, or use --input for a literal string. Use --stream to send stdin line by line.")
    p.add_argument("id", nargs="+", help="Session identifier(s)")
    p.add_argument("--input", dest="input_text", help="Text to send (default: read from stdin)")
    p.add_argument("--stream", action="store_true", help="Send stdin line by line")
    p.set_defaults(func=cmd_write)

    # read
    p = sub.add_parser("read", help="Read output from a PTY session",
                        description="Read output from a PTY session. Consumes the read buffer by default (use --peek to preserve it). Use --screen for a virtual terminal snapshot instead of the raw buffer.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("--screen", action="store_true", help="Return virtual terminal screen snapshot")
    _add_read_args(p)
    p.set_defaults(func=cmd_read)

    # interact
    p = sub.add_parser("interact", help="Atomic write-then-read",
                        description="Atomic write-then-read. Sends input and reads the response in a single operation, avoiding race conditions between separate write and read calls.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("--input", dest="input_text", required=True, help="Text to send")
    _add_read_args(p)
    p.set_defaults(func=cmd_interact)

    # resize
    p = sub.add_parser("resize", help="Resize a PTY session",
                        description="Resize the PTY terminal. Updates the terminal size, sends SIGWINCH to the child process group, and resizes the virtual terminal screen.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("--rows", type=int, required=True, help="New row count")
    p.add_argument("--cols", type=int, required=True, help="New column count")
    p.set_defaults(func=cmd_resize)

    # signal
    p = sub.add_parser("signal", help="Send a signal to a PTY session",
                        description="Send a signal to the child process group. Accepts signal names (SIGTERM, TERM) or numbers (15).")
    p.add_argument("id", help="Session identifier")
    p.add_argument("signal", help="Signal name (e.g. SIGTERM, TERM) or number")
    p.set_defaults(func=cmd_signal)

    # tap
    p = sub.add_parser("tap", help="Forward output of one session to input of another",
                        description="Forward all output from one session to the stdin of another. Multiple taps from the same source are supported. Auto-removed if the target exits.")
    p.add_argument("out_id", help="Source session (whose output to forward)")
    p.add_argument("in_id", help="Target session (whose stdin receives the output)")
    p.set_defaults(func=cmd_tap)

    # untap
    p = sub.add_parser("untap", help="Remove a tap",
                        description="Remove a previously established tap. Untapping a target that was never tapped is a no-op.")
    p.add_argument("out_id", help="Source session")
    p.add_argument("in_id", help="Target session")
    p.set_defaults(func=cmd_untap)

    # list
    p = sub.add_parser("list", help="List active sessions",
                        description="List active sessions as a JSON array. Stale entries (dead server processes) are cleaned up automatically.")
    p.set_defaults(func=cmd_list)

    # exit
    p = sub.add_parser("exit", help="Terminate a session",
                        description="Terminate a session. If the server is unresponsive, force-kills the process and cleans up the socket and registry.")
    p.add_argument("id", help="Session identifier")
    p.set_defaults(func=cmd_exit)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
