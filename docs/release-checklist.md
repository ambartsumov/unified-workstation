# Release checklist

Tick a box only with evidence: the command and its output, a CI run, or a row in the
[acceptance results](acceptance.md#results). An empty box is an honest answer.

Status below is for **1.0.0b1**, prepared in a local workspace.

## Product
- [x] Product name, identifiers and URLs come from one file (`suw/product.py`) — `scripts/product.py --check`
- [x] Version is consistent everywhere — `scripts/product.py --check`
- [x] Licence file, third-party notices
- [x] The namespace behind the application ID is the repository owner's GitHub account

## Security
- [x] No secrets, private endpoints or personal data in files — `scripts/sanitize.py`
- [x] Clean Git history (a single initial commit; no private history) — `scripts/sanitize.py`
- [ ] gitleaks in CI — first CI run pending
- [ ] Dependency audit, licence scan, SBOM — first CI run pending
- [ ] Signed artifacts — signing identities not configured
- [x] Secure defaults: nothing committed, synced, captured or sent without an explicit choice — tests

## Platforms
- [x] Linux x86_64 — automated tests
- [ ] Linux arm64 · macOS Intel · macOS Apple Silicon · Windows x64 · Windows ARM64 — first CI run pending
- [ ] Real-device acceptance on each claimed platform

## Install
- [ ] Clean install from each package format
- [ ] Upgrade from the previous version
- [ ] Downgrade / rollback
- [ ] Uninstall keeps user data

## Function
- [x] Default Mode changes nothing on the desktop — test
- [x] Work folder boundary — tests
- [x] Pairing: code, confirmation number, merge confirmation, unpair — tests
- [x] Settings: validate, save, snapshot, reset; export/import without secrets — tests
- [x] Recovery: snapshots, rebuild, migration with rollback — tests
- [x] Updates: channel rules, checksum enforcement — tests
- [x] Sync, Git, SSH, Cloud engine — tests against real Syncthing and a local sshd (Linux)
- [ ] Workstation Mode on a real desktop of each platform
- [ ] Keyboard, mouse and clipboard between real computers
- [ ] Home and Cloud against real servers
- [ ] Claude Code project context across two real computers

## Experience
- [x] First-run assistant, every page, both languages, light and dark — driven in a headless browser, no script errors
- [ ] Screen-reader pass on each platform
- [ ] Permissions flow on macOS and Windows

## Documentation
- [x] README, user documentation, FAQ, troubleshooting, security, contribution guide
- [x] Links and required pages — `scripts/check_docs.py`
- [ ] Screenshots from a real build in the README
