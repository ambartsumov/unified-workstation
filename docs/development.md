# Development

Everything here works on a fresh clone, with no account, server, private network or secret.

## Set up

```sh
git clone https://github.com/ambartsumov/unified-workstation
cd unified-workstation
python3 -m pip install --user pytest ruff     # the only development tools
make demo                                     # the window, with sample data
```

Python 3.11+. The product has no runtime dependencies.

| Command | What it does |
|---|---|
| `make test` | the test-suite (isolated: private HOME, stubbed service managers, no network) |
| `make lint` | syntax check and ruff |
| `make sanitize` | secrets, private endpoints, personal data — in files and in Git history |
| `make docs` | documentation links, required pages, version and identifier consistency |
| `make check` | all of the above — exactly what CI runs |
| `make demo` / `make run` | open the window with fixtures / with your real setup |
| `make package` | PyInstaller build for this platform (`pip install ".[package]"` first) |

Windows without `make`: run the commands from the `Makefile` directly, for example
`python -m pytest tests\test_public_edition.py`.

## Tests

- `tests/test_public_edition.py` — platform adapters, capabilities, pairing, settings, backup,
  migration, updates, support bundle, first run, the window. Runs on every operating system.
- the other files — the sync, Git, SSH and Cloud engine, against real throw-away Syncthing
  instances and an unprivileged local `sshd`. They need a POSIX system; tests that need a tool
  that is not installed skip themselves.

No test may touch the real machine: `conftest.py` gives each test its own home folder and puts
stub `systemctl`/`launchctl` first on `PATH`.

## Fixtures and demo data

Use only documentation addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`),
`example.com` names and the device names `workstation-1`, `workstation-2`, `home`, `cloud`.
Never a real endpoint or credential. `make sanitize` enforces this; a deliberately fake secret
in a redaction test needs an entry in `scripts/sanitize-allow.txt`.

## Adding things

- **A setting:** add it to `SCHEMA` in `core/config.py`, to `defaults.toml`, to `FIELDS` in
  `core/settings.py`, and its label to both translation catalogs. The tests fail until all four
  agree.
- **A user-facing text:** a key in `suw/resources/i18n/en.json` and `ru.json`.
- **A failure the user can hit:** raise `Problem("code")` in `app/backend.py` and add
  `problem.code.what` (and `.why`, `.fix`) to the catalogs.
- **An operating-system behaviour:** a method on `suw/platform/base.py`, implemented in each
  adapter.
- **An assistant integration:** a subclass of `Assistant` in `integrations/assistants.py`.

See [Architecture](architecture.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).
