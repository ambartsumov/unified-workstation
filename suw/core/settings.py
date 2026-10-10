"""The settings a person sees, on top of the configuration a program reads.

`config.SCHEMA` knows types and limits. This module adds what a settings screen needs: which
section a key belongs to, whether it is an everyday setting or an advanced one, and which
layer it is written to. Labels and help texts are *not* here — they live in the translation
catalogs under the keys `settings.<key>.label` / `.help`, so no user-facing sentence is
hard-coded in logic.

All writes go through `apply`: the whole change is validated first, a snapshot is taken, and
either every key is written or none is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import backup, config, events, i18n
from .config import Config

SECTIONS = ["general", "workspace", "sync", "workstations", "peripherals", "clipboard", "servers", "home", "cloud", "terminal", "git", "assistant", "security", "network", "updates", "privacy", "notifications", "advanced"]


@dataclass
class Field:
    key: str
    section: str
    kind: str                 # bool | int | number | string | choice | list | path
    choices: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    advanced: bool = False
    scope: str = "local"      # local (this computer) | shared (all of the user's computers)
    readonly: bool = False
    unit: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _f(key: str, section: str, *, advanced: bool = False, scope: str = "local", path: bool = False, unit: str = "", readonly: bool = False) -> Field:
    kind, limits = config.SCHEMA[key]
    field = Field(key, section, {"bool": "bool", "int": "int", "number": "number", "string": "string", "list of strings": "list"}[kind], advanced=advanced, scope=scope, unit=unit, readonly=readonly)
    if kind == "string" and limits:
        field.kind, field.choices = "choice", list(limits)
    elif limits:
        field.minimum, field.maximum = limits
    if path:
        field.kind = "path"
    return field


FIELDS: list[Field] = [
    _f("general.language", "general"),
    _f("general.theme", "general"),
    _f("general.autostart", "general"),
    _f("general.start_mode", "general"),
    _f("workstation.open", "general"),
    _f("workstation.close_browser", "general"),
    _f("workstation.launch.editor", "general"),
    _f("workstation.launch.terminal", "general"),
    _f("workstation.launch.servers", "general"),
    _f("workstation.launch.browser", "general"),
    _f("workstation.launch.files", "general"),
    _f("workstation.editor", "general"),
    _f("workstation.browser.app", "general"),
    _f("workstation.browser.profile", "general"),
    _f("workstation.browser.open", "general"),
    _f("workstation.shortcut_on", "general", advanced=True),
    _f("workstation.shortcut_off", "general", advanced=True),
    _f("workstation.workspaces", "general", advanced=True),
    _f("workstation.workspace_shortcuts", "general", advanced=True),
    _f("workstation.wallpaper", "general", advanced=True),
    _f("workstation.dock_enabled", "general", advanced=True),
    _f("work.path", "workspace", path=True),
    _f("work.enabled", "sync"),
    _f("work.trash_days", "sync", unit="days"),
    _f("work.large_file_mb", "sync", unit="MB"),
    _f("work.large_file_policy", "sync"),
    _f("work.ignore", "sync", advanced=True, scope="shared"),
    _f("work.sync_anyway", "sync", advanced=True, scope="shared"),
    _f("work.large_allow", "sync", advanced=True),
    _f("work.listen", "sync", advanced=True),
    _f("work.api_address", "sync", advanced=True),
    _f("device.name", "workstations", readonly=True),
    _f("peripherals.enabled", "peripherals"),
    _f("peripherals.server", "peripherals", scope="shared"),
    _f("peripherals.peer_side", "peripherals", scope="shared"),
    _f("peripherals.bind", "peripherals", advanced=True),
    _f("peripherals.port", "peripherals", advanced=True),
    _f("peripherals.clipboard", "clipboard", scope="shared"),
    _f("home.services", "home", advanced=True, scope="shared"),
    _f("work.replica_path", "home", advanced=True, scope="shared"),
    _f("work.replica_days", "home", unit="days", scope="shared"),
    _f("work.replica_keep", "home", scope="shared"),
    _f("cloud.project_confirm_mb", "cloud", unit="MB"),
    _f("cloud.project_exclude", "cloud", advanced=True, scope="shared"),
    _f("cloud.save_to", "cloud", path=True),
    _f("cloud.outputs", "cloud", advanced=True, scope="shared"),
    _f("workstation.terminal", "terminal"),
    _f("shell.prompt", "terminal", advanced=True),
    _f("autosync.enabled", "git"),
    _f("work.git_autosync", "git"),
    _f("projects.roots", "git", advanced=True),
    _f("sync.policy", "git", advanced=True),
    _f("sync.push", "git", advanced=True),
    _f("autosync.idle_seconds", "git", advanced=True, unit="s"),
    _f("autosync.max_file_mb", "git", advanced=True, unit="MB"),
    _f("updates.channel", "updates"),
    _f("updates.auto_check", "updates"),
    _f("privacy.telemetry", "privacy", readonly=True),
    _f("notify.enabled", "notifications"),
    _f("notify.conflicts", "notifications"),
    _f("notify.devices", "notifications"),
    _f("notify.servers", "notifications"),
    _f("notify.updates", "notifications"),
    _f("notify.permissions", "notifications"),
    _f("health.local_seconds", "advanced", advanced=True, unit="s"),
    _f("health.remote_seconds", "advanced", advanced=True, unit="s"),
    _f("sync.fetch_seconds", "advanced", advanced=True, unit="s"),
    _f("artifacts.store", "advanced", advanced=True),
    _f("artifacts.path", "advanced", advanced=True, path=True),
]
BY_KEY = {field.key: field for field in FIELDS}


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def describe(cfg: Config) -> dict:
    defaults = config._flatten(config.read_toml(config.paths.resources() / "config" / "defaults.toml"))
    rows = []
    for field in FIELDS:
        rows.append({**field.as_dict(), "value": cfg.get(field.key), "default": defaults.get(field.key)})
    return {"sections": SECTIONS, "fields": rows, "warnings": list(cfg.warnings)}


def coerce(field: Field, value: Any) -> Any:
    """Turn what a form sends into the configuration type. Raises ValueError with a plain reason."""
    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(i18n.msg("settings.error.on_off"))
    if field.kind == "int":
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(i18n.msg("settings.error.whole_number"))
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(i18n.msg("settings.error.whole_number")) from exc
    if field.kind == "number":
        if isinstance(value, bool):
            raise ValueError(i18n.msg("settings.error.number"))
        try:
            return float(value) if not isinstance(value, int) else value
        except (TypeError, ValueError) as exc:
            raise ValueError(i18n.msg("settings.error.number")) from exc
    if field.kind == "list":
        if isinstance(value, str):
            value = [line.strip() for line in value.replace(",", "\n").splitlines()]
        if not isinstance(value, list):
            raise ValueError(i18n.msg("settings.error.list"))
        return [str(item) for item in value if str(item).strip()]
    if not isinstance(value, str):
        raise ValueError(i18n.msg("settings.error.text"))
    value = value.strip()
    if field.kind == "path" and not value:
        raise ValueError(i18n.msg("settings.error.folder"))
    return value


def check(changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate a set of changes without writing anything. Returns (clean values, errors by key)."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in changes.items():
        field = BY_KEY.get(key)
        if field is None:
            errors[key] = i18n.msg("settings.error.unknown")
            continue
        if field.readonly:
            errors[key] = i18n.msg("settings.error.readonly")
            continue
        try:
            clean[key] = coerce(field, raw)
        except ValueError as exc:
            errors[key] = exc.args[0] if exc.args else str(exc)   # keeps a catalog sentence translatable
    for scope in ("local", "shared"):
        candidate = config.read_toml(config.local_file() if scope == "local" else config.shared_file())
        mine = [key for key in clean if BY_KEY[key].scope == scope]
        for key in mine:
            config._set_path(candidate, key, clean[key])
        found, _ = config.validate(candidate)
        for message in found:
            key = message.split(":", 1)[0]
            if key in mine:
                errors[key] = message.split(":", 1)[1].strip()
    if "work.path" in clean and "work.path" not in errors:
        problem = workspace_problem(clean["work.path"])
        if problem:
            errors["work.path"] = problem
    return clean, errors


def workspace_problem(value: str) -> str:
    """Why a folder can not be the work folder, in plain words; empty when it can."""
    from . import paths

    target = paths.expand(value)
    if not target.is_absolute():
        return i18n.msg("settings.error.path_full")
    home = paths.home().resolve()
    try:
        resolved = target.resolve()
    except OSError:
        return i18n.msg("settings.error.path_unusable")
    if resolved == home or resolved in home.parents:
        return i18n.msg("settings.error.path_home")
    system_dirs = [home / ".ssh", home / ".config", home / ".local", home / "Library", home / "AppData", paths.config_dir(), paths.state_dir()]
    for forbidden in system_dirs:
        try:
            forbidden = forbidden.resolve()
        except OSError:
            continue
        if resolved == forbidden or forbidden in resolved.parents:
            return i18n.msg("settings.error.path_system")
    parent = resolved
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    import os

    if not os.access(parent, os.W_OK):
        return i18n.msg("settings.error.path_denied")
    return ""


def apply(changes: dict[str, Any]) -> dict:
    clean, errors = check(changes)
    if errors:
        raise SettingsError(errors)
    if not clean:
        return {"changed": [], "snapshot": ""}
    current = config.load()
    changed = [key for key, value in clean.items() if current.get(key) != value]
    if not changed:
        return {"changed": [], "snapshot": ""}
    saved = backup.snapshot("before-settings-change")
    for key in changed:
        config.set_value(key, clean[key], BY_KEY[key].scope)
    events.emit("settings.changed", "settings changed: " + ", ".join(sorted(changed)))
    return {"changed": sorted(changed), "snapshot": saved.stem if saved else ""}


def reset(keys: list[str]) -> dict:
    """Back to the shipped default for the given keys (removes the override)."""
    known = [key for key in keys if key in BY_KEY and not BY_KEY[key].readonly]
    if not known:
        return {"changed": [], "snapshot": ""}
    saved = backup.snapshot("before-settings-reset")
    from . import tomlw

    for scope, path in (("local", config.local_file()), ("shared", config.shared_file())):
        data = config.read_toml(path)
        touched = False
        for key in known:
            parts = key.split(".")
            node = data
            for part in parts[:-1]:
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict) and parts[-1] in node:
                del node[parts[-1]]
                touched = True
        if touched:
            tomlw.dump(path, data, mode=0o600 if scope == "local" else 0o644)
    events.emit("settings.reset", "settings reset to default: " + ", ".join(sorted(known)))
    return {"changed": sorted(known), "snapshot": saved.stem if saved else ""}
