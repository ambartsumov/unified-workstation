"""Layered configuration.

Precedence (later wins):
    resources/config/defaults.toml       shipped with the product
    <config>/profiles/<active>.toml      the active profile (a named set of overrides)
    <config>/shared/shared.toml          meant for all of the user's computers
    <config>/shared/urls.toml            named URLs
    <config>/config.toml                 this computer only

A file that fails to parse never takes the system down: the last version that parsed
successfully (kept under the state directory) is used instead and a warning is recorded.
"""

from __future__ import annotations

import copy
import shutil
import tomllib
from pathlib import Path
from typing import Any

from . import paths, tomlw

_MISSING = object()


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _lkg_path(path: Path) -> Path:
    return paths.state_dir() / "lkg" / path.name


def read_toml(path: Path, warnings: list[str] | None = None) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        if warnings is not None:
            warnings.append(f"{path}: {exc}")
        lkg = _lkg_path(path)
        if lkg.exists():
            try:
                with lkg.open("rb") as handle:
                    return tomllib.load(handle)
            except (tomllib.TOMLDecodeError, OSError):
                return {}
        return {}
    try:
        lkg = _lkg_path(path)
        paths.ensure(lkg.parent)
        shutil.copyfile(path, lkg)
    except OSError:
        pass
    return data


class Config:
    def __init__(self, data: dict, warnings: list[str] | None = None):
        self.data = data
        self.warnings = warnings or []

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def device(self) -> str:
        return self.get("device.name") or default_device_name()

    @property
    def brand(self) -> str:
        return self.get("brand.name", "Unified Workstation")

    def url(self, name_or_url: str) -> str | None:
        """Resolve a named URL (urls.<name>) or pass a literal URL through."""
        if "://" in name_or_url:
            return name_or_url
        value = self.get(f"urls.{name_or_url}")
        if isinstance(value, str) and "://" in value and "REPLACE" not in value:
            return value
        return None


def default_device_name() -> str:
    """The name this computer gets until the user picks one: its own host name, label-safe."""
    import os
    import re
    import socket

    forced = os.environ.get("SUW_DEFAULT_DEVICE", "")
    if forced == "legacy":  # the pre-1.0 fixed names; the test-suite pins them
        return "mac" if paths.platform() == "macos" else "ubuntu"
    if forced:
        return forced
    try:
        host = socket.gethostname().split(".", 1)[0].lower()
    except OSError:
        host = ""
    label = re.sub(r"[^a-z0-9-]+", "-", host).strip("-")[:40]
    return label or {"macos": "mac", "windows": "pc"}.get(paths.platform(), "linux")


def local_file() -> Path:
    return paths.config_dir() / "config.toml"


def shared_file() -> Path:
    return paths.shared_dir() / "shared.toml"


def urls_file() -> Path:
    return paths.shared_dir() / "urls.toml"


def profiles_dir() -> Path:
    return paths.config_dir() / "profiles"


def profile_file(name: str | None = None) -> Path | None:
    """The active profile: a named set of overrides between the shipped defaults and the
    user's own settings. `SUW_PROFILE_FILE` pins one explicitly (used by the test-suite and by
    private editions that ship their own profile)."""
    import os
    import re

    pinned = os.environ.get("SUW_PROFILE_FILE")
    if pinned:
        return Path(pinned)
    if name is None:
        name = str((read_toml(local_file()).get("profile") or {}).get("active", "default"))
    if name in ("", "default") or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
        return None
    return profiles_dir() / f"{name}.toml"


def load() -> Config:
    warnings: list[str] = []
    data = read_toml(paths.resources() / "config" / "defaults.toml", warnings)
    profile = profile_file()
    layers = ([profile] if profile is not None else []) + [shared_file(), urls_file(), local_file()]
    for path in layers:
        data = deep_merge(data, read_toml(path, warnings))
    return Config(data, warnings)


