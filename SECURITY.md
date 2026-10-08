# Security policy

## Supported versions

| Version | Security fixes |
|---|---|
| Latest Stable release | Yes |
| Latest Beta | Yes |
| Anything older | No — please update first |

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private reporting form:
<https://github.com/ambartsumov/unified-workstation/security/advisories/new>

Please include:

- the version (**Settings → Updates**, or `suw version`) and how it was installed
- operating system and version
- what an attacker can do, and what they need in order to do it
- steps to reproduce, or a proof of concept
- whether the problem is already public anywhere

## What not to disclose publicly

Until a fix is released: exploit details, proofs of concept, and anything that identifies an
affected person or system. Never send real credentials, private keys or your work files —
they are not needed to investigate. A support bundle (**Health → Export support bundle**) is
redacted and safe to attach; review it first.

## What to expect

1. An acknowledgement, normally within a few days. This is a volunteer project: no response
   time is guaranteed.
2. An assessment, and questions if something is unclear.
3. A fix developed privately, a release, and an advisory that credits you unless you prefer
   otherwise.
4. Coordinated disclosure: we ask you to wait for the release before publishing details.

## Scope

In scope: this repository's code, its installers and its update mechanism.

Out of scope: vulnerabilities in Syncthing, Deskflow, Tailscale, OpenSSH or an operating
system (report those upstream — tell us too if this product makes them worse); problems that
require an attacker who already runs code as you; and the documented design choice that
everything in the work folder is shared with computers you paired.

The design is described in [docs/security.md](docs/security.md).
