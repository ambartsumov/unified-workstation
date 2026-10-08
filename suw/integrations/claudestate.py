"""Claude Code state that should follow the work folder.

What already travels because it lives inside the work folder: `CLAUDE.md`, `.claude/`
(settings, commands, agents, skills, rules), `.mcp.json`.

What does not, by Claude Code's own design: auto memory. It is kept per machine in
`~/.claude/projects/<project>/memory/`. Claude Code lets a project move it with the
`autoMemoryDirectory` setting; `share` points it at `<project>/.claude/memory`, written as a
`~/…` path so the same line is right on every computer.

What stays on each machine on purpose: sign-in credentials, and session transcripts
(`claude --resume` lists the sessions started on that machine; they are stored under the
machine's own absolute paths).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from ..core import paths
from ..core.config import Config
from . import worksync

KEY = "autoMemoryDirectory"


class ClaudeStateError(RuntimeError):
    pass


def projects(cfg: Config) -> list[Path]:
    """The work folder itself plus every top-level folder that is its own Git repository
    (Claude Code keeps one memory per repository)."""
    base = worksync.root(cfg)
    if not base.is_dir():
        return []
    return [base] + sorted(entry for entry in base.iterdir() if entry.is_dir() and not entry.is_symlink() and (entry / ".git").exists())


def locate(cfg: Config, name: str) -> Path:
    base = worksync.root(cfg).resolve()
    if name in ("", ".", "work"):
        return base
    target = (base / name).resolve()
    if base not in target.parents or not target.is_dir():
        raise ClaudeStateError(f"'{name}' is not a folder inside {base}")
    return target


def portable(path: Path) -> str:
    """`~/Desktop/Work/x/.claude/memory`: identical text on every workstation."""
    try:
        return "~/" + path.resolve().relative_to(paths.home().resolve()).as_posix()
    except ValueError:
        return str(path)


def local_memory(project: Path) -> Path:
    """Where Claude Code keeps this project's memory when nothing is configured."""
    return paths.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve())) / "memory"


def _settings(project: Path) -> tuple[Path, dict]:
    file = project / ".claude" / "settings.json"
    if not file.exists():
        return file, {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ClaudeStateError(f"{file} is not valid JSON ({exc}); it was left untouched") from exc
    if not isinstance(data, dict):
        raise ClaudeStateError(f"{file} does not hold a settings object; it was left untouched")
    return file, data


def row(project: Path) -> dict:
    shared_dir = project / ".claude" / "memory"
    try:
        _file, data = _settings(project)
        configured = str(data.get(KEY, ""))
        error = ""
    except ClaudeStateError as exc:
        configured, error = "", str(exc)
    local = local_memory(project)
    return {
        "project": project.name,
        "path": str(project),
        "shared": bool(configured) and paths.expand(configured).resolve() == shared_dir.resolve(),
        "configured": configured,
        "shared_files": len(list(shared_dir.glob("*.md"))) if shared_dir.is_dir() else 0,
        "local_files": len(list(local.glob("*.md"))) if local.is_dir() else 0,
        "error": error,
    }


def status(cfg: Config) -> list[dict]:
    return [row(project) for project in projects(cfg)]


def share(project: Path, *, dry: bool = False) -> dict:
    """Keep this project's Claude memory inside the project. Nothing is overwritten or
    deleted: notes that exist only on this machine are copied in, and stay where they were."""
    file, data = _settings(project)
    target = project / ".claude" / "memory"
    wanted = portable(target)
    current = str(data.get(KEY, ""))
    if current and paths.expand(current).resolve() != target.resolve():
        raise ClaudeStateError(f"{file} already sets {KEY} to {current}; it was left as it is")
    local = local_memory(project)
    incoming = [note for note in sorted(local.glob("*.md")) if not (target / note.name).exists()] if local.is_dir() else []
    result = {"project": project.name, "setting": wanted, "changed": current != wanted, "copied": [note.name for note in incoming], "backup": ""}
    if dry:
        return result
    if current != wanted:
        if file.exists():
            keep = paths.ensure(paths.state_dir() / "claude-backups") / f"{project.name}-settings-{time.strftime('%Y%m%d-%H%M%S')}.json"
            shutil.copy2(file, keep)
            result["backup"] = str(keep)
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_name(file.name + ".suw-tmp")
        tmp.write_text(json.dumps({**data, KEY: wanted}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(file)
    target.mkdir(parents=True, exist_ok=True)
    for note in incoming:
        shutil.copy2(note, target / note.name)
    return result
