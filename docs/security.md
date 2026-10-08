# Security

How the product protects you, and what it deliberately does not do. To report a
vulnerability, see [SECURITY.md](../SECURITY.md).

## Principles

- **Local-first.** No account, no central service, no cloud backend. Your computers talk to
  each other directly.
- **Explicit trust.** A computer is trusted only after you paired it and compared a
  confirmation number. A server is trusted only after you confirmed its fingerprint. Nothing on
  the local network is trusted automatically.
- **Least privilege.** The application runs as you, never as administrator or root, and asks
  only for permissions a feature you switched on actually needs.
- **Private by default.** Sync and keyboard sharing listen on your private network; public
  discovery and relay servers are switched off.

## Credentials

Passwords are stored in the operating system's credential store:

| System | Store |
|---|---|
| Linux | Secret Service (GNOME Keyring, KWallet) |
| macOS | Keychain |
| Windows | Credential Manager |

Configuration files hold only *references* (`keychain://name`). A configuration that contains
something that looks like a secret value is rejected. If no credential store is available, the
application says so and does not save passwords.

## The application window

The window is served from this computer to this computer only: loopback address, a port chosen
by the system, and a random token per session. Requests from another site or another host name
are refused, and the window loads nothing from the network (strict Content-Security-Policy —
no remote scripts, fonts or trackers).

## The background service

Controlled through a Unix socket readable only by you (Linux, macOS), or a loopback port with
a per-start token stored in your profile (Windows). It refuses to run as root.

## Data boundary

Everything in the work folder is shared; system identity never is. See
[Work folder](workspace.md). Git operations that destroy work — reset, clean, force-push — are
never performed by the application.

## Logs and support bundles

The event log redacts tokens, keys, passwords and credentials in URLs as it is written.
**Health → Export support bundle** additionally replaces names, addresses and identities with
placeholders and contains no work files. Review the bundle before sharing it.

## Updates

Fetched over HTTPS, used only if the SHA-256 matches the published one, and installed only by
you. Installers are signed; your operating system verifies the signature. See
[Updates](updates.md).

## Known limits

- Anyone who can run programs as *you* on your computer can read what you can read. The
  application does not defend against that.
- A project's `.env` file inside the work folder is shared with your paired computers by
  design. Do not pair a computer you do not control.
