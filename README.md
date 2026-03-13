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

All commands are subcommands of `pty`. All responses are JSON.

### pty spawn

```
pty spawn <id> <cmd> [--rows 24] [--cols 80] [--detach]
```

Spawn a process in a new PTY session.

By default, the server runs in the **foreground**: stdin is forwarded to the PTY line-by-line, and raw PTY output is streamed to stdout. The session is also accessible via socket commands (`pty read`, `pty write`, etc.) from other processes. Status JSON is printed to stderr. The server exits when the child process exits, or on Ctrl+C.

```bash
# Foreground — interactive, stdout is a live tap of PTY output
pty spawn myshell sh

# Pipe input, capture output
echo "ls -la" | pty spawn myshell sh > output.txt 2>/dev/null &
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

### pty read

```
pty read <id> [--total_timeout 5000] [--stable_timeout 500] [--pattern REGEX] [--no_strip_ansi] [--peek]
```

Read output since the last read. Returns JSON:
```json
{
  "status": "ok",
  "exited": false,
  "response": "..."
}
```

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

All responses include `"status"`: `"ok"` on success, `"error"` on failure.

**Timeout behavior:** Wait up to `total_timeout` ms for the first byte. Once output starts, return after `stable_timeout` ms of silence (or when `total_timeout` expires, whichever comes first). If `--pattern` is given, return as soon as the output matches the regex.

By default, a read **consumes** the output — subsequent reads only see new data. Use `--peek` to read without consuming: the output is buffered and included in the next read. A normal read (without `--peek`) clears the buffer. This is useful for monitoring a session without interfering with a primary reader.

ANSI escape sequences are stripped by default. Use `--no_strip_ansi` to preserve them.

### pty interact

```
pty interact <id> --input TEXT [--total_timeout 5000] [--stable_timeout 500] [--pattern REGEX] [--no_strip_ansi] [--peek]
```

Atomic write-then-read. Sends `TEXT` and reads the response in a single operation, avoiding race conditions between separate write and read calls.

### pty list

```
pty list
```

List active sessions as a JSON array. Stale entries (dead server processes) are cleaned up automatically.

### pty exit

```
pty exit <id>
```

Terminate a session. If the server is unresponsive, force-kills the process and cleans up the socket and registry.

## Architecture

Each session is a server process that:
1. Spawns the child process in a PTY (using stdlib `pty` + `subprocess`)
2. Runs a background reader thread that continuously reads PTY output into a buffer
3. Listens on a Unix domain socket at `/tmp/pty_sessions/session_<id>.sock`
4. Handles JSON messages from clients (write, read, interact, exit)

In foreground mode, the reader thread also streams raw PTY output to stdout, and a separate thread forwards stdin to the PTY. In detached mode (`--detach`), stdin/stdout are disconnected and the process runs independently.

Reads are serialized (one at a time) via an async lock. The read waits on an event that the background reader signals whenever new data arrives, implementing the timeout and pattern-matching logic reactively.

A shared registry at `/tmp/pty_sessions/registry.json` (protected by `flock`) tracks active sessions.

## Tests

```
uv run pytest tests/ -v
```
