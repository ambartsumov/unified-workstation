# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
`MAJOR.MINOR.PATCH` with `aN` / `bN` / `rcN` pre-releases (see [docs/release.md](docs/release.md)).

## [Unreleased]

### Changed
- **The window speaks one language at a time.** Sentences that program logic produces —
  what this computer can do and why, install guidance, operating-system permissions, the
  first-run plan, the system check, recovery and uninstall previews, pairing and settings
  errors, the stages of adding a Cloud server — now come from the translation catalogs
  instead of being written in code, so they follow the chosen language. The start page
  (before the catalog loads) does too. Still English: the detailed diagnostics list and the
  event log, which are technical records, and command-line output.
- **Status rows wrap instead of collapsing.** When a name, a state and its buttons do not fit
  on one line, the buttons move below. Before, a wide group of buttons squeezed the middle
  column until its text broke letter by letter in a narrow window.

### Fixed
- **Windows:** output redirected to a file or a pipe no longer stops with an encoding error on
  the first arrow, typographic quote or Russian sentence; text leaves the program as UTF-8.
- **Packaged builds:** a command that fails no longer opens a modal error box and waits for a
  click that never comes when a script or a service started it; it prints one line, records
  the error in the event log and exits with a failure code.
- **macOS:** the remote shell is handed to `rsync` as one argument. The `rsync` that ships with
  macOS took the following option for the remote shell (“Failed to exec --exclude=…”) when
  sending a project to Cloud. Confirmation on a macOS runner is pending.
- **Sync page:** the “two versions kept” label is no longer covered by the buttons beside it.
- **Capability table:** the explanation column keeps its width in a narrow window.

## [1.0.0b1] — first public beta

The first public edition. It is derived from a private, single-user setup and was extracted
with a clean history; nothing personal was carried over.

### Added
- **Application window** (opens reliably with a sandboxed browser too) with a first-run assistant, overview, workstations, work folder, sync,
  keyboard and mouse, servers, assistant and Git, settings, health and recovery pages.
- **Platform adapters** for Linux, macOS and Windows: directories, background services,
  start at sign-in, credential storage, notifications, permissions.
- **Capability detection** with five honest statuses and a plain-language reason for each.
- **Pairing codes** with a six-digit confirmation number; no shared repository or account.
- **Settings** for every option, validated as a whole, with a snapshot before every change.
- **Recovery centre**: settings snapshots, restore, rebuild, reset, portable export/import
  that never contains secrets.
- **Configuration migrations** with backup, validation and automatic rollback.
- **Update check** with release channels, checksum enforcement and downgrade protection.
- **Support bundle** with names, addresses and identities replaced by placeholders.
- **Demo mode** (`suw app --demo`) driven entirely by fixtures.
- **Profiles**: a configuration layer between the shipped defaults and the user's settings.
- **Assistant adapter model**; Claude Code is the first integration.
- English and Russian translations.
- Packaging definitions for Flatpak, AppImage, DEB, RPM, macOS DMG, a per-user Windows
  installer, MSIX and winget; CI and release workflows with signing, SBOM and provenance.

### Changed
- Defaults no longer act on the user's behalf: no managed project folders, no automatic Git
  commits, no pre-created devices, no browser pages — each is an explicit choice.
- The default work folder is `Desktop/Work`. An existing `Desktop/work` keeps being used.
- A new computer is named after its host name instead of a fixed name.
- Bundled resources ship inside the package, so the product runs from an installed build as
  well as from a source checkout.
- The daemon's control channel works on Windows (loopback with a per-start token).
- Background components are started by the application where neither systemd nor launchd is
  available.

### Security
- The application window is reachable from this computer only: loopback, random port,
  per-session token, Host and Origin checks, a strict Content-Security-Policy.
- System folders, credential folders and the home folder can not be chosen as the work folder.
- A server is trusted only after its fingerprint was confirmed in the window; a changed
  identity blocks the connection.

### Fixed
- Found by the first CI run: settings files restored on Windows were written with doubled
  line endings and could not be read back; optional tools (Git, SSH, a credential store, a
  coding assistant) were reported as failed checks when absent; the window did not open with a
  sandboxed (snap) browser.

### Known limitations
See [docs/beta.md](docs/beta.md).
