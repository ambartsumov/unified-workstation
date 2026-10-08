# Public beta

**Version 1.0.0b1 is a beta.** It is offered so that people can try it and report what does
not work. It is not labelled Stable and the updater will not offer it to Stable users.

## What the beta is

- the complete application window: first-run assistant, overview, workstations and pairing,
  work folder, sync, keyboard and mouse, servers, assistant and Git, settings, health, recovery
- English and Russian
- demo mode (`suw app --demo`)
- the engine for sync, pairing, servers and recovery, with its test-suite

## Supported in this beta

Only what the [verification table](supported-platforms.md#verification-status-version-100b1)
lists as verified. Today that is **Linux x86_64 (Ubuntu 24.04, GNOME)** at the level of
automated tests. Everything else is *pending*: it is implemented and has CI jobs defined, but
has not been observed working yet.

## Known limitations

- **macOS and Windows have not been run on real hardware.** Expect rough edges; please report
  them with the *Platform compatibility* issue form.
- **Keyboard and mouse sharing has not been verified on any device pair** for this edition.
- **No signed installers yet.** They require signing identities that only the maintainers can
  provide (see [Release process](release.md)). Until then: install from source.
- Windows: Workstation Mode opens your tools but does not arrange windows or register a global
  shortcut; sending a project to Cloud needs an `rsync`; the background components are started
  by the application rather than a Windows service.
- Linux desktops other than GNOME: Workstation Mode opens your tools without placing windows.
- Home-server replica setup, Cloud bootstrap profiles and credential entry for password
  sign-in are available from the command line only.
- Capability explanations and operating-system permission texts are in English in both
  languages.
- The embedded window needs `pywebview`; without it the window opens in a Chromium-based
  browser in application mode, or in the default browser.
- Automatic installation of updates is not implemented: the application downloads and
  verifies, you open the installer.

## Rolling back

Uninstall keeps your work folder and (if you choose) your settings; install the previous
version. Settings changed by a migration are restored from the snapshot taken before it
(**Recovery → Settings**).

## Reporting

**Health → Export support bundle**, then
[open an issue](https://github.com/ambartsumov/unified-workstation/issues/new/choose).
Security problems: [SECURITY.md](../SECURITY.md) — not a public issue.

## Leaving beta

The first Stable release requires every row of the [release checklist](release-checklist.md)
to be ticked with evidence, including the acceptance procedures on each platform it claims.
