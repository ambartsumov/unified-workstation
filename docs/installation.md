# Installation

> **Status: public beta.** Signed installers are produced by the release workflow once the
> signing identities are configured (see [Release process](release.md)). Until the first signed
> release is published, install from source as described at the end of this page. Platform
> verification status is listed in [Supported platforms](supported-platforms.md).

## Linux

| Format | Install |
|---|---|
| Flatpak | open the `.flatpakref` from the release page, or `flatpak install` the bundle |
| AppImage | download, mark as executable in the file's Properties, double-click |
| DEB (Debian, Ubuntu, Mint) | open the `.deb` with your software centre |
| RPM (Fedora, openSUSE) | open the `.rpm` with your software centre |

## macOS

Download the `.dmg` for your Mac (**Apple Silicon** or **Intel**), open it and drag
*Unified Workstation* to *Applications*. Release builds are signed with a Developer ID and
notarized by Apple, so macOS opens them without a warning.

## Windows

Run `unified-workstation-…-setup.exe` for your computer (**x64** or **ARM64**). It installs for
your user account only and does not ask for administrator rights. A `winget` package and an
MSIX package are prepared for the same release.

## What the application builds on

The application uses well-known tools when they are installed and tells you plainly when they
are not. None is required to start.

| Tool | Needed for |
|---|---|
| [Syncthing](https://syncthing.net) | keeping the work folder in sync |
| [Deskflow](https://deskflow.org) | sharing one keyboard and mouse |
| OpenSSH | Home and Cloud servers |
| [Tailscale](https://tailscale.com) (optional) | reaching your computers when they are not on the same network |
| Git, a code editor, a terminal (optional) | Workstation Mode |

**Health → Components** shows what is installed and how to get what is missing on your system.

## From source (developers, and until signed releases exist)

Requires Python 3.11 or newer. No other dependency.

```sh
git clone https://github.com/ambartsumov/unified-workstation
cd unified-workstation
python3 bin/suw.py app          # open the application window
python3 bin/suw.py app --demo   # the same window with sample data; touches nothing
```

On Linux and macOS, `bin/suw` is a launcher that finds a suitable Python.

Next: [First run](first-run.md)
