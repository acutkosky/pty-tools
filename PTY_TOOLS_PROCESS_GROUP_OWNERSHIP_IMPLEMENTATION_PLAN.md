# Process-group ownership plan (superseded)

This draft has been superseded by
[PTY_TOOLS_STARTUP_READINESS_AND_TIMEOUT_GRACE_PLAN.md](PTY_TOOLS_STARTUP_READINESS_AND_TIMEOUT_GRACE_PLAN.md).

After review, we decided not to make `pty-tools` own or automatically kill the
launched command's process group. Automatic cleanup remains direct-child-only;
applications that need group or tree containment can supply a guardian wrapper.
