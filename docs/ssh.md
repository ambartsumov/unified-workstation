# SSH

Home and Cloud servers are reached with standard OpenSSH. The application adds names and
checks on top; it does not replace or weaken SSH.

## Roles instead of addresses

You define **Home**, **Cloud** (and paired workstations) once. After that:

- in the application: **Servers → Connect**
- in a terminal: `ssh home`, `ssh cloud` (with *Server names for SSH* enabled)

When an address changes you edit it in one place and both keep working.

## What is written to your SSH configuration

One `Include` line inside a clearly marked block in `~/.ssh/config`, pointing at
`~/.ssh/suw.conf`, which the application owns and regenerates. Your own entries are never
edited. The original file is backed up before the first change, and
[Recovery](recovery.md) can undo it.

## Identity checks can not be switched off

- A server is trusted only after you compared and confirmed its fingerprint.
- If a server's identity changes later, the connection is **blocked** and the application says
  so in plain words: *"The connection failed because the server's identity changed."* You
  review the new fingerprint before anything connects again.

## Passwords and keys

Key sign-in is recommended. If a server needs a password, it is stored in the operating
system's credential store (Keychain, Secret Service, Windows Credential Manager) — never in a
configuration file. See [Security](security.md).
