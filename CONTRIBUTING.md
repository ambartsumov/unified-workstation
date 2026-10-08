# Contributing

Thank you for considering it. This project optimises for trust, simplicity and reliability
over the number of features — contributions that make it calmer and more honest are the most
welcome kind.

## Before you start

- For anything larger than a fix, open an issue first so we can agree on the shape.
- Security problems go through [SECURITY.md](SECURITY.md), never a public issue.
- Be kind: [Code of Conduct](CODE_OF_CONDUCT.md).

## Setting up

```sh
git clone https://github.com/ambartsumov/unified-workstation
cd unified-workstation
python3 -m pip install --user pytest ruff
make demo      # the window with sample data
make check     # everything CI runs
```

No account, server, private network or secret is needed for any of it. Details:
[docs/development.md](docs/development.md).

## What a good change looks like

- **Small and focused.** One concern per pull request; no unrelated reformatting.
- **Tested.** New behaviour has a test; a bug fix has a test that failed before it.
- **Portable.** No operating-system check in core code — extend the platform adapter.
- **Honest.** If something works only on one platform, the capability matrix and
  [Supported platforms](docs/supported-platforms.md) say so. Never mark a platform as verified
  without evidence.
- **Translated.** User-facing text goes into both catalogs (`suw/resources/i18n/`).
- **Safe.** Anything that changes the user's machine is backed up and reversible; nothing
  deletes user data.
- **Clean.** `make sanitize` passes: no real addresses, names, paths or credentials, not even
  in tests. Use `192.0.2.x`, `example.com`, `workstation-1`.

## Commit messages

Imperative, present tense, with a short scope: `fix(pairing): refuse a code with an invalid port`.
Explain *why* in the body when it is not obvious.

## Reporting platform results

Running the [acceptance procedures](docs/acceptance.md) on your hardware and reporting the
result (the *Platform compatibility* issue form) is one of the most valuable contributions
during the beta.

## Licence

By contributing you agree that your contribution is licensed under the [MIT licence](LICENSE).
