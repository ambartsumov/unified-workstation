"""What an automatic checkpoint may stage (pure policy, no git calls).

Three layers never share one mechanism: source goes to Git, large artifacts go to the
artifact store, secrets go to the keychain. This module decides, per changed file, which
of those it is. Nothing here deletes or rewrites a file — a file that is not allowed into
a checkpoint simply stays where it is, uncommitted, and the user is told why.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import state

DENY = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json", "service-account*.json", "*.kdbx", ".netrc", ".npmrc", ".pypirc",
]
ALLOW = [".env.example", ".env.sample", ".env.template", ".env.dist"]
ARTIFACT_GLOBS = ["*.ckpt", "*.safetensors", "*.pt", "*.pth", "*.onnx", "*.gguf", "*.h5", "*.npz", "*.parquet", "*.tar", "*.tar.gz", "*.tgz", "*.zip", "*.7z"]
INLINE_ALLOW = "suw:allow-secret"
SCAN_MAX_BYTES = 1_000_000

SECRET_RULES: list[tuple[str, re.Pattern]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Tailscale key", re.compile(r"\btskey-[A-Za-z0-9-]{20,}")),
    ("credentials in URL", re.compile(r"https?://[^/\s:@]+:[^/\s@]{6,}@")),
    ("password assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]")),
]
_PLACEHOLDER = re.compile(r"(?i)example|changeme|change_me|placeholder|your[_-]|xxxx|\*\*\*|<[^>]+>|\$\{|\{\{|dummy|redacted|not[_-]?set")


@dataclass
class Held:
    """A changed file that was kept out of a checkpoint, and why."""

    path: str
    reason: str  # secret-name | secret-content | oversized | artifact
    detail: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason, "detail": self.detail}


def _match(name: str, patterns: list[str]) -> bool:
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(name, p) for p in patterns)


def sensitive_name(name: str, deny: list[str], allow: list[str]) -> bool:
    return not _match(name, allow) and _match(name, deny)


def under(name: str, prefixes: list[str]) -> bool:
    return any(name == p.rstrip("/") or name.startswith(p.rstrip("/") + "/") for p in prefixes if p.strip("/"))


def scan_text(text: str) -> list[tuple[str, int]]:
    """(rule, line number) for every likely secret. Pure."""
    hits: list[tuple[str, int]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if INLINE_ALLOW in line:
            continue
        for rule, pattern in SECRET_RULES:
            match = pattern.search(line)
            if not match:
                continue
            if rule == "password assignment" and _PLACEHOLDER.search(match.group(2)):
                continue
            hits.append((rule, number))
            break
    return hits


def file_digest(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_file(file: Path) -> list[tuple[str, int]]:
    try:
        if file.is_symlink() or not file.is_file() or file.stat().st_size > SCAN_MAX_BYTES:
            return []
        raw = file.read_bytes()
    except OSError:
        return []
    if b"\0" in raw[:8192]:
        return []
    return scan_text(raw.decode("utf-8", "replace"))


# ── allow-once (false-positive handling) ────────────────────────────────────


def allow_once(root: Path, name: str) -> bool:
    """Let exactly this content of `name` through the secret scan one time."""
    file = root / name
    if not file.is_file():
        return False
    memo = state.load("policy_allow")
    memo.setdefault(str(root), {})[name] = file_digest(file)
    state.save("policy_allow", memo)
    return True


def _allowed_once(root: Path, name: str, consume: bool) -> bool:
    memo = state.load("policy_allow")
    wanted = memo.get(str(root), {}).get(name)
    if not wanted:
        return False
    try:
        if file_digest(root / name) != wanted:
            return False
    except OSError:
        return False
    if consume:
        del memo[str(root)][name]
        state.save("policy_allow", memo)
    return True


# ── the plan ────────────────────────────────────────────────────────────────


def plan(root: Path, files: list[str], untracked: set[str], policy: dict, *, consume: bool = False) -> tuple[list[str], list[Held]]:
    """Split changed files into (stage, held).

    Size and artifact rules apply to *untracked* files only: a file the user already
    committed by hand is their decision. Secret rules apply to everything.
    """
    stage: list[str] = []
    held: list[Held] = []
    limit = float(policy.get("max_file_mb", 50)) * 1024 * 1024
    artifact_paths = [str(p) for p in policy.get("artifact_paths", [])]
    artifact_globs = [str(p) for p in policy.get("artifact_globs", ARTIFACT_GLOBS)]
    ignore = [str(p) for p in policy.get("secret_ignore", [])]
    for name in files:
        file = root / name
        if under(name, artifact_paths):
            held.append(Held(name, "artifact", "declared artifact path"))
            continue
        if sensitive_name(name, policy.get("deny", DENY), policy.get("allow", ALLOW)):
            held.append(Held(name, "secret-name", "file name is on the deny list"))
            continue
        if name in untracked:
            try:
                size = file.stat().st_size if file.is_file() else 0
            except OSError:
                size = 0
            if size > limit:
                held.append(Held(name, "oversized", f"{size / 1048576:.0f} MB > {policy.get('max_file_mb', 50)} MB"))
                continue
            if _match(name, artifact_globs):
                held.append(Held(name, "artifact", "model/dataset/archive file type"))
                continue
        if not _match(name, ignore):
            hits = scan_file(file)
            if hits and not _allowed_once(root, name, consume):
                rule, line = hits[0]
                held.append(Held(name, "secret-content", f"{rule} at line {line}"))
                continue
        stage.append(name)
    return stage, held


def warn_sized(root: Path, files: list[str], warn_mb: float) -> list[str]:
    limit = warn_mb * 1024 * 1024
    out = []
    for name in files:
        try:
            if (root / name).is_file() and (root / name).stat().st_size > limit:
                out.append(name)
        except OSError:
            pass
    return out


def held_key(held: list[Held]) -> str:
    return hashlib.sha1("\n".join(sorted(f"{h.path}:{h.reason}" for h in held)).encode()).hexdigest()[:16]


def describe(held: list[Held]) -> str:
    if not held:
        return ""
    first = held[0]
    more = f" (+{len(held) - 1} more)" if len(held) > 1 else ""
    words = {"secret-name": "secret file", "secret-content": "possible secret", "oversized": "oversized file", "artifact": "artifact"}
    return f"{words.get(first.reason, first.reason)} not committed: {first.path}{more}"
