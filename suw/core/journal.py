"""Installer journal: every change SUW makes to the machine is recorded so it can be undone.

`rollback` replays the journal backwards and only ever touches things SUW itself changed.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import events, i18n, paths
from .proc import run

BEGIN = "# >>> suw managed block >>>"
END = "# <<< suw managed block <<<"


def _file() -> Path:
    return paths.state_dir() / "journal.jsonl"


def record(action: str, target: str, **data) -> None:
    paths.ensure(paths.state_dir())
    entry = {"ts": time.time(), "action": action, "target": target, **data}
    with _file().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def entries() -> list[dict]:
    try:
        return [json.loads(line) for line in _file().read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def _already(action: str, target: str) -> bool:
    return any(e["action"] == action and e["target"] == target for e in entries())


# ── files ───────────────────────────────────────────────────────────────────


def backup_file(path: Path, restore: bool = False) -> Path | None:
    """Copy a file aside before its first modification (once per file).

    `restore=True` marks files SUW replaced wholesale: rollback copies the backup back.
    Files changed surgically (a managed block, one git config key) are undone surgically
    instead, so edits the user made in the meantime survive a rollback.
    """
    if not path.exists() or _already("backup", str(path)):
        return None
    dest = paths.ensure(paths.state_dir() / "backups") / (path.name.lstrip(".") + f".{int(time.time())}.bak")
    shutil.copy2(path, dest)
    dest.chmod(0o600)
    record("backup", str(path), backup=str(dest), restore=restore, sha256=_sha256(dest), owner="suw")
    return dest


def _sha256(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def manifest() -> list[dict]:
    """Backup manifest: original file, backup path, checksum, timestamp, ownership."""
    return [
        {"original": e["target"], "backup": e["backup"], "sha256": e.get("sha256", ""), "timestamp": e["ts"], "owner": e.get("owner", "suw")}
        for e in entries()
        if e["action"] == "backup"
    ]


def verify_backups() -> list[str]:
    """Originals whose saved backup no longer matches the checksum recorded for it."""
    return [m["original"] for m in manifest() if m["sha256"] and Path(m["backup"]).exists() and _sha256(Path(m["backup"])) != m["sha256"]]


def owned() -> dict[str, list[str]]:
    """What SUW owns on this machine, grouped for the uninstall preview."""
    out: dict[str, list[str]] = {"files": [], "blocks": [], "settings": [], "services": []}
    for e in entries():
        bucket = {"create": "files", "block": "blocks", "gsettings": "settings", "gsettings-reset": "settings", "service": "services", "gitconfig": "settings"}.get(e["action"])
        if bucket and e["target"] not in out[bucket]:
            out[bucket].append(e["target"])
    return out


def write_file(path: Path, text: str, mode: int = 0o644) -> bool:
    """Create or replace a file SUW owns entirely. Returns True when content changed."""
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        return False
    existed = path.exists()
    if existed:
        backup_file(path, restore=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    if not existed and not _already("create", str(path)):
        record("create", str(path))
    return True


def symlink(link: Path, target: Path) -> bool:
    if link.is_symlink() and link.resolve() == target.resolve():
        return False
    if link.exists() or link.is_symlink():
        if not link.is_symlink():
            backup_file(link, restore=True)
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    if not _already("create", str(link)):
        record("create", str(link))
    return True


def managed_block(path: Path, body: str, *, top: bool = False, comment: str = "#") -> bool:
    """Insert or refresh the single SUW-owned block in a user file; the rest is untouched."""
    begin, end = BEGIN.replace("#", comment, 1), END.replace("#", comment, 1)
    block = f"{begin}\n{body.rstrip()}\n{end}\n"
    old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    new = strip_block(old, comment)
    if top:
        new = block + ("\n" + new.lstrip("\n") if new.strip() else "")
    else:
        new = (new.rstrip("\n") + "\n\n" if new.strip() else "") + block
    if new == old:
        return False
    if path.exists():
        backup_file(path)
        mode = path.stat().st_mode & 0o777
    else:
        mode = 0o644
        record("create", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    path.chmod(mode)
    if not _already("block", str(path)):
        record("block", str(path), comment=comment)
    return True


def strip_block(text: str, comment: str = "#") -> str:
    begin, end = BEGIN.replace("#", comment, 1), END.replace("#", comment, 1)
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == begin:
            skipping = True
            continue
        if line.rstrip("\n") == end:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out)


# ── settings ────────────────────────────────────────────────────────────────


def gsettings_get(schema: str, key: str, path: str = "") -> str | None:
    target = f"{schema}:{path}" if path else schema
    res = run(["gsettings", "get", target, key], timeout=5)
    return res.out.strip() if res.ok else None


def gsettings_set(schema: str, key: str, value: str, path: str = "", journal: bool = True) -> bool:
    target = f"{schema}:{path}" if path else schema
    old = gsettings_get(schema, key, path)
    if old is None:
        return False
    if old == value:
        return True
    ident = f"{target} {key}"
    if journal and not _already("gsettings", ident):
        record("gsettings", ident, schema=target, key=key, old=old)
    return run(["gsettings", "set", target, key, value], timeout=5).ok


# ── rollback ────────────────────────────────────────────────────────────────


def rollback(dry_run: bool = False) -> list[str]:
    """Undo journaled changes, newest first. Returns human-readable lines."""
    done: list[str] = []
    backups = {e["target"]: e["backup"] for e in entries() if e["action"] == "backup" and e.get("restore")}
    for entry in reversed(entries()):
        action, target = entry["action"], entry["target"]
        path = Path(target)
        if action == "block":
            if path.exists():
                done.append(i18n.msg("journal.undo.block", target=target))
                if not dry_run:
                    text = strip_block(path.read_text(encoding="utf-8", errors="replace"), entry.get("comment", "#"))
                    path.write_text(text.rstrip("\n") + "\n" if text.strip() else "", encoding="utf-8")
        elif action == "create":
            if path.is_symlink() or path.exists():
                if path.is_file() and not path.is_symlink() and path.read_text(errors="replace").strip() and target in {
                    e["target"] for e in entries() if e["action"] == "block"
                }:
                    continue  # user content remains in a file we first created: keep it
                done.append(i18n.msg("journal.undo.remove", target=target))
                if not dry_run:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
        elif action == "gsettings":
            done.append(i18n.msg("journal.undo.gsettings", target=target))
            if not dry_run:
                run(["gsettings", "set", entry["schema"], entry["key"], entry["old"]], timeout=5)
        elif action == "gsettings-reset":
            done.append(i18n.msg("journal.undo.shortcut", target=target))
            if not dry_run:
                run(["gsettings", "reset-recursively", entry["schema"]], timeout=5)
        elif action == "service":
            done.append(i18n.msg("journal.undo.service", target=target))
            if not dry_run:
                run(entry["undo"], timeout=20)
        elif action == "gitconfig":
            done.append(i18n.msg("journal.undo.gitconfig", target=target))
            if not dry_run:
                run(["git", "config", "--global", "--unset-all", entry["key"], entry["pattern"]], timeout=5)
    for target, backup in backups.items():
        path = Path(target)
        if not Path(backup).exists():
            continue
        done.append(i18n.msg("journal.undo.backup", target=target))
        if not dry_run:
            shutil.copy2(backup, path)
    if not dry_run:
        archive = _file().with_suffix(f".jsonl.rolledback.{int(time.time())}")
        if _file().exists():
            _file().replace(archive)
        events.emit("install.rollback", f"rolled back {len(done)} change(s)")
    return done
