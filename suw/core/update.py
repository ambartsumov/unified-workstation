"""Updates: check, download, verify — install is always the user's decision.

Trust chain
    1. The update manifest is fetched over HTTPS from the project's release page.
    2. The manifest names one artifact per platform/architecture with its SHA-256.
    3. A downloaded artifact is used only if its SHA-256 matches. A mismatch deletes the file.
    4. The artifact itself is a signed installer; the operating system verifies that signature
       when it is opened (Gatekeeper/notarization on macOS, Authenticode on Windows, the
       package manager's signature on Linux).

Channels: `stable`, `beta`, `nightly`. A build only ever offers updates from its own channel
or a more conservative one, and never a version older than the one running, so a development
build can not replace Stable by accident and a downgrade can not be pushed.

Installs that a package manager owns (Flatpak, apt, dnf, Homebrew, winget, MSIX) are updated
by that package manager; this module then only reports that a newer version exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as pyplatform
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import product
from . import events, paths, state
from .config import Config

CHANNELS = ["stable", "beta", "nightly"]
_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?")
MAX_MANIFEST = 512 * 1024
MAX_ARTIFACT = 600 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


@dataclass
class Offer:
    version: str
    channel: str
    notes: str = ""
    url: str = ""
    sha256: str = ""
    size: int = 0
    filename: str = ""
    managed_by: str = ""      # non-empty: a package manager installs it, not this application
    published: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def parse(version: str) -> tuple:
    """Sort key for PEP 440-style versions: 1.2.0.dev1 < 1.2.0a1 < 1.2.0b1 < 1.2.0rc1 < 1.2.0."""
    match = _VERSION.match(version.strip().lstrip("v"))
    if not match:
        return (0, 0, 0, -1, 0, 0)
    major, minor, patch, pre, pre_n, dev = match.groups()
    stage = {"a": 0, "b": 1, "rc": 2}.get(pre or "", 3)
    if dev is not None and pre is None:
        stage = -1  # a bare development build precedes every pre-release of that version
    return (int(major), int(minor), int(patch or 0), stage, int(pre_n or 0), 0 if dev is None else -1, int(dev or 0))


def newer(candidate: str, current: str = product.VERSION) -> bool:
    return parse(candidate) > parse(current)


def allowed_channels(configured: str) -> list[str]:
    """`beta` users also get stable releases; `stable` users get stable only."""
    wanted = configured if configured in CHANNELS else "stable"
    return CHANNELS[: CHANNELS.index(wanted) + 1]


def arch() -> str:
    machine = pyplatform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def install_kind() -> str:
    """How this copy was installed — decides who is responsible for updating it."""
    forced = os.environ.get("SUW_INSTALL_KIND", "")
    if forced:
        return forced
    if os.environ.get("FLATPAK_ID"):
        return "flatpak"
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if not paths.frozen():
        return "source"
    exe = str(Path(sys.executable).resolve()).lower()
    system = paths.platform()
    if system == "windows":
        return "msix" if "windowsapps" in exe else "installer"
    if system == "macos":
        return "homebrew" if "/caskroom/" in exe or "/homebrew/" in exe else "dmg"
    return "package" if exe.startswith(("/usr/", "/opt/")) else "appimage"


_MANAGED = {
    "flatpak": "Flatpak (flatpak update, or your software centre)",
    "package": "your system's package manager",
    "homebrew": "Homebrew (brew upgrade)",
    "msix": "Microsoft Store / App Installer",
    "source": "git (this is a source checkout)",
}


def _fetch(url: str, limit: int, timeout: float = 15) -> bytes:
    if not url.lower().startswith("https://") and not os.environ.get("SUW_UPDATE_ALLOW_INSECURE"):
        raise UpdateError("Updates are only fetched over HTTPS.")
    request = urllib.request.Request(url, headers={"User-Agent": f"{product.SLUG}/{product.VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            data = reply.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError("The update server could not be reached. Check your connection and try again.") from exc
    if len(data) > limit:
        raise UpdateError("The update server sent more data than expected; nothing was used.")
    return data


def select(manifest: dict, channel: str = "stable", current: str = product.VERSION, system: str | None = None, machine: str | None = None, kind: str | None = None) -> Offer | None:
    """Pick the newest release this installation may take. Pure."""
    system, machine, kind = system or paths.platform(), machine or arch(), kind or install_kind()
    best: Offer | None = None
    for release in manifest.get("releases", []):
        if not isinstance(release, dict):
            continue
        version, release_channel = str(release.get("version", "")), str(release.get("channel", ""))
        if release_channel not in allowed_channels(channel) or product.channel(version) != release_channel:
            continue  # a release labelled more stable than its version says is refused
        if not newer(version, current) or (best and not newer(version, best.version)):
            continue
        offer = Offer(version, release_channel, str(release.get("notes", ""))[:4000], published=str(release.get("published", "")))
        if kind in _MANAGED:
            offer.managed_by = _MANAGED[kind]
        else:
            artifact = next((a for a in release.get("artifacts", []) if a.get("os") == system and a.get("arch") in (machine, "universal", "any") and a.get("kind", kind) == kind), None)
            if artifact is None:
                continue  # nothing published for this computer in that release
            sha = str(artifact.get("sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha) or not str(artifact.get("url", "")).startswith("https://"):
                continue
            offer.url, offer.sha256, offer.size = str(artifact["url"]), sha, int(artifact.get("size", 0))
            offer.filename = Path(str(artifact["url"]).split("?", 1)[0]).name
        best = offer
    return best


def check(cfg: Config, url: str | None = None) -> dict:
    """Ask the release page. Never raises: the result says what happened."""
    result: dict = {"current": product.VERSION, "channel": str(cfg.get("updates.channel", "stable")), "install_kind": install_kind(), "checked": time.time(), "offer": None, "error": ""}
    try:
        manifest = json.loads(_fetch(url or product.UPDATE_MANIFEST, MAX_MANIFEST))
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
        offer = select(manifest, result["channel"])
        result["offer"] = offer.as_dict() if offer else None
    except UpdateError as exc:
        result["error"] = str(exc)
    except ValueError:
        result["error"] = "The update information could not be read; nothing was changed."
    state.save("update", result)
    return result


def last() -> dict:
    return state.load("update", {}) or {}


def downloads() -> Path:
    return paths.ensure(paths.cache_dir() / "updates")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(offer: Offer) -> Path:
    """Download and verify. Returns the verified file; raises and deletes it on any mismatch."""
    if offer.managed_by:
        raise UpdateError(f"This installation is updated by {offer.managed_by}.")
    if not offer.url or not offer.sha256:
        raise UpdateError("No download is published for this computer.")
    if offer.size and offer.size > MAX_ARTIFACT:
        raise UpdateError("The update is larger than this version accepts.")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", offer.filename) or f"{product.SLUG}-{offer.version}"
    target = downloads() / name
    if target.exists() and sha256(target) == offer.sha256:
        return target
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(_fetch(offer.url, MAX_ARTIFACT, timeout=120))
    actual = sha256(partial)
    if actual != offer.sha256:
        partial.unlink(missing_ok=True)
        events.emit("update.rejected", f"update {offer.version} failed its integrity check and was deleted", "error")
        raise UpdateError("The downloaded update did not match its published checksum and was deleted. Nothing was installed.")
    os.replace(partial, target)
    events.emit("update.downloaded", f"update {offer.version} downloaded and verified")
    return target


def previous_installers() -> list[dict]:
    """Verified installers kept on disk: the way back to an earlier version without Internet."""
    out = []
    for path in sorted(downloads().glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and not path.name.endswith(".part"):
            out.append({"name": path.name, "bytes": path.stat().st_size, "modified": path.stat().st_mtime})
    return out
