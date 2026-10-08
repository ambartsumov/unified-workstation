#!/usr/bin/env python3
"""Checksums and the update manifest for a release.

    scripts/release_manifest.py dist/ --base-url https://…/releases/download/v1.2.3

Writes dist/SHA256SUMS and dist/update-manifest.json. The manifest is what installed copies
read (`suw/core/update.py`): one release entry with an artifact per operating system and
architecture, each with its SHA-256. The channel is derived from the version, never passed in.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from suw import product  # noqa: E402

KINDS = [
    (re.compile(r"-(x86_64|aarch64)\.AppImage$"), "linux", "appimage"),
    (re.compile(r"-macos-(arm64|x86_64)\.dmg$"), "macos", "dmg"),
    (re.compile(r"-windows-(x64|arm64)-setup\.exe$"), "windows", "installer"),
]
ARCH = {"x64": "x86_64", "aarch64": "arm64"}


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    files = sorted(p for p in args.directory.iterdir() if p.is_file() and p.name not in ("SHA256SUMS", "update-manifest.json"))
    if not files:
        print("no artifacts found", file=sys.stderr)
        return 1
    sums, artifacts = [], []
    for path in files:
        sha = digest(path)
        sums.append(f"{sha}  {path.name}")
        if product.VERSION not in path.name and path.suffix not in (".json", ".txt"):
            print(f"✕ {path.name} does not carry version {product.VERSION}", file=sys.stderr)
            return 1
        for pattern, system, kind in KINDS:
            match = pattern.search(path.name)
            if match:
                artifacts.append({"os": system, "arch": ARCH.get(match.group(1), match.group(1)), "kind": kind, "url": f"{args.base_url.rstrip('/')}/{path.name}", "sha256": sha, "size": path.stat().st_size})
    (args.directory / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    manifest = {"schema": 1, "product": product.SLUG, "releases": [{"version": product.VERSION, "channel": product.channel(), "published": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "notes": args.notes, "artifacts": artifacts}]}
    (args.directory / "update-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{len(sums)} checksums, {len(artifacts)} updatable artifact(s), channel {product.channel()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
