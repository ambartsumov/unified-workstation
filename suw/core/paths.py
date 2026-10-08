"""Filesystem locations.

Where things live is the operating system adapter's decision (`suw.platform`); this module
adds the environment overrides the test-suite uses to isolate itself from the real machine
and the lookups for files shipped inside the package.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent  # a source checkout; meaningless for an installed build


def _env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def platform() -> str:
    """'linux', 'macos' or 'windows'."""
    from .. import platform as osplatform

    return osplatform.name()


def _os():
    from .. import platform as osplatform

    return osplatform.get(platform())


def home() -> Path:
    return _env("SUW_HOME", Path.home())


def config_dir() -> Path:
    return _env("SUW_CONFIG_DIR", _os().config_dir(home()))


def shared_dir() -> Path:
    """Git repository with the non-secret configuration shared by all workstations."""
    return config_dir() / "shared"


def state_dir() -> Path:
    return _env("SUW_STATE_DIR", _os().state_dir(home()))


def cache_dir() -> Path:
    return _env("SUW_CACHE_DIR", _os().cache_dir(home()))


def log_dir() -> Path:
    return state_dir()


def runtime_dir() -> Path:
    base = os.environ.get("SUW_RUNTIME_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg) / "suw"
    return _os().runtime_dir(home())


def socket_path() -> Path:
    return runtime_dir() / "suwd.sock"


def ssh_dir() -> Path:
    return home() / ".ssh"


def default_workspace() -> Path:
    return _os().default_workspace(home())


def resources() -> Path:
    """Files shipped with the product: default configuration, service templates, shell snippets."""
    frozen = getattr(sys, "_MEIPASS", "")
    if frozen and (Path(frozen) / "suw" / "resources").is_dir():
        return Path(frozen) / "suw" / "resources"
    return PACKAGE_ROOT / "resources"


def frozen() -> bool:
    """True for a packaged build (installer), False for a source checkout or pip install."""
    return bool(getattr(sys, "frozen", False))


def launcher() -> Path:
    """The command that starts this product: the packaged executable, or `bin/suw` of a checkout."""
    override = os.environ.get("SUW_LAUNCHER")
    if override:
        return Path(override)
    if frozen():
        return Path(sys.executable)
    script = REPO_ROOT / "bin" / "suw"
    if script.exists() and platform() != "windows":
        return script
    found = _os().which("suw")
    return Path(found) if found else script


def ensure(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass
    return path


def expand(value: str) -> Path:
    if value.startswith("~"):
        return home() / value[1:].lstrip("/\\")
    return Path(os.path.expandvars(value)) if "%" in value or "$" in value else Path(value)
