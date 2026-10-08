#!/usr/bin/env python3
"""Documentation checks for CI: every relative link resolves, every required page exists, and
every `suw …` command shown in the user documentation is a real command."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REQUIRED = ["getting-started", "installation", "first-run", "workspace", "sync", "workstations", "peripherals", "clipboard", "claude-code", "home-server", "cloud", "ssh", "security", "recovery", "troubleshooting", "updates", "uninstall", "privacy", "faq", "supported-platforms"]
ROOT_FILES = ["README.md", "LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md", "CHANGELOG.md", "ROADMAP.md", "THIRD_PARTY_NOTICES.md"]
LINK = re.compile(r"(?<!\!)\[[^\]]+\]\(([^)\s]+)\)")
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def anchors(path: Path) -> set[str]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            out.add(re.sub(r"[^\w\- ]", "", title).replace(" ", "-"))
    return out


def main() -> int:
    problems = []
    for name in ROOT_FILES:
        if not (ROOT / name).is_file():
            problems.append(f"missing {name}")
    for name in REQUIRED:
        if not (ROOT / "docs" / f"{name}.md").is_file():
            problems.append(f"missing docs/{name}.md")
    pages = [ROOT / n for n in ROOT_FILES if n.endswith(".md") and (ROOT / n).exists()] + sorted((ROOT / "docs").rglob("*.md")) + sorted((ROOT / ".github").rglob("*.md"))
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for target in LINK.findall(text) + IMAGE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file, _, anchor = target.partition("#")
            destination = (page.parent / file).resolve() if file else page
            if not destination.exists():
                problems.append(f"{page.relative_to(ROOT)}: broken link {target}")
            elif anchor and destination.suffix == ".md" and anchor not in anchors(destination):
                problems.append(f"{page.relative_to(ROOT)}: no heading for {target}")
    from suw.cli.main import build_parser

    known = set(next(a for a in build_parser()._actions if a.dest == "command" or getattr(a, "choices", None)).choices)  # type: ignore[union-attr]
    for page in [p for p in pages if "advanced" not in p.parts]:
        for command in re.findall(r"(?:^|[`\s])suw ([a-z][a-z-]+)", page.read_text(encoding="utf-8"), flags=re.M):
            if command not in known:
                problems.append(f"{page.relative_to(ROOT)}: `suw {command}` is not a command")
    for problem in sorted(set(problems)):
        print(f"✕ {problem}")
    print(f"documentation: {len(pages)} pages, " + ("all links resolve" if not problems else f"{len(set(problems))} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
