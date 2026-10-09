#!/usr/bin/env python3
"""Public-safety scan. Run before every push and in CI: `make sanitize`.

Looks at every tracked file *and* every blob in the Git history, plus commit metadata, for
things that must never be published:

    SECRETS            tokens, private keys, passwords in assignments
    PRIVATE ENDPOINTS  IP addresses outside the documentation ranges, private-network names
    PERSONAL PATHS     /home/<name>, /Users/<name>, C:\\Users\\<name>
    PERSONAL DATA      e-mail addresses, plus every term in your local deny-list
    PRIVATE HISTORY    commit authors/e-mails that are not on the allow-list
    PRIVATE CONFIG     files that should never be tracked (.env, keys, real inventories)

The deny-list (your own names, hosts, project names) is read from `.sanitize-denylist` in the
repository root or from $SUW_SANITIZE_DENYLIST — one term per line. It is deliberately *not*
part of the repository: a list of private terms would itself be a leak.

Exit status 0 = clean, 1 = findings. `--report FILE` also writes a Markdown report.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECRETS = [
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("Tailscale key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9]{10,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("API key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}")),
    ("password assignment", re.compile(r"""(?i)\b(?:password|passwd|secret|api[_-]?key|auth[_-]?token)\b\s*[:=]\s*["'](?!\s*["'])(?!keychain://)(?!<)(?!\{)(?!\$)[^"'\s]{8,}["']""")),
    ("credentials in URL", re.compile(r"https?://[^/\s:@\"'<>{}$]+:[^/\s@\"'<>{}$]+@")),
]
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
PRIVATE_NAME = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.ts\.net\b", re.I)
HOME_PATH = re.compile(r"""(?:/home/|/Users/|[A-Za-z]:\\Users\\)([A-Za-z0-9._-]+)""")
EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")

# Placeholders that are fine to publish.
OK_NETWORKS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8", "0.0.0.0/32", "100.64.0.0/10", "255.255.255.255/32")]
OK_TS_NAMES = re.compile(r"(?:^|\.)(?:example|tail|tailnet|tail1234|your-tailnet|tail-scale)\.ts\.net$|^[a-z0-9-]+\.ts\.net$", re.I)  # a real name has a tailnet label; `host.ts.net` is a placeholder
OK_USERS = {"user", "username", "me", "you", "name", "demo", "runner", "runneradmin", "alice", "bob", "sam", "shared", "public", "example", "linuxbrew", "your-name", "test"}
OK_EMAIL_DOMAINS = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net|invalid)|users\.noreply\.github\.com|noreply\.github\.com|github\.com|ambartsumov\.github\.io)$", re.I)
OK_EMAIL_LOCAL = re.compile(r"^(?:git|noreply|actions|dependabot|security|conduct)", re.I)
FORBIDDEN_FILES = re.compile(r"(?:^|/)(?:\.env(?:\.(?!example|sample|template|dist)[^/]*)?|id_(?:rsa|dsa|ecdsa|ed25519)|[^/]*\.(?:pem|p12|pfx|kdbx|key)|\.netrc|known_hosts|inventory\.toml|credentials\.json|\.sanitize-denylist)$")
BINARY = re.compile(r"\.(?:png|jpg|jpeg|gif|ico|icns|webp|woff2?|ttf|zip|gz|pdf)$", re.I)
SELF = {"scripts/sanitize.py", "scripts/sanitize-allow.txt"}  # the patterns above would match their own definitions
# Private-range addresses identify nobody; they are accepted as examples in tests and documentation only.
EXAMPLE_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "1.2.3.4/32")]
EXAMPLE_PLACES = ("tests/", "docs/")
# Lines that deliberately show what a secret looks like (redaction/policy tests and fixtures).
FIXTURE_HINT = re.compile(r"redact|policy|not-real|fake|dummy|example|fixture|placeholder|REPLACE|x{8}|Z\" \* |\"x\" \*|'x' \*", re.I)


def git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, errors="replace")
    return done.stdout if done.returncode == 0 else ""


def denylist() -> list[re.Pattern]:
    path = Path(os.environ.get("SUW_SANITIZE_DENYLIST") or ROOT / ".sanitize-denylist")
    if not path.is_file():
        return []
    terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    return [re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.I) for term in terms]


def allowed() -> set[tuple[str, str]]:
    """Reviewed fixtures: `path<TAB>finding` lines in scripts/sanitize-allow.txt. Each entry is a
    deliberately fake secret used to test redaction or refusal; adding one needs a review."""
    path = ROOT / "scripts" / "sanitize-allow.txt"
    if not path.is_file():
        return set()
    return {tuple(line.split("\t", 1)) for line in path.read_text(encoding="utf-8").splitlines() if "\t" in line and not line.startswith("#")}  # type: ignore[misc]


