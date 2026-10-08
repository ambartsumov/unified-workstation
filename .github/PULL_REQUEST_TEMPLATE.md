## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was verified

<!-- The command you ran and what it printed. "It should work" is not verification. -->

```
COMMAND   :
RESULT    :
```

## Checklist

- [ ] `make check` passes (tests, lint, public-safety scan, docs)
- [ ] New behaviour has a test; a fix has a test that failed before it
- [ ] No operating-system check added to core code (platform adapter instead)
- [ ] User-facing text is in both translation catalogs
- [ ] Anything that changes the user's machine is backed up and reversible
- [ ] If a platform limitation changed: capability matrix and `docs/supported-platforms.md` updated
- [ ] No real addresses, names, paths or credentials — including in tests and screenshots
- [ ] `CHANGELOG.md` updated for a user-visible change
