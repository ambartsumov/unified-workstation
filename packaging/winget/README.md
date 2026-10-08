# winget manifest

`scripts/product.py --render` writes the three manifest files for the current version to
`packaging/winget/generated/`. The installer URL and its SHA-256 are filled in by the release
workflow once the signed installer exists; the result is submitted to
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) by a maintainer (a pull
request to a third-party repository is a manual publishing step).