def scan_text(where: str, text: str, deny: list[re.Pattern], found: dict[str, set[str]]) -> None:
    is_test = where.startswith("tests/") or "/tests/" in where
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            line = line[:4000]
        ref = f"{where}:{number}"
        for label, pattern in SECRETS:
            if pattern.search(line) and not ((is_test or "docs/" in where) and FIXTURE_HINT.search(line)) and not (label == "password assignment" and FIXTURE_HINT.search(line)):
                found["SECRETS"].add(f"{ref}  {label}")
        for match in IPV4.finditer(line):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if where.replace("history:", "").split(":")[-1].startswith(EXAMPLE_PLACES) and any(address in network for network in EXAMPLE_NETWORKS):
                continue
            if any(address in network for network in OK_NETWORKS) or re.search(r"(?i)version|v\d|syncthing |deskflow |openssh|\d+\.\d+\.\d+\.\d+\.", line):
                continue
            found["PRIVATE ENDPOINTS"].add(f"{ref}  address {match.group(0)}")
        for match in PRIVATE_NAME.finditer(line):
            if not OK_TS_NAMES.search(match.group(0)):
                found["PRIVATE ENDPOINTS"].add(f"{ref}  private-network name {match.group(0)}")
        for match in HOME_PATH.finditer(line):
            if match.group(1).lower() not in OK_USERS and not match.group(1).startswith(("$", "{", "<", "%")):
                found["PERSONAL PATHS"].add(f"{ref}  {match.group(0)}")
        for match in EMAIL.finditer(line):
            local = match.group(0).split("@", 1)[0]
            if line[max(0, match.start() - 1) : match.start()] == ":" or "://" in line[max(0, match.start() - 40) : match.start()]:
                continue  # the user part of a URL, reported as "credentials in URL" instead
            if not match.group(1).rsplit(".", 1)[-1].isalpha() or re.fullmatch(r"[A-Z_]+", local):
                continue  # `tool@v1.2.3` (a version pin) or an @PLACEHOLDER@ in a template, not an address
            if not OK_EMAIL_DOMAINS.search(match.group(1)) and not OK_EMAIL_LOCAL.search(local) and "." in match.group(1) and not re.search(r"\.(?:service|socket|json|toml|md|py|sh|desktop|plist|conf)$", match.group(1)):
                found["PERSONAL DATA"].add(f"{ref}  e-mail address")
        for pattern in deny:
            if pattern.search(line):
                found["PERSONAL DATA"].add(f"{ref}  deny-listed term")


def scan(history: bool = True) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    deny = denylist()
    tracked = [line for line in git("ls-files").splitlines() if line]
    if not tracked:  # not a repository yet: scan the directory
        tracked = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    for name in tracked:
        if FORBIDDEN_FILES.search(name):
            found["PRIVATE CONFIG"].add(f"{name}  must not be tracked")
        if BINARY.search(name) or name in SELF:
            continue
        try:
            scan_text(name, (ROOT / name).read_text(encoding="utf-8", errors="replace"), deny, found)
        except OSError:
            continue
        for pattern in deny:
            if pattern.search(name):
                found["PERSONAL DATA"].add(f"{name}  deny-listed term in the file name")
    if history:
        seen: set[str] = set()
        for line in git("rev-list", "--all", "--objects").splitlines():
            sha, _, name = line.partition(" ")
            if not name or sha in seen or BINARY.search(name) or name in SELF:
                continue
            seen.add(sha)
            if git("cat-file", "-t", sha).strip() != "blob":
                continue
            if FORBIDDEN_FILES.search(name):
                found["PRIVATE HISTORY"].add(f"history:{name}  a file that must not be tracked exists in an earlier commit")
            before: dict[str, set[str]] = defaultdict(set)
            scan_text(f"history:{sha[:10]}:{name}", git("cat-file", "-p", sha), deny, before)
            for values in before.values():
                for value in values:
                    current = re.sub(r"^history:[0-9a-f]+:", "", value)
                    if not any(current in existing for existing in sum((list(v) for v in found.values()), [])):
                        found["PRIVATE HISTORY"].add(value)
        for line in set(git("log", "--all", "--format=%an <%ae>|%cn <%ce>").replace("|", "\n").splitlines()):
            match = EMAIL.search(line)
            if match and ("commit-identity", match.group(0).lower()) in allowed():
                continue  # the address the repository owner chose to publish under
            if match and not OK_EMAIL_DOMAINS.search(match.group(1)):
                found["PRIVATE HISTORY"].add(f"commit identity uses a personal e-mail address ({line.split('<')[0].strip()[:1]}… <…@{match.group(1)}>)")
            for pattern in deny:
                if pattern.search(line):
                    found["PRIVATE HISTORY"].add("commit identity contains a deny-listed term")
        messages = git("log", "--all", "--format=%H%n%B")
        scan_text("history:commit-messages", messages, deny, found)
    return found


CATEGORIES = ["PRIVATE DATA", "PRIVATE ENDPOINTS", "SECRETS", "PERSONAL PATHS", "PRIVATE HISTORY", "PRIVATE CONFIG"]


def render(found: dict[str, set[str]], deny_terms: int, commits: int) -> str:
    found = {**found, "PRIVATE DATA": found.get("PERSONAL DATA", set())}
    lines = ["# Public sanitization report", "", f"Scanned: tracked files and every blob of {commits} commit(s); deny-list terms loaded: {deny_terms}.", ""]
    for category in CATEGORIES:
        items = sorted(found.get(category, ()))
        lines.append(f"## {category} FOUND: {len(items)}")
        lines += [f"- {item}" for item in items[:200]] or ["- none"]
        lines.append("")
    total = sum(len(found.get(c, ())) for c in CATEGORIES)
    lines.append("**RESULT: CLEAN**" if not total else f"**RESULT: {total} finding(s) — do not publish.**")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-history", action="store_true", help="scan tracked files only")
    parser.add_argument("--report", help="also write the report to this file")
    parser.add_argument("--require-denylist", action="store_true", help="fail when no deny-list is present (use before the first publication)")
    args = parser.parse_args()
    deny_terms = len(denylist())
    if args.require_denylist and not deny_terms:
        print("No deny-list found (.sanitize-denylist). Add your private terms, one per line, and run again.", file=sys.stderr)
        return 1
    found = scan(history=not args.no_history)
    ok = allowed()
    for category in list(found):
        found[category] = {item for item in found[category] if (re.sub(r"^history:[0-9a-f]+:", "", item).split(":", 1)[0], item.split("  ", 1)[-1]) not in ok}
    commits = len(git("rev-list", "--all").split())
    report = render(found, deny_terms, commits)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    print(report)
    return 1 if "RESULT: CLEAN" not in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
