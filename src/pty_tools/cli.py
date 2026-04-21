"""Unified CLI for pty-tools."""

import argparse
import json
import os
import shlex
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
from pty_tools.server import DEFAULT_BUFFER_LIMIT, daemonize_server, parse_buffer_limit, run_server


def cmd_spawn(args):
    # `pty spawn <id> <cmd...>` accepts either a single shlex-quoted string
    # ('ls -la') or argv-style words (ls -la). Multi-word forms are re-joined
    # so the server sees a single shlex-parseable command string.
    if not args.cmd:
        print(json.dumps({"status": "error", "error": "Missing command"}))
        sys.exit(1)
    cmd_str = args.cmd[0] if len(args.cmd) == 1 else shlex.join(args.cmd)

    if is_server_alive(args.id):
        result = {"status": "error", "error": f"Session '{args.id}' already exists"}
        print(json.dumps(result))
        sys.exit(1)

    if args.detach:
        result = daemonize_server(args.id, cmd_str, rows=args.rows, cols=args.cols,
                                  time_limit=args.time_limit,
                                  buffer_limit=args.buffer_limit)
        print(json.dumps(result))
        if result["status"] != "ok":
            sys.exit(1)
    else:
        status = {
            "status": "ok",
            "session_id": args.id,
            "command": cmd_str,
            "pid": os.getpid(),
        }
        print(json.dumps(status), file=sys.stderr)
        rc = run_server(args.id, cmd_str, rows=args.rows, cols=args.cols, foreground=True,
                        time_limit=args.time_limit, buffer_limit=args.buffer_limit)
        sys.exit(rc)


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
            result = send_request(args.id, {"type": "screen"}, timeout=30.0)
            print(json.dumps(result))
        except PTYClientError as e:
            print(json.dumps({"status": "error", "error": str(e)}))
            sys.exit(1)
        return
    _send_read(args.id, _build_read_msg(args, "read"))


def cmd_interact(args):
    if args.diff and not args.screen:
        print(json.dumps({"status": "error", "error": "--diff requires --screen"}))
        sys.exit(1)
    msg = _build_read_msg(args, "interact")
    msg["text"] = args.input_text.encode("utf-8").decode("unicode_escape")
    if args.screen:
        msg["screen"] = True
    if args.diff:
        msg["diff"] = True
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
    msg = {"type": "exit"}
    if args.drain:
        msg["drain"] = True
        msg["strip_ansi"] = not args.no_strip_ansi
    try:
        result = send_request(args.id, msg, timeout=10.0)
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
    parser.add_argument("--total-timeout", type=int, default=5000, help="Total timeout in ms (default: 5000)")
    parser.add_argument("--stable-timeout", type=int, default=500, help="Stable timeout in ms (default: 500)")
    parser.add_argument("--pattern", help="Regex pattern to wait for")
    parser.add_argument("--no-strip-ansi", action="store_true", help="Preserve ANSI escape sequences")
    parser.add_argument("--peek", action="store_true", help="Read without consuming output")
    parser.add_argument("--screen", action="store_true",
                       help="Return virtual terminal screen snapshot instead of raw buffer")


