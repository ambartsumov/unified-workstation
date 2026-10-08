"""Handoff bundle: everything a brand-new workstation needs, in one file.

Contains git bundles of this repository and of the shared configuration (inventory, URLs,
layout) plus a double-clickable installer. No secrets: keys and tokens are re-created on the
new machine through its own keychain and `gh auth login`.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from . import events, gitsync, paths
from .config import Config

INSTALLER = """#!/bin/bash
# Unified Workstation - new-device installer. Double-click (macOS) or run with bash.
set -euo pipefail
cd "$(dirname "$0")"
REPO_NAME="@REPO_NAME@"
REPO_REMOTE="@REPO_REMOTE@"
SHARED_REMOTE="@SHARED_REMOTE@"
DEST="$HOME/Projects/active/$REPO_NAME"
SHARED="$HOME/.config/suw/shared"

if ! command -v git >/dev/null 2>&1; then
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "Installing Apple command line tools (a dialog will open). Re-run this file when it finishes."
        xcode-select --install || true
        exit 1
    fi
    echo "Please install git first (sudo apt install git)."; exit 1
fi

mkdir -p "$HOME/Projects/active" "$HOME/.config/suw"
chmod 700 "$HOME/.config/suw"
if [ ! -d "$DEST/.git" ]; then
    git clone -q repo.bundle "$DEST"
    if [ -n "$REPO_REMOTE" ]; then git -C "$DEST" remote set-url origin "$REPO_REMOTE"; else git -C "$DEST" remote remove origin; fi
fi
if [ ! -d "$SHARED/.git" ]; then
    git clone -q shared.bundle "$SHARED"
    if [ -n "$SHARED_REMOTE" ]; then git -C "$SHARED" remote set-url origin "$SHARED_REMOTE"; else git -C "$SHARED" remote remove origin; fi
fi
exec bash "$DEST/install.sh"
"""


def _bundle(repo: Path, dest: Path) -> bool:
    if not (repo / ".git").exists() or not gitsync.git(repo, "rev-parse", "HEAD").ok:
        return False
    return gitsync.git(repo, "bundle", "create", str(dest), "--all", timeout=120).ok


def _remote(repo: Path) -> str:
    url = gitsync.git(repo, "remote", "get-url", "origin").out.strip()
    # A remote with an embedded token must never travel in the bundle.
    return "" if gitsync.url_has_credentials(url) or "@" in url.split("://", 1)[-1].split("/", 1)[0] and url.startswith("http") else url


def build(cfg: Config, output: Path | None = None) -> Path:
    repo = paths.REPO_ROOT
    stamp = datetime.now().strftime("%Y%m%d")
    output = output or paths.home() / f"suw-handoff-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "suw-handoff"
        root.mkdir()
        if not _bundle(repo, root / "repo.bundle"):
            raise SystemExit("error: the SUW repository has no commits yet; commit it first")
        if not _bundle(paths.shared_dir(), root / "shared.bundle"):
            raise SystemExit("error: shared configuration is not initialised; run `suw bootstrap` first")
        script = (
            INSTALLER.replace("@REPO_NAME@", repo.name)
            .replace("@REPO_REMOTE@", _remote(repo))
            .replace("@SHARED_REMOTE@", _remote(paths.shared_dir()))
        )
        installer = root / "Install Workstation.command"
        installer.write_text(script)
        installer.chmod(0o755)
        day_one = repo / "docs" / "installation.md"
        if day_one.exists():
            shutil.copyfile(day_one, root / "READ ME FIRST.md")
        with tarfile.open(output, "w:gz") as archive:
            archive.add(root, arcname="suw-handoff")
    output.chmod(0o600)
    events.emit("handoff", f"handoff bundle written: {output.name}")
    return output
