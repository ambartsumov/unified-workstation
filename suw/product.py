"""Product identity. The single place every name, identifier and URL comes from.

Installers, package metadata, service labels, the update manifest and the UI all read these
values (packaging/ is generated from them by `scripts/product.py`), so the identifiers below
stay stable across releases. Changing APP_ID after a public release breaks updates and
orphans stored credentials — treat it as a breaking change.
"""

from __future__ import annotations

from . import __version__

NAME = "Unified Workstation"
SHORT = "SUW"
SLUG = "unified-workstation"            # package names, archive names, repository name
CLI = "suw"                             # command name
SUMMARY = "Turn your computers into one coordinated workspace"
DESCRIPTION = (
    "Unified Workstation keeps one work folder identical on every computer you use, lets one "
    "keyboard and mouse move between them, and gives your home and cloud servers a name "
    "instead of an address. Local-first: no account, no central service."
)

# Reverse-DNS identifiers. One namespace for every platform.
NAMESPACE = "io.github.ambartsumov"
APP_ID = f"{NAMESPACE}.UnifiedWorkstation"   # Linux desktop id / Flatpak id / AppStream id
BUNDLE_ID = APP_ID                           # macOS CFBundleIdentifier
WINDOWS_PACKAGE_ID = "UnifiedWorkstation.UnifiedWorkstation"   # winget PackageIdentifier
WINDOWS_APP_USER_MODEL_ID = "UnifiedWorkstation.App"           # notifications, taskbar grouping
WINDOWS_DIR = "UnifiedWorkstation"           # %APPDATA%\UnifiedWorkstation
SERVICE_DAEMON = f"{NAMESPACE}.suwd"         # launchd label; systemd unit is suwd.service
CREDENTIAL_SERVICE = "suw"                   # keychain / Secret Service / Credential Manager service name

HOMEPAGE = "https://github.com/ambartsumov/unified-workstation"
ISSUES = f"{HOMEPAGE}/issues"
RELEASES = f"{HOMEPAGE}/releases"
DOCS = f"{HOMEPAGE}/tree/main/docs"
UPDATE_MANIFEST = "https://github.com/ambartsumov/unified-workstation/releases/latest/download/update-manifest.json"
LICENSE = "MIT"
VENDOR = "Unified Workstation contributors"

VERSION = __version__


def channel(version: str = VERSION) -> str:
    """stable | beta | nightly — derived from the version, never configured separately,
    so a development build can not be labelled Stable by accident (PEP 440)."""
    lowered = version.lower()
    if "dev" in lowered or "+" in lowered:
        return "nightly"
    if any(tag in lowered for tag in ("a", "b", "rc")):
        return "beta"
    return "stable"


def as_dict() -> dict:
    return {
        "name": NAME,
        "short": SHORT,
        "slug": SLUG,
        "cli": CLI,
        "summary": SUMMARY,
        "description": DESCRIPTION,
        "app_id": APP_ID,
        "bundle_id": BUNDLE_ID,
        "windows_package_id": WINDOWS_PACKAGE_ID,
        "homepage": HOMEPAGE,
        "issues": ISSUES,
        "releases": RELEASES,
        "docs": DOCS,
        "license": LICENSE,
        "vendor": VENDOR,
        "version": VERSION,
        "channel": channel(),
    }
