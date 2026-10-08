# Recovery

Everything on the **Recovery** page works without Internet.

| You want to… | Where |
|---|---|
| get back a file another computer replaced or deleted | Recovery → Files → *View and restore* |
| resolve a conflict | Sync → Conflicts |
| undo a settings change | Recovery → Settings → *Restore* next to a snapshot |
| move settings to another computer | Recovery → *Export configuration* / *Import configuration* |
| recover from a damaged settings file | Recovery → *Rebuild damaged configuration* |
| start over with default settings | Recovery → *Reset all settings* |
| disconnect a computer that misbehaves | Workstations → *Unpair* |
| reset one integration | Health → Repair |
| go back to an earlier application version | Recovery → Earlier application versions |

## Snapshots

A snapshot of your settings is taken automatically **before** every change: a settings edit, a
restore, an import, a reset, a repair, a migration during an update. Restoring a snapshot first
saves the current settings as a new snapshot, so a restore can itself be undone.

## Damaged configuration

If a settings file can not be read, the application keeps running on the last version that
worked and tells you. *Rebuild* replaces the unreadable file with that version and keeps the
damaged file beside it.

## Export and import

An export clearly separates:

- **portable settings** — always included
- **this computer's own settings** (name, folder locations, ports) — only if you tick the box
- **paired computers and server addresses** — only if you tick the box
- **passwords, keys, tokens** — never. They stay in this computer's credential store; after an
  import the application asks for them when they are needed.

An import is validated as a whole before anything is written.

## Repair

**Health → Repair** always works in this order: back up → repair → verify. No repair deletes
your files.

## Earlier application versions

Installers the application downloaded and verified are kept, so an earlier version can be
installed again. Your settings are migrated forward automatically on update; a failed
migration is rolled back and the previous format stays in use.
