# Sync

Files in the work folder are synchronised by [Syncthing](https://syncthing.net), a mature
open-source engine. The application configures and supervises a private Syncthing instance
for you; it does not implement its own sync protocol.

## States

| State | Meaning |
|---|---|
| ● In sync | every paired computer has the same files |
| ◐ Syncing | changes are being exchanged |
| ○ Offline | this computer is up to date; the other one is not reachable |
| ▲ Conflict | a file changed on two computers; both versions are kept |
| ‖ Paused | paused until you resume |
| ✕ Error | something is blocking sync — the page says what |
| ○ Not set up | sync is not configured, or no computer is paired yet |

## Actions

**Sync now**, **Pause**, **Resume**, **Scan for changes**, **View conflicts**,
**Restore version** — all on the **Sync** page. No terminal needed.

## Conflicts

If the same file is edited on two computers before they sync, both versions are kept. Choose
which one to keep; the other goes to the application's trash (kept 30 days by default), not
away.

## Earlier versions

A file replaced or deleted from another computer is kept as an earlier version for 30 days
(configurable). **Sync → View and restore** lists them. Restoring never overwrites: if a file
with that name exists, the restored copy gets a new name.

## Offline

Each computer keeps working without the other and without Internet. When the connection
returns, sync resumes by itself. Computers on the same local network sync directly, with no
Internet involved.

## How files travel

Directly between your paired computers, encrypted, over your local network or your private
network. Syncthing's public discovery and relay servers are switched off.

Next: [Workstations and pairing](workstations.md)
