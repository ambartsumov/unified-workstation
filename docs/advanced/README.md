# Advanced: the command line

Nothing here is needed for normal use. The `suw` command is for scripting, diagnostics and
people who prefer a terminal.

```sh
suw status [--json]         # can I work right now?
suw doctor                  # full diagnosis with fixes
suw capabilities [--json]   # what this computer supports
suw app [--demo]            # open the window
suw pair code | review <code> | accept <code>
suw config show | set <key> <value> | validate
suw support -o bundle.zip   # redacted support bundle
suw uninstall [--dry-run] [--purge]
```

Most commands accept `--json` (machine-readable output) and those that change something accept
`--dry-run`.

The pages below are the engine's original command-line reference. They predate the application
window: where they describe editing a file or running a script, the window now does the same
through **Settings**, **Servers** and **Health**.

- [Sync engine and project manifests](cli-sync.md)
- [Cloud role, bootstrap profiles, project transfer](cli-cloud.md)
- [Clipboard internals and live test](cli-clipboard.md)
- [Networking](cli-networking.md)
- [Security internals](cli-security.md)
- [Recovery from the command line](cli-recovery.md)
- [Engine architecture](cli-architecture.md) · [Design decisions](decisions/)

Environment variables for isolated runs and tests: `SUW_HOME`, `SUW_CONFIG_DIR`,
`SUW_STATE_DIR`, `SUW_CACHE_DIR`, `SUW_RUNTIME_DIR`, `SUW_PROFILE_FILE`.
