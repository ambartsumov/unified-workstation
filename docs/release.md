# Release process

For maintainers. Releases are built, signed and assembled by `.github/workflows/release.yml`;
the workflow ends with a **draft** release that a maintainer reviews and publishes.

## Versioning

`MAJOR.MINOR.PATCH`, with pre-releases `aN` / `bN` / `rcN` and development builds `.devN`.
The single source is `suw/__init__.py`. Everything else — package metadata, installers,
AppStream, MSIX, winget, the update manifest — is rendered from it by `scripts/product.py`, and
`scripts/product.py --check` (part of `make check`) fails on any mismatch.

| Version | Channel | GitHub release |
|---|---|---|
| `1.2.0` | stable | release |
| `1.3.0b1`, `1.3.0rc1` | beta | pre-release |
| `1.3.0.dev4` | nightly | pre-release |

The channel is computed from the version (`product.channel`). The updater refuses a release
whose declared channel is more stable than its version says.

## Cutting a release

1. Update `suw/__init__.py` and add a `## [version]` section to `CHANGELOG.md`.
2. `make check` and the [release checklist](release-checklist.md).
3. Tag `vX.Y.Z` and push the tag.
4. The workflow verifies the tag, runs every check, builds per platform and architecture,
   signs, creates the SBOM, checksums, provenance attestations and `update-manifest.json`, and
   opens a draft release.
5. Run the [acceptance procedures](acceptance.md) against the draft's artifacts.
6. Publish the draft. Submit the winget manifest (`packaging/winget/generated/`).

## Signing identities (repository secrets)

No signing key is ever committed, and no secret is printed in a log. The macOS and Windows
jobs run only after you add their secrets **and** set the repository variable
`SIGNING_MACOS` / `SIGNING_WINDOWS` to `true`; from then on a missing identity **fails** the job
rather than producing an unsigned build. Until then a release carries the Linux packages only.

| Platform | Secrets | Notes |
|---|---|---|
| macOS | `MACOS_CERTIFICATE_P12`, `MACOS_CERTIFICATE_PASSWORD`, `MACOS_DEVELOPER_ID`, `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | Developer ID Application certificate; notarization with `notarytool`; stapled |
| Windows | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_SIGNING_PROFILE` | Azure Artifact Signing; application files and installer are both signed and verified |
| Linux | none | keyless build provenance (`actions/attest-build-provenance`) |

## Supply chain

- Runtime dependencies: none. Build dependencies are audited (`pip-audit`), licence-scanned
  and listed in a CycloneDX SBOM attached to every release.
- GitHub Actions are updated by Dependabot. Pinning them to commit SHAs is on the
  [roadmap](../ROADMAP.md).
- `scripts/sanitize.py` and gitleaks run on every push and before every release.

## Identifiers

Defined once in `suw/product.py`. The application ID `io.github.ambartsumov.…` is tied to the
GitHub account that owns this repository. Changing an identifier after a public release breaks
updates and orphans stored credentials; a fork that ships its own builds must use its own
namespace.
