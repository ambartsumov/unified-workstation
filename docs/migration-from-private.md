# Migrating from the private edition

The public edition was extracted from a personal, single-user setup. This page is for the
person who ran that setup (or any fork like it) and wants to move to the public product
without losing anything — and for anyone who wants to keep a personal layer on top of it.

## The model: core + profile, not a fork

```
public core  +  your profile  +  platform adapter  +  optional integrations
```

Everything personal — branding, device names, default folders, which pages the browser opens,
automatic Git checkpoints — is configuration. Put it in a profile file:

```
<settings folder>/profiles/<name>.toml
```

and select it (`profile.active = "<name>"` in this computer's settings, or
`SUW_PROFILE_FILE=/path/to/profile.toml` for a managed setup). A profile sits between the
shipped defaults and your own settings, so the code stays identical to the public one.
`suw/resources/config/shared.example.toml` shows the format.

## What changed in the defaults

| Setting | Private pre-release | Public default |
|---|---|---|
| Work folder | `~/Desktop/work` | `~/Desktop/Work` |
| Managed project folders | one personal folder | none |
| Automatic Git checkpoints | on | off (opt-in) |
| Pre-created devices | two fixed names and `home` | none — this computer is named after its host name |
| Browser pages in Workstation Mode | personal list | none |
| Keyboard/mouse host | a fixed device name | unset until chosen |
| Shared settings | a private Git repository | not a repository; pairing codes replace it |

## What migrates automatically

On first start the configuration migration (`suw/core/migrate.py`) takes a snapshot, then:

- keeps your existing work folder: if `~/Desktop/work` exists and no folder was configured, it
  is written down explicitly, so no second empty folder appears;
- keeps your device name, identity, paired computers and server roles — they are in the
  settings and inventory files, which are used in place;
- validates the result and rolls back to the snapshot if anything fails.

## What is deliberately not carried over

| Item | Why | What to do |
|---|---|---|
| Passwords, tokens | they live in the operating system's credential store and were never in configuration | nothing — the same store is used |
| Machine identity, SSH host keys, private-network identity | never part of any export | nothing |
| Private endpoints | not part of an *export* unless you tick *Include paired computers and server addresses* | re-enter, or tick the box when exporting from your own computer |
| The private shared-settings repository | the public edition does not need one | optional: keep it as your own backup |

## Running both on one computer

The two editions use the same settings and data folders, so they see the same setup. That is
the migration path, not a way to run them side by side: stop the private edition's services
before starting the public one, and do not run both daemons at once. To try the public edition
without touching an existing setup, use the demo mode, or point it at a scratch location:

```sh
SUW_HOME=/tmp/uw-trial HOME=/tmp/uw-trial python3 bin/suw.py app
```

## Keeping the private edition

It remains a valid reference installation. Nothing in the public repository depends on it,
and nothing from it — history, endpoints, names — was copied into the public history.
