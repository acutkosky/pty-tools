# Reliable startup readiness and graceful time limits

## Status

Accepted implementation plan. This replaces the earlier process-group ownership
proposal after design review.

## Outcome

The change has two goals:

1. A detached `pty spawn` reports success only after its server and direct PTY
   child are fully initialized and the socket request path is usable.
2. A time limit sends `SIGTERM` to the direct child, waits one second, and sends
   `SIGKILL` to that direct child only if it has not exited.

There is no new process-group ownership or descendant-cleanup guarantee.

## User-visible behavior

### Detached startup

`pty spawn --detach` returns success after all of the following are true:

- the session ID is owned by the new server;
- the Unix socket is bound and accepting requests;
- the PTY direct child was successfully created;
- PTY output collection is active; and
- child-exit observation and the optional time limit are installed.

A nonexistent executable or another startup failure returns an error without a
ready session, registry entry, or socket left behind. A command that is created
successfully and then exits immediately still counts as a successful spawn so
its output and exit status can be read.

### Time limit

At the configured deadline:

1. If the direct child is running, send it `SIGTERM` by PID.
2. Wait up to one second for it to exit and record its status.
3. If it remains alive, send it `SIGKILL` by PID and reap it.
4. Close the PTY server and remove its owner-matched socket and registry entry.

The deadline closes the session even if the direct child exited earlier. The
one-second grace is incurred only while a live direct child is being given time
to stop.

The direct-child status remains observable in the existing form: a normal
`sleep` terminated at the deadline reports `SIGTERM` (foreground exit status
143), while a child that ignores `SIGTERM` reports `SIGKILL` (137) after the
grace period.

## Deliberate non-goals

- Natural direct-child exit does not trigger a signal to its descendants.
- Time-limit and `pty exit` cleanup do not call `killpg()`.
- No child PID or process-group ID is added to the registry.
- No supervisor, anchor, subreaper, or long-lived process-group monitor is
  introduced.
- `pty signal` is unchanged and continues to signal the active child's process
  group explicitly.

Closing a PTY can still cause terminal-attached programs to receive ordinary
kernel-generated hangup behavior. Conversely, descendants using mechanisms
such as `nohup`, redirected standard streams, or a new session may survive. A
caller that needs stricter containment should launch a guardian wrapper as the
direct command.

## Registry readiness protocol

New registry entries have an internal state:

```json
{
  "session-name": {
    "command": "sh",
    "pid": 1234,
    "socket_path": "/tmp/pty_sessions/session_session-name.sock",
    "created_at": 123.0,
    "state": "starting"
  }
}
```

The server reserves the name as `starting`, then changes the same owner-matched
entry to `ready` only after startup completes. `daemonize_server()` waits for
its own server PID to reach `ready`; seeing its reservation is not enough.

Compatibility and visibility rules:

- a live `starting` or `ready` entry blocks duplicate reservation;
- `pty list` shows only ready entries and does not expose the internal `state`;
- legacy entries without `state` are treated as ready; and
- a connection attempted before the socket is bound reports that the live
  session is still starting.

Registry cleanup is owner-checked. A startup failure or stale liveness check
can unlink the socket and remove the entry only if the recorded server PID
still matches. This prevents late cleanup from deleting a newer server that
reused the same session ID.

## Server startup transaction

The startup sequence is:

1. Install catchable server signal handlers.
2. Atomically reserve the session ID as `starting`.
3. Bind the Unix socket with `start_serving=False`.
4. Open the PTY and create the direct child with `start_new_session=True`.
5. Close the server's copy of the slave descriptor.
6. Install the event-loop PTY reader and direct-child exit watcher.
7. Install the optional time-limit callback.
8. Start accepting connections.
9. Mark the owner-matched registry entry `ready`.
10. Open the server's internal readiness gate without yielding the event loop.

The client handler waits on the internal gate, protecting against a client that
guesses the socket path during startup. A signal received before startup is
complete sets an abort flag; the startup coroutine observes it and performs the
single rollback path instead of racing a concurrent shutdown coroutine.

Rollback closes whichever subset of the server socket, PTY descriptors,
direct child, reader, watcher, and timer was created. If the direct child was
created, rollback kills and reaps that PID only. It then removes the socket and
registry reservation through the owner-checked cleanup operation.

## Detached-parent readiness timeout

The parent retains the existing two-second startup deadline but polls readiness
at 10 ms intervals instead of 100 ms intervals. This avoids adding material
startup latency while still waiting for the stronger condition.

If readiness does not arrive, the parent first sends the server `SIGTERM` and
allows one second for catchable rollback. It uses `SIGKILL` only if that server
does not exit. The parent then performs owner-checked cleanup as a final stale
entry safeguard.

## Verification

Tests cover:

- `starting` to `ready` transitions and owner-matched mutation;
- owner-checked cleanup preserving a mismatched replacement;
- immediate socket use after detached spawn returns;
- failed child creation leaving no socket or registry entry;
- concurrent duplicate spawns producing exactly one winner;
- ordinary timeout exit via `SIGTERM`;
- a child completing cleanup within the one-second grace;
- an uncooperative child receiving `SIGKILL` after the full grace; and
- a same-process-group descendant not being explicitly signaled by timeout.
