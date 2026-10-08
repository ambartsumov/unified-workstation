# Roadmap

Four lists, kept apart so that nothing planned is mistaken for something that exists.
"Stable" means implemented **and** verified with evidence; see
[Supported platforms](docs/supported-platforms.md).

## Stable

Nothing is labelled Stable yet. The first Stable release requires the
[release checklist](docs/release-checklist.md) to be complete.

## In the current beta (implemented; verification in progress)

- Application window, first-run assistant, settings, health, recovery — all platforms
- Work folder and file sync through Syncthing
- Pairing with code and confirmation number
- Keyboard, mouse and clipboard sharing through Deskflow
- Home and Cloud server roles; transactional Cloud replacement; project-scoped transfer
- Claude Code project-context classification
- Update check with checksum enforcement

## Planned

- Signed installers on every platform; first run of the release workflow
- Real-device acceptance on Linux, macOS and Windows, including the three-computer test and
  every keyboard/mouse pair
- Automatic installation of verified updates, with rollback
- Buttons for what is command-line only today: Home replica setup, Cloud bootstrap profiles,
  saving a server password
- Localised capability explanations and permission texts
- A system-tray / menu-bar status item
- QR code for pairing
- GitHub Actions pinned to commit SHAs
- Screen-reader review on each platform

## Experimental

- Flatpak with host-tool access through the portal
- Linux sessions without systemd (components started by the application)
- Windows on ARM64

## Platform-specific

- **Windows:** window placement and a global shortcut for Workstation Mode; a native `rsync`
  alternative for sending projects to Cloud; background components as a proper per-user service
- **macOS:** native file watching for Git status; window placement without Hammerspoon
- **Linux:** window placement on KDE Plasma and other desktops; RPM and DEB repositories

## Not planned

A hosted backend, user accounts, a proprietary sync protocol, a custom remote transport, or
telemetry infrastructure. Local-first is a principle, not a phase.
