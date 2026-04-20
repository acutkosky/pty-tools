# pty_tools

Command-line tools for spawning processes in PTYs and interacting with them programmatically. Each session runs in a server process, communicating over a Unix domain socket. Useful standalone and as building blocks for LLM agents that need terminal access.

## Install

```
uv sync
```

## Quick start

```bash
# Start a shell session (runs in foreground, PTY output streams to stdout)
pty spawn myshell sh

# Or detach it to run in the background
pty spawn --detach myshell sh

# Send a command and get output
pty interact myshell --input "echo hello\n" --stable_timeout 500
# {"status": "ok", "exited": false, "response": "$ echo hello\r\nhello\r\n$ "}

# List active sessions
pty list

# Clean up
pty exit myshell
```

## Commands

All commands are subcommands of `pty`. All responses are JSON. Every response includes a `"status"` field: `"ok"` on success, `"error"` on failure. Error responses include an `"error"` field with a description.

### pty spawn

```
pty spawn [--rows 24] [--cols 80] [--detach] [--time_limit SECONDS] [--buffer_limit SIZE] <id> <cmd...>
```

Spawn a process in a new PTY session. `<cmd>` may be either a single shlex-quoted string (`'ls -la'`) or space-separated argv words (`ls -la`). Everything after `<id>` is treated as the child's argv, so any pty-spawn options must appear **before** `<id>`:

```bash
pty spawn --detach --rows 40 myshell ls -la   # --rows applies to the PTY
pty spawn myshell ls --rows 40                # --rows is passed to `ls`
```

By default, the server runs in the **foreground**: stdin is forwarded to the PTY line-by-line, and raw PTY output is streamed to stdout. The session is also accessible via socket commands (`pty read`, `pty write`, etc.) from other processes. Status JSON is printed to stderr. The server exits when the child process exits, or on Ctrl+C.

```bash
# Foreground — interactive, stdout is a live tap of PTY output
pty spawn myshell sh
# stderr: {"status": "ok", "session_id": "myshell", "command": "sh", "pid": 12345}
# stdout: raw PTY output
# exit code: child's exit code

# Pipe input, capture output
echo "ls -la" | pty spawn myshell sh > output.txt 2>/dev/null &
```

Use `--time_limit` to set a maximum lifetime in seconds. When the limit expires, the child process is killed and the session is cleaned up. If omitted, the process runs until it exits on its own or is explicitly terminated.

```bash
# Kill the process after 30 seconds
pty spawn --detach --time_limit 30 myshell 'long-running-command'
```

Use `--buffer_limit` to cap how much child output is retained in memory if no one is reading it. The server keeps a ring buffer of the most recent `SIZE` bytes; older bytes are evicted as new output arrives. Accepts an integer with an optional binary suffix: `4096`, `64K`, `256M`, `1G` (case-insensitive, optional `iB` is tolerated; `K=1024`, `M=1024²`, `G=1024³`). Default is 256M. Every `pty read` / `pty interact` / `pty read --screen` response includes a `"truncated"` field — the cumulative number of output bytes that were dropped from the ring buffer before any reader consumed them. The counter is monotonic for the lifetime of the session, so clients can diff across reads to detect new loss.

```bash
pty spawn --detach --buffer_limit 64M myshell 'noisy-command'
```

With `--detach`, the server runs as a detached background process — the command returns immediately once the session is ready. Detached sessions survive the parent process exiting (including SSH logout).

```bash
pty spawn --detach myshell sh
# {"status": "ok", "session_id": "myshell", "command": "sh", "pid": 12345, ...}
```

### pty write

```
pty write <id> [id...] [--input TEXT] [--stream]
```

Send input to one or more sessions. Three modes:
- `--input TEXT` — send a literal string (escape sequences like `\n` are interpreted)
- `--stream` — send stdin line by line (each line is delivered atomically)
- *(default)* — read all of stdin, send as one chunk

```bash
pty write myshell --input 'echo hello\n'
# {"session_id": "myshell", "status": "ok"}
```

### pty read

```
pty read <id> [--total_timeout 5000] [--stable_timeout 500] [--pattern REGEX] [--no_strip_ansi] [--peek] [--screen]
```

Read output since the last read. Returns JSON:
```json
{
  "status": "ok",
  "exited": false,
  "truncated": 0,
  "response": "..."
}
```

`truncated` is the cumulative number of output bytes dropped from the ring buffer (see `--buffer_limit` on `pty spawn`); it's zero unless the buffer has overflowed.

When the child process exits, includes exit status:
```json
{
  "status": "ok",
  "exited": true,
  "exit_code": 0,
  "signal": null,
  "response": "..."
}
```

**Timeout behavior:** Wait up to `total_timeout` ms for the first byte. Once output starts, return after `stable_timeout` ms of silence (or when `total_timeout` expires, whichever comes first). If `--pattern` is given, return as soon as the output matches the regex.

By default, a read **consumes** the output — subsequent reads only see new data. Use `--peek` to read without consuming: the output is buffered and included in the next read. A normal read (without `--peek`) clears the buffer. This is useful for monitoring a session without interfering with a primary reader.

ANSI escape sequences are stripped by default. Use `--no_strip_ansi` to preserve them.