def _set_path(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def set_value(dotted: str, value: Any, scope: str = "local") -> Path:
    """Persist one key. scope: local | shared | urls."""
    path = {"local": local_file(), "shared": shared_file(), "urls": urls_file()}[scope]
    data = read_toml(path)
    _set_path(data, dotted, value)
    tomlw.dump(path, data, mode=0o600 if scope == "local" else 0o644)
    return path


# ── validation ──────────────────────────────────────────────────────────────

_BOOL, _INT, _NUM, _STR, _LIST = "bool", "int", "number", "string", "list of strings"
SCHEMA: dict[str, tuple[str, tuple | None]] = {
    "brand.name": (_STR, None),
    "brand.short": (_STR, None),
    "brand.control_center": (_STR, None),
    "device.name": (_STR, None),
    "device.id": (_STR, None),
    "projects.roots": (_LIST, None),
    "projects.scan_roots": (_LIST, None),
    "projects.extra": (_LIST, None),
    "projects.exclude": (_LIST, None),
    "projects.layout": (_LIST, None),
    "sync.poll_seconds": (_INT, (5, 3600)),
    "sync.fetch_seconds": (_INT, (30, 86400)),
    "sync.policy": (_STR, ("manual", "merge", "rebase")),
    "sync.push": (_BOOL, None),
    "sync.max_backoff_seconds": (_INT, (30, 86400)),
    "sync.offline_retry_seconds": (_INT, (30, 86400)),
    "autosync.enabled": (_BOOL, None),
    "autosync.idle_seconds": (_INT, (1, 3600)),
    "autosync.min_interval_seconds": (_INT, (0, 86400)),
    "autosync.max_wait_seconds": (_INT, (0, 86400)),
    "autosync.max_file_mb": (_NUM, (0.001, 100000)),
    "autosync.warn_file_mb": (_NUM, (0.001, 100000)),
    "autosync.deny": (_LIST, None),
    "autosync.allow": (_LIST, None),
    "autosync.artifact_globs": (_LIST, None),
    "health.local_seconds": (_INT, (10, 3600)),
    "health.remote_seconds": (_INT, (30, 86400)),
    "health.offline_after_failures": (_INT, (1, 100)),
    "home.services": (_LIST, None),
    "cloud.outputs": (_LIST, None),
    "cloud.save_to": (_STR, None),
    "artifacts.store": (_STR, ("dir", "ssh")),
    "artifacts.path": (_STR, None),
    "artifacts.host": (_STR, None),
    "artifacts.remote_path": (_STR, None),
    "workstation.shortcut": (_STR, None),
    "workstation.control_center_shortcut": (_STR, None),
    "work.enabled": (_BOOL, None),
    "work.path": (_STR, None),
    "work.trash_days": (_INT, (1, 3650)),
    "work.large_file_mb": (_NUM, (0, 1000000)),
    "work.large_file_policy": (_STR, ("hold", "sync")),
    "work.large_allow": (_LIST, None),
    "work.ignore": (_LIST, None),
    "work.sync_anyway": (_LIST, None),
    "work.api_address": (_STR, None),
    "work.listen": (_LIST, None),
    "work.git_autosync": (_BOOL, None),
    "work.replica_path": (_STR, None),
    "work.replica_days": (_INT, (1, 3650)),
    "work.replica_keep": (_INT, (1, 1000)),
    "work.replica_port": (_INT, (1024, 65535)),
    "cloud.project_exclude": (_LIST, None),
    "cloud.project_confirm_mb": (_INT, (1, 1000000)),
    "peripherals.enabled": (_BOOL, None),
    "peripherals.server": (_STR, None),
    "peripherals.peer_side": (_STR, ("left", "right", "up", "down")),
    "peripherals.port": (_INT, (1024, 65535)),
    "peripherals.clipboard": (_BOOL, None),
    "peripherals.bind": (_STR, None),
    "workstation.open": (_STR, ("work", "project")),
    "workstation.shortcut_on": (_STR, None),
    "workstation.shortcut_off": (_STR, None),
    "workstation.close_browser": (_BOOL, None),
    "workstation.workspaces": (_LIST, None),
    "workstation.workspace_shortcuts": (_BOOL, None),
    "workstation.editor": (_STR, None),
    "workstation.terminal": (_STR, None),
    "workstation.indicator": (_STR, None),
    "workstation.wallpaper": (_BOOL, None),
    "workstation.dock_enabled": (_BOOL, None),
    "workstation.dock": (_LIST, None),
    "workstation.helper_extension": (_BOOL, None),
    "workstation.launch.editor": (_BOOL, None),
    "workstation.launch.terminal": (_BOOL, None),
    "workstation.launch.servers": (_BOOL, None),
    "workstation.launch.browser": (_BOOL, None),
    "workstation.launch.files": (_BOOL, None),
    "workstation.browser.app": (_STR, ("default", "chromium", "chrome", "firefox", "brave", "safari")),
    "workstation.browser.profile": (_STR, ("existing", "dedicated")),
    "workstation.browser.open": (_LIST, None),
    "shell.prompt": (_STR, ("keep", "starship")),
    "notify.enabled": (_BOOL, None),
    "notify.conflicts": (_BOOL, None),
    "notify.devices": (_BOOL, None),
    "notify.servers": (_BOOL, None),
    "notify.updates": (_BOOL, None),
    "notify.permissions": (_BOOL, None),
    "config.schema": (_INT, (1, 1000)),
    "general.language": (_STR, ("auto", "en", "ru")),
    "general.theme": (_STR, ("auto", "light", "dark")),
    "general.autostart": (_BOOL, None),
    "general.start_mode": (_STR, ("default", "last")),
    "onboarding.done": (_BOOL, None),
    "onboarding.kind": (_STR, ("personal", "workstation")),
    "updates.channel": (_STR, ("stable", "beta", "nightly")),
    "updates.auto_check": (_BOOL, None),
    "privacy.telemetry": (_BOOL, None),
    "profile.active": (_STR, None),
}
FREE_SECTIONS = ("urls", "theme", "secrets", "platform")  # user-defined keys, string values
_SECRETISH = ("password", "passwd", "token", "secret", "api_key", "apikey", "authkey")


def _flatten(data: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, dotted + "."))
        else:
            out[dotted] = value
    return out


