# Updates

## Channels

| Channel | For | Version looks like |
|---|---|---|
| **Stable** | everyone | `1.2.0` |
| **Beta** | people who want to try the next release early | `1.3.0b1`, `1.3.0rc1` |
| **Development builds** | contributors | `1.3.0.dev4` |

The channel a build belongs to is derived from its version, so a development build can not be
published or installed as Stable. The channel you are running is shown at the bottom of the
navigation and under **Settings → Updates**. A Beta user also receives Stable releases; a
Stable user receives Stable only. An update is never a downgrade.

## How an update happens

1. The application reads the public update manifest from the release page (HTTPS).
2. **Download and verify** fetches the installer and compares its SHA-256 with the published
   one. A mismatch deletes the file and nothing is installed.
3. You open the installer. Your operating system verifies its signature (Developer ID and
   notarization on macOS, Authenticode on Windows, the package signature on Linux).

Nothing is installed without you, and your settings are never replaced: they are migrated,
with a snapshot first and an automatic rollback if migration fails.

## Installed through a package manager?

Flatpak, DEB/RPM repositories, Homebrew, winget and MSIX update the application themselves. The
application then only tells you that a newer version exists.

## Going back

Verified installers are kept under **Recovery → Earlier application versions**.

## Verifying a download yourself

Every release carries `SHA256SUMS`, a CycloneDX SBOM and a build provenance attestation:

```sh
sha256sum --check SHA256SUMS --ignore-missing
gh attestation verify <file> --repo ambartsumov/unified-workstation
```
