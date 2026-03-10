# pty_tools

Command-line tools for spawning processes in PTYs and interacting with them programmatically. Each session runs in a daemonized server process, communicating over a Unix domain socket. Useful standalone and as building blocks for LLM agents that need terminal access.

## Install

```
uv sync
```

## Quick start

```bash
# Start a shell session
pty spawn myshell sh

# Send a command and get output
pty interact myshell --input "echo hello\n" --stable_timeout 500
# {"status": "ok", "exited": false, "response": "$ echo hello\r\nhello\r\n$ ", "mode": "raw"}

# List active sessions
pty list

# Clean up
pty exit myshell
```

## Commands

All commands are subcommands of `pty`. All responses are JSON.

### pty spawn

```
pty spawn <id> <cmd> [--rows 24] [--cols 80]
```

Spawn a process in a new PTY session. The server runs as a detached subprocess — the command returns immediately once the session is ready.

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
pty read <id> [--total_timeout 5000] [--stable_timeout 500] [--pattern REGEX] [--no_strip_ansi]
```

Read output since the last read. Returns JSON:
```json
{"status": "ok", "exited": false, "response": "...", "mode": "raw"}
```

When the child process exits, includes exit status:
```json
{"status": "ok", "exited": true, "exit_code": 0, "signal": null, "response": "...", "mode": "raw"}
```

All responses include `"status"`: `"ok"` on success, `"error"` on failure.

**Timeout behavior:** Wait up to `total_timeout` ms for the first byte. Once output starts, return after `stable_timeout` ms of silence (or when `total_timeout` expires, whichever comes first). If `--pattern` is given, return as soon as the output matches the regex.

ANSI escape sequences are stripped by default. Use `--no_strip_ansi` to preserve them.

The `mode` field is `"raw"` for normal output or `"screen"` when a TUI program (vim, htop, etc.) is using the alternate screen buffer, in which case the response is a pyte-rendered snapshot of the screen contents.

### pty interact

```
pty interact <id> --input TEXT [--total_timeout 5000] [--stable_timeout 500] [--pattern REGEX] [--no_strip_ansi]
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

`pty spawn` launches a detached subprocess that:
1. Spawns the child process via `pexpect` in a PTY
2. Listens on a Unix domain socket at `/tmp/pty_sessions/session_<id>.sock`
3. Handles JSON messages from clients (write, read, interact, exit)

Sessions survive the parent process exiting (including SSH logout) since the server runs in its own session via `start_new_session`.

Reads are serialized (one at a time) and run `pexpect.expect()` in a thread executor so the async server stays responsive to other clients.

A shared registry at `/tmp/pty_sessions/registry.json` (protected by `flock`) tracks active sessions.

## Tests

```
uv run pytest tests/ -v
```
