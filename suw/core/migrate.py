"""Configuration migrations: backup → migrate → validate → (on failure) roll back.

`config.schema` in the local file records the layout version. Each migration is a small pure
function from one version to the next. A failed migration leaves the files exactly as they
were and reports why; the product keeps running on the previous layout.
"""

from __future__ import annotations

import copy
from typing import Callable

from . import backup, config, events, paths, tomlw

CURRENT = 2


def _v1_to_v2(local: dict, shared: dict) -> tuple[dict, dict]:
    """1 → 2 (public edition): the default work folder became `~/Desktop/Work`. A pre-release
    setup that relied on the old default `~/Desktop/work` gets it written down explicitly, so
    an existing folder keeps being used instead of a new empty one appearing beside it."""
    explicit = (local.get("work") or {}).get("path") or (shared.get("work") or {}).get("path")
    old = paths.home() / "Desktop" / "work"
    new = paths.home() / "Desktop" / "Work"
    if not explicit and old.is_dir() and not (new.is_dir() and not old.samefile(new)):
        local.setdefault("work", {})["path"] = "~/Desktop/work"
    return local, shared


MIGRATIONS: dict[int, Callable[[dict, dict], tuple[dict, dict]]] = {1: _v1_to_v2}


def version(local: dict) -> int:
    try:
        return int(local.get("config", {}).get("schema", 1))
    except (TypeError, ValueError):
        return 1


def pending() -> list[int]:
    local = config.read_toml(config.local_file())
    if not config.local_file().exists():
        return []
    return list(range(version(local), CURRENT))


def run() -> dict:
    """Bring the configuration to the current layout. Idempotent; safe to call at every start."""
    local_path, shared_path = config.local_file(), config.shared_file()
    if not local_path.exists():
        return {"migrated": False, "from": CURRENT, "to": CURRENT}
    local, shared = config.read_toml(local_path), config.read_toml(shared_path)
    start = version(local)
    if start >= CURRENT:
        return {"migrated": False, "from": start, "to": start}
    saved = backup.snapshot(f"before-migration-v{start}")
    new_local, new_shared = copy.deepcopy(local), copy.deepcopy(shared)
    try:
        for step in range(start, CURRENT):
            new_local, new_shared = MIGRATIONS[step](new_local, new_shared)
        new_local.setdefault("config", {})["schema"] = CURRENT
        errors = config.validate(new_local)[0] + config.validate(new_shared)[0]
        if errors:
            raise ValueError(errors[0])
        tomlw.dump(local_path, new_local, "This machine only.")
        if shared_path.exists():
            tomlw.dump(shared_path, new_shared, mode=0o644)
    except Exception as exc:
        # Nothing was written unless both files validated; restore covers a half-written pair.
        if saved is not None:
            try:
                backup.restore(saved.stem)
            except backup.BackupError:
                pass
        events.emit("config.migration_failed", f"configuration migration from v{start} failed: {exc}", "error")
        return {"migrated": False, "from": start, "to": start, "error": str(exc), "snapshot": saved.stem if saved else ""}
    events.emit("config.migrated", f"configuration migrated from v{start} to v{CURRENT}")
    return {"migrated": True, "from": start, "to": CURRENT, "snapshot": saved.stem if saved else ""}
