# Architecture

```text
┌──────────────────────────── UI ────────────────────────────┐
│  application window (suw/app/static)   ·   CLI (suw/cli)   │
└──────────────┬──────────────────────────────┬──────────────┘
               │ plain data in / out          │
        suw/app/backend.py  (one method per thing a person can do;
               │             every failure is a Problem: what / why / fix)
┌──────────────┴──────────── CORE ENGINE (suw/core) ─────────────────────┐
│ workspace & settings   config · settings · profiles · migrate · backup │
│ device identity        inventory · pairing                             │
│ state & history        state · events (redacting) · journal (undo)     │
│ health & recovery      capabilities · doctor · support · update        │
│ server roles           health · askpass · cloud/                       │
│ first run              onboarding · bootstrap                          │
│ background             daemon/suwd · ipc · supervise · lock            │
└──────────────┬───────────────────────────────┬────────────────────────┘
   PLATFORM ADAPTERS (suw/platform)      INTEGRATIONS (suw/integrations)
   linux · macos · windows               worksync (Syncthing) · peripherals (Deskflow)
   directories · services · autostart    tailscale · ssh · apps (editor/terminal)
   credentials · notifications           assistants (Claude Code, …) · gnome · browser
   permissions · opening things
```

## Rules the code follows

- **Operating-system differences live in the platform adapters.** Code asks
  `suw.platform.current()` for a directory, a credential, an autostart entry, a service, so a
  new operating-system behaviour is a change to one adapter. This holds for everything written
  for the public edition; parts of the original engine (`core/bootstrap.py`, service control in
  `integrations/`, the GNOME and Hammerspoon desktop helpers) still branch on the platform and
  are being moved behind the adapters.
- **Capabilities are detected, never assumed** (`core/capabilities.py`). Each feature has one
  status — supported, partially supported, requires permission, not installed, unavailable —
  and a reason in plain language. An unsupported feature is never reported as healthy.
- **Mature tools do the hard parts.** Syncthing synchronises, Deskflow shares input, OpenSSH
  connects, the operating system stores credentials. The product configures and supervises
  them; it implements no sync protocol, input protocol or transport of its own.
- **Local-first.** No backend, no account, no database server. State is small JSON/TOML files,
  written atomically, versioned and self-repairing.
- **Every change to the user's machine is journaled** (`core/journal.py`): backed up first,
  recorded, reversible. User data is never journaled for removal.
- **No user-facing sentence in logic.** Texts live in `suw/resources/i18n/*.json`; a test
  keeps the English and Russian catalogs identical in keys and placeholders.
- **The standard library is the only runtime dependency.** `pywebview` (an embedded window)
  and PyInstaller (packaging) are optional and build-time only.

## Configuration layers

```
shipped defaults  →  active profile  →  shared settings  →  this computer
```

The *profile* layer is what keeps a personal or organisational setup from becoming a fork: a
private edition is the public core plus one profile file plus optional integrations. See
[Migrating from the private edition](migration-from-private.md).

## The application window

A loopback HTTP server (`suw/app/server.py`) serves a small static page to the system's own web
view or browser. Loopback only, random port, per-session token, Host and Origin checks, and a
Content-Security-Policy that forbids everything remote. The page is vanilla HTML, CSS and
JavaScript with no build step. `suw app --demo` swaps the backend for fixtures
(`suw/app/demo.py`) and touches nothing.

## Background work

`suwd` is one small per-user daemon: Git reconciliation of managed repositories (opt-in),
health probes with bounded exponential backoff, and supervision of the sync and sharing
services. It never blocks on, or dies because of, a server, a network or an assistant being
unavailable. On systems without systemd or launchd the product starts its components itself
(`core/supervise.py`) and restores them at sign-in.

Design decisions with their reasoning: [advanced/decisions](advanced/decisions/).