def _type_ok(kind: str, value: Any) -> bool:
    if kind == _BOOL:
        return isinstance(value, bool)
    if kind == _INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == _NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == _STR:
        return isinstance(value, str)
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate(data: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a configuration. Pure."""
    errors, warnings = [], []
    for dotted, value in _flatten(data).items():
        section = dotted.split(".", 1)[0]
        leaf = dotted.rsplit(".", 1)[-1].lower()
        if section == "secrets":
            if not (isinstance(value, str) and value.startswith("keychain://")):
                errors.append(f"{dotted}: secrets must be references like keychain://name, never values")
            continue
        if any(word in leaf for word in _SECRETISH) and isinstance(value, str) and value and not value.startswith("keychain://"):
            errors.append(f"{dotted}: looks like a secret value; store it with `suw secret set` and reference keychain://name")
            continue
        if section in FREE_SECTIONS:
            if section != "platform" and not isinstance(value, str):
                errors.append(f"{dotted}: expected a string")
            elif section == "urls" and "://" not in value:
                errors.append(f"{dotted}: expected a URL (https://…)")
            continue
        if dotted not in SCHEMA:
            warnings.append(f"{dotted}: unknown key (ignored)")
            continue
        kind, limits = SCHEMA[dotted]
        if not _type_ok(kind, value):
            errors.append(f"{dotted}: expected {kind}, got {type(value).__name__}")
        elif limits and kind == _STR and value not in limits:
            errors.append(f"{dotted}: must be one of {', '.join(limits)}")
        elif limits and kind in (_INT, _NUM) and not limits[0] <= value <= limits[1]:
            errors.append(f"{dotted}: must be between {limits[0]} and {limits[1]}")
    return errors, warnings


def diff() -> list[tuple[str, Any, Any, str]]:
    """Every key that differs from the shipped defaults: (key, default, value, source)."""
    defaults = _flatten(read_toml(paths.resources() / "config" / "defaults.toml"))
    layers = [("shared", _flatten(read_toml(shared_file()))), ("shared", _flatten(read_toml(urls_file()))), ("local", _flatten(read_toml(local_file())))]
    merged, source = dict(defaults), {}
    for name, layer in layers:
        for key, value in layer.items():
            merged[key] = value
            source[key] = name
    return [(key, defaults.get(key), value, source[key]) for key, value in sorted(merged.items()) if key in source and defaults.get(key) != value]


def check_set(dotted: str, value: Any, scope: str) -> list[str]:
    """Dry run of `set_value`: errors the file would contain afterwards for this key."""
    path = {"local": local_file(), "shared": shared_file(), "urls": urls_file()}[scope]
    candidate = read_toml(path)
    _set_path(candidate, dotted, value)
    errors, _ = validate(candidate)
    return [e for e in errors if e.startswith(dotted + ":")]


def parse_cli_value(text: str) -> Any:
    """`suw config set` values: TOML literal when it parses, plain string otherwise."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text
