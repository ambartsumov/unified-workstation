# Supported platforms

Two different questions are answered here and kept apart on purpose:

1. **What can work** on a platform — design and operating-system limits.
2. **What has been verified**, and how. A platform is called *supported* only when it is in the
   tested matrix. Nothing below is marked verified without evidence.

Inside the application, **Health → What works on this computer** shows the same statuses,
detected live on your machine.

## Verification status (version 1.0.0b1)

| Platform | Automated tests | Packaged build | Real-device acceptance |
|---|---|---|---|
| Linux x86_64 — Ubuntu 24.04, GNOME, Wayland | **Verified**: full suite passes locally; the window was driven through every page in headless Chromium | Pending (CI job defined) | Pending for this edition¹ |
| Linux arm64 | Pending — first CI run | Pending | Pending |
| Linux — KDE Plasma, X11 sessions, Fedora, Arch | Not tested | Pending | Pending |
| macOS 13+ Intel | Pending — first CI run | Pending (needs Developer ID) | Pending |
| macOS 14+ Apple Silicon | Pending — first CI run | Pending (needs Developer ID) | Pending |
| Windows 10/11 x64 | Pending — first CI run (platform layer and window only) | Pending (needs signing identity) | Pending |
| Windows 11 ARM64 | Pending — first CI run (platform layer and window only) | Pending | Pending |

¹ The engine this edition is derived from was used daily on one Ubuntu workstation. That is
experience, not acceptance of this edition: the [acceptance procedures](acceptance.md) have
not yet been run against a 1.0 build.

**Keyboard/mouse sharing and the three-computer sync test have not been verified on real
hardware for any operating-system pair.** See [Public beta](beta.md) for what that means.

## Feature matrix (design limits)

| Feature | Linux | macOS | Windows |
|---|---|---|---|
| Application window, first-run assistant, settings | Yes | Yes | Yes |
| Work folder | Yes | Yes | Yes |
| File sync (Syncthing) | Yes | Yes | Yes — started by the application (no Windows service) |
| Pairing with code and confirmation number | Yes | Yes | Yes |
| Shared keyboard and mouse (Deskflow) | X11: yes · Wayland: needs the input-capture portal (GNOME 46+, Plasma 6.1+) | Needs Accessibility and Input Monitoring permission | Not inside administrator windows or on the sign-in screen |
| Shared clipboard | Text; images depend on the desktop under Wayland | Text and images | Text and images |
| Clipboard from server sessions | With WezTerm (or another OSC 52 terminal) | With WezTerm | With WezTerm |
| Workstation Mode: open my tools | Yes | Yes | Yes |
| Workstation Mode: place windows on workspaces, global shortcut | GNOME only (helper extension) | Through Hammerspoon, if installed | **Not implemented** |
| Background service | systemd user service; otherwise started by the application | LaunchAgent | Started by the application at sign-in |
| Password storage | Secret Service | Keychain | Credential Manager |
| Home and Cloud servers (SSH) | Yes | Yes | Yes (OpenSSH client feature) |
| Send a project to Cloud (rsync) | Yes | Yes | **Unavailable** until an `rsync` is installed |
| Instant change detection for Git status | Yes (inotify) | Timer-based | Timer-based |
| Home-server replica setup, Cloud bootstrap | The *server* must run Linux | — | — |

## Packages

| Format | Architectures | Signing |
|---|---|---|
| Flatpak, AppImage, DEB, RPM | x86_64, arm64 | build provenance attestation; repository signing where a repository is used |
| macOS DMG | Apple Silicon, Intel (separate builds) | Developer ID + notarization — the release job fails without it |
| Windows installer (per-user), MSIX, winget manifest | x64, ARM64 | Authenticode through Azure Artifact Signing — the release job fails without it |

## Minimum versions

Python 3.11 (source installs only; packaged builds bring their own) · macOS 12 · Windows 10
version 2004 · a Linux desktop with a web view or a Chromium-based browser for the window.