def main(argv=None):
    # Shared across the main parser and every subparser so --socket-dir works
    # in either position: `pty --socket-dir X spawn ...` or
    # `pty spawn --socket-dir X ...`. SUPPRESS keeps the subparser from
    # overwriting a value set at the main parser when no flag is given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--socket-dir", dest="socket_dir",
                        default=argparse.SUPPRESS,
                        help="Directory for session sockets and registry "
                             "(default: $PTY_SOCKET_DIR or /tmp/pty_sessions)")

    parser = argparse.ArgumentParser(prog="pty", description="PTY session management",
                                      parents=[common])
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # spawn
    p = sub.add_parser("spawn", parents=[common], help="Spawn a new PTY session",
                        description="Spawn a process in a new PTY session. Runs in foreground by default (stdin forwarded, PTY output streamed to stdout). Use --detach to daemonize.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="Command to run in the PTY (accepts either a single "
                        "shlex-quoted string or space-separated argv words)")
    p.add_argument("--rows", type=int, default=24, help="Terminal rows (default: 24)")
    p.add_argument("--cols", type=int, default=80, help="Terminal columns (default: 80)")
    p.add_argument("--detach", action="store_true",
                   help="Daemonize the server (default: run in foreground)")
    p.add_argument("--time-limit", type=float, default=None,
                   help="Maximum lifetime in seconds; process is killed when exceeded")
    p.add_argument("--buffer-limit", type=parse_buffer_limit, default=DEFAULT_BUFFER_LIMIT,
                   help="Max bytes retained from child output in a ring buffer "
                        "(suffixes K/M/G, binary; e.g. 64M, 1G). Default: 256M. "
                        "Evictions are reported as 'truncated' in read/screen responses.")
    p.set_defaults(func=cmd_spawn)

    # write
    p = sub.add_parser("write", parents=[common], help="Send input to PTY session(s)",
                        description="Send input to one or more PTY sessions. Reads from stdin by default, or use --input for a literal string. Use --stream to send stdin line by line.")
    p.add_argument("id", nargs="+", help="Session identifier(s)")
    p.add_argument("--input", dest="input_text", help="Text to send (default: read from stdin)")
    p.add_argument("--stream", action="store_true", help="Send stdin line by line")
    p.set_defaults(func=cmd_write)

    # read
    p = sub.add_parser("read", parents=[common], help="Read output from a PTY session",
                        description="Read output from a PTY session. Consumes the read buffer by default (use --peek to preserve it). Use --screen for a virtual terminal snapshot instead of the raw buffer.")
    p.add_argument("id", help="Session identifier")
    _add_read_args(p)
    p.set_defaults(func=cmd_read)

    # interact
    p = sub.add_parser("interact", parents=[common], help="Atomic write-then-read",
                        description="Atomic write-then-read. Sends input and reads the response in a single operation, avoiding race conditions between separate write and read calls. Add --screen for a post-write screen snapshot, or --screen --diff for a unified diff between the pre- and post-write screen (compresses well for most TUIs).")
    p.add_argument("id", help="Session identifier")
    p.add_argument("--input", dest="input_text", required=True, help="Text to send")
    p.add_argument("--diff", action="store_true",
                   help="With --screen: return unified diff between pre-write and post-write screen")
    _add_read_args(p)
    p.set_defaults(func=cmd_interact)

    # resize
    p = sub.add_parser("resize", parents=[common], help="Resize a PTY session",
                        description="Resize the PTY terminal. Updates the terminal size, sends SIGWINCH to the child process group, and resizes the virtual terminal screen.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("--rows", type=int, required=True, help="New row count")
    p.add_argument("--cols", type=int, required=True, help="New column count")
    p.set_defaults(func=cmd_resize)

    # signal
    p = sub.add_parser("signal", parents=[common], help="Send a signal to a PTY session",
                        description="Send a signal to the child process group. Accepts signal names (SIGTERM, TERM) or numbers (15).")
    p.add_argument("id", help="Session identifier")
    p.add_argument("signal", help="Signal name (e.g. SIGTERM, TERM) or number")
    p.set_defaults(func=cmd_signal)

    # tap
    p = sub.add_parser("tap", parents=[common], help="Forward output of one session to input of another",
                        description="Forward all output from one session to the stdin of another. Multiple taps from the same source are supported. Auto-removed if the target exits.")
    p.add_argument("out_id", help="Source session (whose output to forward)")
    p.add_argument("in_id", help="Target session (whose stdin receives the output)")
    p.set_defaults(func=cmd_tap)

    # untap
    p = sub.add_parser("untap", parents=[common], help="Remove a tap",
                        description="Remove a previously established tap. Untapping a target that was never tapped is a no-op.")
    p.add_argument("out_id", help="Source session")
    p.add_argument("in_id", help="Target session")
    p.set_defaults(func=cmd_untap)

    # list
    p = sub.add_parser("list", parents=[common], help="List active sessions",
                        description="List active sessions as a JSON array. Stale entries (dead server processes) are cleaned up automatically.")
    p.set_defaults(func=cmd_list)

    # exit
    p = sub.add_parser("exit", parents=[common], help="Terminate a session",
                        description="Terminate a session. If the server is unresponsive, force-kills the process and cleans up the socket and registry. With --drain, returns any remaining PTY output and the child's exit status before shutting down.")
    p.add_argument("id", help="Session identifier")
    p.add_argument("--drain", action="store_true",
                   help="Kill child, flush remaining PTY output, and return "
                        "it (plus exit_code/signal) before shutdown")
    p.add_argument("--no-strip-ansi", action="store_true",
                   help="With --drain, preserve ANSI escape sequences in output")
    p.set_defaults(func=cmd_exit)

    args = parser.parse_args(argv)
    socket_dir = getattr(args, "socket_dir", None)
    if socket_dir is not None:
        os.environ["PTY_SOCKET_DIR"] = socket_dir
    args.func(args)


if __name__ == "__main__":
    main()
