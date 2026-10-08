"""Small structured state store under the state directory.

One JSON document per concern (mode, sync memo, notifications, …) instead of one large
blob. Every write is temp-file + fsync + rename, durable documents keep the previous good
version next to them, and a document that fails to parse is quarantined and replaced by
that previous version instead of taking the system down.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import paths, tomlw

SCHEMA = 2
# Rewritten every few seconds and fully reconstructible: no backup copy is kept.
VOLATILE = {"runtime", "shell"}
# name -> {from_schema: migrate(data) -> data}. Applied in order on load.
MIGRATIONS: dict[str, dict[int, Callable[[dict], dict]]] = {}


def path(name: str) -> Path:
    return paths.state_dir() / f"{name}.json"


def _backup(name: str) -> Path:
    return paths.state_dir() / f"{name}.json.bak"


def _parse(file: Path) -> Any:
    return json.loads(file.read_text(encoding="utf-8"))


def _migrate(name: str, data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    version = int(data.get("_schema", 1))
    for source in sorted(MIGRATIONS.get(name, {})):
        if version <= source:
            data = MIGRATIONS[name][source](data)
    data.pop("_schema", None)  # bookkeeping only; callers see their own keys
    return data


def _quarantine(file: Path) -> Path | None:
    target = file.with_name(f"{file.name}.corrupt-{int(time.time())}")
    try:
        file.replace(target)
        return target
    except OSError:
        return None


def load(name: str, default: Any = None) -> Any:
    """Never raises. A corrupt document falls back to its previous good version."""
    file = path(name)
    try:
        return _migrate(name, _parse(file))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        _quarantine(file)
    try:
        data = _migrate(name, _parse(_backup(name)))
        tomlw.atomic_write(file, json.dumps(data, ensure_ascii=False, indent=1), 0o600)
        return data
    except (OSError, ValueError):
        return {} if default is None else default


def save(name: str, data: Any) -> None:
    paths.ensure(paths.state_dir())
    file = path(name)
    if isinstance(data, dict):
        data = {**data, "_schema": SCHEMA}
    if name not in VOLATILE and file.exists():
        try:
            _parse(file)  # only a document that still parses is worth keeping as the backup
            shutil.copyfile(file, _backup(name))
        except (OSError, ValueError):
            pass
    tomlw.atomic_write(file, json.dumps(data, ensure_ascii=False, indent=1), 0o600)


def recover() -> list[str]:
    """Startup pass: drop half-written temp files, repair or quarantine corrupt documents.

    Returns human-readable lines describing what was repaired (empty when all was well).
    """
    root = paths.state_dir()
    actions: list[str] = []
    if not root.is_dir():
        return actions
    for leftover in root.glob(".*.json.*"):
        try:
            leftover.unlink()
            actions.append(f"removed interrupted write {leftover.name}")
        except OSError:
            pass
    for file in sorted(root.glob("*.json")):
        try:
            _parse(file)
            continue
        except (OSError, ValueError):
            pass
        name = file.name[: -len(".json")]
        _quarantine(file)
        try:
            data = _parse(_backup(name))
            tomlw.atomic_write(file, json.dumps(data, ensure_ascii=False, indent=1), 0o600)
            actions.append(f"{file.name}: corrupt, restored from previous version")
        except (OSError, ValueError):
            actions.append(f"{file.name}: corrupt, reset (kept aside for inspection)")
    return actions


def health() -> dict:
    """Facts for `suw doctor`: is the store readable, and what was quarantined."""
    root = paths.state_dir()
    documents = sorted(p.name for p in root.glob("*.json")) if root.is_dir() else []
    bad = []
    for name in documents:
        try:
            _parse(root / name)
        except (OSError, ValueError):
            bad.append(name)
    return {
        "schema": SCHEMA,
        "documents": len(documents),
        "unreadable": bad,
        "quarantined": sorted(p.name for p in root.glob("*.corrupt-*")) if root.is_dir() else [],
    }