Use `--screen` to get a snapshot of the virtual terminal screen instead of the read buffer. This uses [pyte](https://github.com/selectel/pyte) to maintain a virtual terminal that tracks all PTY output. The screen snapshot is independent of the read buffer — it doesn't consume it, and reflects what a user would currently see on the terminal (after cursor movement, clears, scrolling, etc.).

```bash
pty read myshell --screen
# {"status": "ok", "response": "$ echo hello\nhello\n$ ", "rows": 24, "cols": 80}
```

### pty interact

```
pty interact <id> --input TEXT [--total_timeout 5000] [--stable_timeout 500] [--pattern REGEX] [--no_strip_ansi] [--peek]
```

Atomic write-then-read. Sends `TEXT` and reads the response in a single operation, avoiding race conditions between separate write and read calls. Output format is the same as `pty read`:

```bash
pty interact myshell --input 'echo hello\n'
# {"status": "ok", "exited": false, "response": "echo hello\r\nhello\r\n$ "}
```

### pty resize

```
pty resize <id> --rows R --cols C
```

Resize the PTY. Updates the terminal size via `TIOCSWINSZ`, sends `SIGWINCH` to the child process group, and resizes the virtual terminal screen. The child process (e.g. vim, less, bash) will reflow its output to the new dimensions.

```bash
pty resize myshell --rows 40 --cols 120
# {"status": "ok", "rows": 40, "cols": 120}
```

In foreground mode, `SIGWINCH` is automatically propagated — when the parent terminal is resized, the PTY and child process are updated to match.

### pty signal

```
pty signal <id> <signal>
```

Send a signal to the child process group. Accepts signal names (`SIGTERM`, `TERM`) or numbers (`15`).

```bash
pty signal myshell TERM
# {"status": "ok", "signal": "SIGTERM"}

pty signal myshell 9
# {"status": "ok", "signal": "SIGKILL"}
```

### pty tap

```
pty tap <out_id> <in_id>
```

Forward all output from one session to the stdin of another. Output is delivered in order via a dedicated worker thread. Multiple taps from the same source are supported — each target receives a copy independently.

```bash
pty tap builder logger
# {"status": "ok", "message": "Tapping output to 'logger'"}
```

If the target session exits or becomes unreachable, the tap is automatically removed. The source session continues operating normally.

### pty untap

```
pty untap <out_id> <in_id>
```

Remove a previously established tap. Untapping a target that was never tapped is a no-op.

```bash
pty untap builder logger
# {"status": "ok", "message": "Removed tap to 'logger'"}
```

### pty list

```
pty list
```

List active sessions as a JSON array. Stale entries (dead server processes) are cleaned up automatically.

```bash
pty list
# [{"session_id": "myshell", "command": "sh", "pid": 12345, "socket_path": "/tmp/pty_sessions/session_myshell.sock", "created_at": 1711234567.89}]
```

### pty exit

```
pty exit <id> [--drain] [--no_strip_ansi]
```

Terminate a session. If the server is unresponsive, force-kills the process and cleans up the socket and registry.

```bash
pty exit myshell
# {"status": "ok", "message": "Shutting down"}
```

With `--drain`, the server kills the child, flushes any remaining PTY output, and returns it along with the child's exit status before shutting down — useful when you want a last-chance read guaranteed not to miss trailing bytes:

```bash
pty exit --drain myshell
# {"status": "ok", "exited": true, "exit_code": 0, "signal": null,
#  "truncated": 0, "response": "...final output..."}
```

## Architecture

Each session is a server process that:
1. Spawns the child process in a PTY (using stdlib `pty` + `subprocess`)
2. Runs a background reader thread that continuously reads PTY output into a buffer
3. Listens on a Unix domain socket at `<socket_dir>/session_<id>.sock` (default `/tmp/pty_sessions`, override with `$PTY_SOCKET_DIR` or the `--socket-dir` flag)
4. Handles JSON messages from clients (write, read, interact, tap, untap, exit)

A pyte virtual terminal (`Screen` + `Stream`) is fed inline in the reader path. This maintains a screen buffer that reflects what a user would see, independent of the read buffer. Screen snapshots are served via the `screen` message type.

In foreground mode, the reader thread also streams raw PTY output to stdout, and a separate thread forwards stdin to the PTY. SIGWINCH is caught and propagated to the child. SIGTERM/SIGHUP are forwarded to the child process group before shutdown. In detached mode (`--detach`), stdin/stdout are disconnected and the process runs independently.

Taps are implemented as a set of target session IDs on the server. When new output arrives, each chunk is queued and sent to targets sequentially by a single worker thread, preserving ordering. Failed sends (target exited, socket gone) automatically remove the tap.

Reads are serialized (one at a time) via an async lock. The read waits on an event that the background reader signals whenever new data arrives, implementing the timeout and pattern-matching logic reactively.

A shared registry at `<socket_dir>/registry.json` (protected by `flock`) tracks active sessions. The socket directory can be configured via the `PTY_SOCKET_DIR` environment variable or the top-level `pty --socket-dir <path>` flag; both client and server invocations must agree on the value (the `--socket-dir` flag sets the env var so that daemonized servers inherit it).

## Tests

```
uv run pytest tests/ -v
```
