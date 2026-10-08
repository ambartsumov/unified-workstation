# Architecture

## Layers

| Layer | Lives in | Notes |
|---|---|---|
| User experience | `suw/ui/`, `gnome-extension/`, `dotfiles/hammerspoon/` | Control Center (GTK 4), text panel, panel/menu-bar indicator, shortcut |
| Control plane | `suw/cli/`, `suw/daemon/`, `suw/core/` | one CLI, one daemon, shared core |
| Workstation state | `dotfiles/`, `~/.config/suw/`, project repos | config is layered, shared part is a git repo |
| Network fabric | `suw/integrations/{tailscale,ssh}.py`, `dotfiles/tmux/` | logical names, OSC 52 clipboard |
| Compute fabric | `suw/cloud/`, `suw/core/health.py` | cloud role lifecycle, agentless probes |
| Durability | GitHub, `suw/core/{journal,events}.py` | history, installer journal, event log |

The recommended top-level `core/ cli/ daemon/ ui/ integrations/` folders from the
specification are the sub-packages of one importable Python package, `suw/`.

## Process model

```text
suw (CLI) ──────────────┐
Control Center (GTK) ───┼── Unix socket 0600 ──► suwd ──► git / ssh / tailscale
panel / menu bar ───────┘        ▲
                                 └── state/runtime.json (status cache, atomically replaced)
```

- `suwd` runs two loops: **sync** (30 s local `git status`, 5 min network reconcile, immediate
  on a fresh commit or due checkpoint, exponential backoff on failure) and **health** (local
  metrics every 30 s; Tailscale, home and cloud every 2 min).
- The CLI never requires the daemon. Without it `suw status` computes a local view and
  `suw sync` reconciles in-process.
- A failure in one repository, server or integration is isolated and logged; the others
  continue.

## Source-of-truth map

| Data | Authority |
|---|---|
| Project history | Git / GitHub |
| Fleet inventory, URLs, layout, branding | `~/.config/suw/shared/` (git repo, synced by `suwd`) |
| Machine-specific settings | `~/.config/suw/config.toml` |
| Secrets | OS keychain (`suw secret`), referenced as `keychain://name` |
| Cloud endpoint, host key (public), hardware | inventory, per node, with history |
| Live health | runtime discovery, cached in `~/.local/state/suw/runtime.json` |
| What the installer changed | `~/.local/state/suw/journal.jsonl` |
| What automation did | `~/.local/state/suw/events.jsonl` (rotated, redacted) |

## State machines

**Mode**: `DEFAULT → STARTING_WORKSTATION → WORKSTATION → EXITING_WORKSTATION → DEFAULT`.
The desktop settings Workstation Mode changes are captured once on entry and restored on
exit. Transitional states make an interrupted switch resumable: the next call completes it
using the originally captured values.

**Sync** (per repository): `CLEAN · LOCAL_CHANGES · CHECKPOINT_PENDING · LOCAL_COMMITTED · PUSH_PENDING · PUSH_FAILED · REMOTE_AHEAD · SYNCED · DIVERGED · CONFLICT · OFFLINE · AUTH_REQUIRED · POLICY_BLOCKED · RECOVERY_REQUIRED` (see sync.md). Formerly: `SYNCED · SYNCING · OFFLINE · AHEAD · BEHIND · DIVERGED · CONFLICT ·
ERROR`, plus `LOCAL` for a repository that has no upstream yet.

**Cloud role**: `empty → active(node) → active(new node) [old: retired] → empty`. Nodes are
never deleted from the inventory, only retired.

## Why the daemon polls `git status`

A 30-second `git status` over a handful of repositories costs milliseconds, behaves the same
on Linux and macOS, and needs no native dependency. Event-driven watching (inotify/FSEvents)
is on the roadmap behind the same `Debounce` interface.

## Extension points

- `suw.cloud.CloudProvider` — provider plugins (API create/destroy) next to the generic SSH one.
- `inventory.capabilities()` / `recommend_profile()` — the basis for future `suw run --target`.
- `devices.<name>` with `role = "workstation"` — any number of laptops, not just two.
