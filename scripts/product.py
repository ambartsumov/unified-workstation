#!/usr/bin/env python3
"""Product metadata for packaging: one source (`suw/product.py`), every format rendered from it.

    scripts/product.py --get VERSION        print one value (used by build scripts)
    scripts/product.py --render             write packaging/*/generated/ from the *.in templates
    scripts/product.py --check              fail if any version or identifier is out of step

`--check` is what keeps "no mismatched versions" true: the application, the installers, the
package metadata, the changelog and the documentation must all name the same version.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from suw import product  # noqa: E402


def windows_version(version: str) -> str:
    """MSIX needs four numeric parts; pre-releases are told apart by the last one."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?", version)
    major, minor, patch, pre, pre_n, dev = match.groups()  # type: ignore[union-attr]
    build = 9000 if not pre and dev is None else {"a": 1000, "b": 2000, "rc": 3000}.get(pre or "", 0) + int(pre_n or dev or 0)
    return f"{major}.{minor}.{patch or 0}.{build}"


def values() -> dict[str, str]:
    channel = product.channel()
    return {
        "NAME": product.NAME, "SHORT": product.SHORT, "SLUG": product.SLUG, "SUMMARY": product.SUMMARY, "DESCRIPTION": product.DESCRIPTION,
        "APP_ID": product.APP_ID, "BUNDLE_ID": product.BUNDLE_ID, "WINDOWS_PACKAGE_ID": product.WINDOWS_PACKAGE_ID,
        "WINDOWS_APP_USER_MODEL_ID": product.WINDOWS_APP_USER_MODEL_ID, "WINDOWS_DIR": product.WINDOWS_DIR,
        "HOMEPAGE": product.HOMEPAGE, "ISSUES": product.ISSUES, "RELEASES": product.RELEASES, "DOCS": product.DOCS, "VENDOR": product.VENDOR,
        "VERSION": product.VERSION, "WINDOWS_VERSION": windows_version(product.VERSION), "CHANNEL": channel,
        "RELEASE_TYPE": "stable" if channel == "stable" else "development", "DATE": datetime.date.today().isoformat(),
    }  # fmt: skip


def render() -> list[Path]:
    table = values()
    written = []
    for template in sorted((ROOT / "packaging").rglob("*.in")):
        text = template.read_text(encoding="utf-8")
        for key, value in table.items():
            text = text.replace(f"@{key}@", value)
        left = re.findall(r"@[A-Z_]+@", text)
        if left:
            raise SystemExit(f"{template}: unknown placeholder(s) {sorted(set(left))}")
        name = template.name[: -len(".in")].replace("@APP_ID@", product.APP_ID)
        target = template.parent / "generated" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def check() -> list[str]:
    problems = []
    version = product.VERSION
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.dev\d+)?", version):
        problems.append(f"version '{version}' is not MAJOR.MINOR.PATCH[a|b|rcN][.devN]")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog and product.channel() != "nightly":
        problems.append(f"CHANGELOG.md has no section for {version}")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'version = { attr = "suw.__version__" }' not in pyproject:
        problems.append("pyproject.toml must take its version from suw.__version__")
    for label, needle in (("homepage", product.HOMEPAGE), ("issues", product.ISSUES)):
        if needle not in pyproject:
            problems.append(f"pyproject.toml {label} differs from suw/product.py")
    for path in list((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]:
        for found in re.findall(r"unified-workstation-(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)", path.read_text(encoding="utf-8")):
            if found != version:
                problems.append(f"{path.relative_to(ROOT)} names version {found}, the product is {version}")
    for source in list((ROOT / "suw").rglob("*.py")) + list((ROOT / "packaging").rglob("*.in")):
        text = source.read_text(encoding="utf-8")
        if source.name != "product.py" and re.search(r"io\.github\.(?!" + re.escape(product.NAMESPACE.rsplit(".", 1)[-1]) + r")[a-z0-9_-]+\.", text):
            problems.append(f"{source.relative_to(ROOT)} hard-codes an application identifier; use suw/product.py")
    try:
        render()
    except SystemExit as exc:
        problems.append(str(exc))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--get")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.get:
        print(values()[args.get])
        return 0
    if args.render:
        for path in render():
            print(path.relative_to(ROOT))
        return 0
    problems = check()
    for problem in problems:
        print(f"✕ {problem}")
    print("product metadata: " + ("consistent" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
