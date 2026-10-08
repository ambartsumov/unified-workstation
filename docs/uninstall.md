# Uninstall

Uninstalling never deletes your work folder, your projects, your synced files, your Git
repositories or anything on your Home or Cloud servers.

**Recovery → Uninstall → Show what would be removed** lists, before you decide:

- **Always kept** — work folder, projects, server data, installed tools, saved passwords
- **Removed** — the background services, the start-at-sign-in entry, and the marked blocks the
  application added to your configuration files (each restored from its backup)
- **Removed only if you ask** — the application's settings, logs and list of paired computers

## How

| System | Steps |
|---|---|
| Windows | Settings → Apps → Unified Workstation → Uninstall. It asks whether to keep your settings. |
| macOS | First **Recovery → Uninstall** inside the application (stops services, restores files), then move the application to the Bin. |
| Linux (Flatpak) | `flatpak uninstall io.github.ambartsumov.UnifiedWorkstation` |
| Linux (DEB/RPM) | remove *unified-workstation* with your software centre |
| Linux (AppImage) | delete the file |

On Linux and macOS, run the in-application step first, or use the command line:
`suw uninstall` (add `--purge` to also remove settings; `--dry-run` to only look).

## Removing settings by hand

The folders are shown under **Settings → Advanced**. Deleting them removes settings, logs and
the pairing list — never the work folder.
