"""The contract every operating system adapter fulfils.

Core code never asks "is this Linux?": it asks the adapter for a directory, a credential,
an autostart entry or a capability. A feature the operating system cannot provide is reported
as such (see `Support`), never silently skipped and never reported as healthy.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.proc import Result, run, spawn

# How well a feature works on this machine, right now.
SUPPORTED = "SUPPORTED"
PARTIAL = "PARTIALLY_SUPPORTED"
NEEDS_PERMISSION = "REQUIRES_PERMISSION"
NOT_INSTALLED = "NOT_INSTALLED"
UNAVAILABLE = "UNAVAILABLE"


@dataclass
class Permission:
    """One permission the product may need, explained in the user's terms."""

    id: str
    title: str
    what: str            # what is being granted
    why: str             # which feature needs it
    effect: str          # what happens once it is granted
    revoke: str          # how to take it back
    state: str = "unknown"      # granted | missing | unknown | not_needed
    settings_url: str = ""      # deep link to the system settings pane, when one exists

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Autostart:
    enabled: bool
    mechanism: str
    detail: str = ""


@dataclass
class DesktopInfo:
    os: str                       # linux | macos | windows
    os_version: str = ""
    arch: str = ""
    desktop: str = ""             # GNOME, KDE, Aqua, Explorer …
    session: str = ""             # wayland | x11 | "" where the distinction does not exist
    service_manager: str = ""     # systemd | launchd | windows | none
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Platform:
    """Defaults that are true everywhere; adapters override what differs."""

    name = "generic"
    service_manager = "none"

    # ── directories ─────────────────────────────────────────────────────────
    def config_dir(self, home: Path) -> Path:
        return home / ".config" / "suw"

    def state_dir(self, home: Path) -> Path:
        return home / ".local" / "state" / "suw"

    def cache_dir(self, home: Path) -> Path:
        return home / ".cache" / "suw"

    def runtime_dir(self, home: Path) -> Path:
        return self.state_dir(home) / "run"

    def default_workspace(self, home: Path) -> Path:
        return home / "Desktop" / "Work"

    # ── executables ─────────────────────────────────────────────────────────
    def which(self, tool: str) -> str:
        """Absolute path of a tool, including the usual places a GUI process does not have on PATH."""
        found = shutil.which(tool)
        if found:
            return found
        for folder in self.extra_bin_dirs():
            for suffix in self.executable_suffixes():
                candidate = folder / f"{tool}{suffix}"
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
        return ""

    def extra_bin_dirs(self) -> list[Path]:
        return []

    def executable_suffixes(self) -> list[str]:
        return [""]

    # ── desktop ─────────────────────────────────────────────────────────────
    def info(self) -> DesktopInfo:
        return DesktopInfo(os=self.name)

    def open_path(self, path: Path) -> bool:
        return False

    def open_url(self, url: str) -> bool:
        import webbrowser

        try:
            return webbrowser.open(url)
        except Exception:
            return False

    def notify(self, app: str, title: str, body: str, urgent: bool = False) -> bool:
        return False

    # ── background services ─────────────────────────────────────────────────
    def service(self, unit: str, label: str, action: str) -> Result:
        """start | stop | restart | status a per-user background service.

        `unit` is the systemd unit name, `label` the launchd label / Windows task name."""
        return Result(127, "", "no service manager on this platform")

    def service_active(self, unit: str, label: str) -> bool:
        return self.service(unit, label, "status").ok

    # ── start at sign-in (the application window / tray, not the daemon) ─────
    def autostart(self) -> Autostart:
        return Autostart(False, "none", "not supported on this platform")

    def set_autostart(self, command: list[str], enabled: bool) -> bool:
        return False

    # ── credentials ─────────────────────────────────────────────────────────
    def credential_backend(self) -> str:
        return "none"

    def credential_get(self, service: str, name: str) -> str | None:
        return None

    def credential_put(self, service: str, name: str, value: str) -> bool:
        return False

    def credential_delete(self, service: str, name: str) -> bool:
        return False

    # ── permissions ─────────────────────────────────────────────────────────
    def permissions(self) -> list[Permission]:
        return []

    # ── helpers shared by adapters ──────────────────────────────────────────
    @staticmethod
    def _run(cmd: list[str], **kw) -> Result:
        return run(cmd, **kw)

    @staticmethod
    def _spawn(cmd: list[str]) -> bool:
        return spawn(cmd)
