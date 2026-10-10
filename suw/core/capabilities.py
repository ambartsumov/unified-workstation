"""What this computer can do, detected — never assumed.

Two views:

* `components()` — the tools the product builds on (Git, Syncthing, Deskflow, …): installed
  or not, which version, and how to get them on this operating system.
* `features()` — what the user cares about (file sync, shared keyboard, …), each with one
  honest status: SUPPORTED, PARTIALLY_SUPPORTED, REQUIRES_PERMISSION, NOT_INSTALLED or
  UNAVAILABLE, plus the reason in plain language.

Operating systems do not offer identical capabilities and this module does not pretend they
do. A feature that cannot be verified on this machine is never reported as supported.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

from .. import platform as osplatform
from ..platform import NEEDS_PERMISSION, NOT_INSTALLED, PARTIAL, SUPPORTED, UNAVAILABLE
from . import i18n, paths
from .config import Config
from .proc import run


@dataclass
class Component:
    id: str
    name: str
    purpose: str
    installed: bool = False
    path: str = ""
    version: str = ""
    required_for: list[str] = field(default_factory=list)
    install: dict[str, str] = field(default_factory=dict)   # per-OS guidance
    optional: bool = True

    def as_dict(self) -> dict:
        data = asdict(self)
        data["install_hint"] = self.install.get(paths.platform(), "")
        return data


@dataclass
class Feature:
    id: str
    status: str
    reason: str = ""
    action: str = ""     # what the user can do about it; empty when nothing is needed
    needs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# id, display name, candidate executables, version arguments. Purpose and install guidance are
# catalog sentences: `component.<id>.purpose` and `install.<id>.<system>`.
_SYSTEMS = ("linux", "macos", "windows")


def _install(ident: str) -> dict[str, str]:
    return {system: i18n.msg(f"install.{ident}.{system}") for system in _SYSTEMS}

_CATALOG: list[tuple[str, str, list[str], list[str]]] = [
    ("git", "Git", ["git"], ["--version"]),
    ("ssh", "OpenSSH", ["ssh"], ["-V"]),
    ("syncthing", "Syncthing", ["syncthing"], ["--version"]),
    ("deskflow", "Deskflow", ["deskflow-core", "deskflow"], ["--version"]),
    ("tailscale", "Tailscale", ["tailscale"], ["version"]),
    ("claude", "Claude Code", ["claude"], ["--version"]),
    ("wezterm", "WezTerm", ["wezterm"], ["--version"]),
    ("tmux", "tmux", ["tmux"], ["-V"]),
    ("rsync", "rsync", ["rsync"], ["--version"]),
    ("openssl", "OpenSSL", ["openssl"], ["version"]),
]
_EDITORS = [("cursor", "Cursor"), ("code", "Visual Studio Code"), ("codium", "VSCodium"), ("zed", "Zed"), ("subl", "Sublime Text"), ("idea", "IntelliJ IDEA"), ("pycharm", "PyCharm"), ("nvim", "Neovim"), ("vim", "Vim")]
_TERMINALS = {
    "linux": [("wezterm", "WezTerm"), ("ghostty", "Ghostty"), ("kitty", "kitty"), ("alacritty", "Alacritty"), ("gnome-terminal", "GNOME Terminal"), ("ptyxis", "Ptyxis"), ("konsole", "Konsole"), ("xfce4-terminal", "Xfce Terminal"), ("xterm", "xterm")],
    "macos": [("wezterm", "WezTerm"), ("ghostty", "Ghostty"), ("kitty", "kitty"), ("alacritty", "Alacritty")],
    "windows": [("wezterm", "WezTerm"), ("wt", "Windows Terminal"), ("alacritty", "Alacritty"), ("powershell", "PowerShell")],
}
_VERSION = re.compile(r"v?(\d+\.\d+(?:\.\d+)?(?:[-+.\w]*)?)")


def _version(exe: str, args: list[str]) -> str:
    res = run([exe, *args], timeout=6)
    match = _VERSION.search((res.out or res.err).splitlines()[0] if (res.out or res.err).strip() else "")
    return match.group(1) if match else ""


def components(with_versions: bool = True) -> list[Component]:
    os_ = osplatform.current()
    out = []
    for ident, name, exes, args in _CATALOG:
        path = next((found for found in (os_.which(exe) for exe in exes) if found), "")
        out.append(Component(ident, name, i18n.msg(f"component.{ident}.purpose"), bool(path), path, _version(path, args) if path and with_versions else "", install=_install(ident)))
    editor = next(((os_.which(exe), label) for exe, label in _EDITORS if os_.which(exe)), ("", ""))
    out.append(Component("editor", editor[1] or i18n.msg("component.editor.name"), i18n.msg("component.editor.purpose"), bool(editor[0]), editor[0],
                         install={k: i18n.msg("install.editor") for k in _SYSTEMS}))
    terminals = _TERMINALS.get(paths.platform(), [])
    terminal = next(((os_.which(exe), label) for exe, label in terminals if os_.which(exe)), ("", ""))
    if not terminal[0] and paths.platform() == "macos":
        terminal = ("/System/Applications/Utilities/Terminal.app", "Terminal")
    out.append(Component("terminal", terminal[1] or i18n.msg("component.terminal.name"), i18n.msg("component.terminal.purpose"), bool(terminal[0]), terminal[0],
                         install={k: i18n.msg("install.terminal") for k in _SYSTEMS}))
    return out


def by_id(items: list[Component]) -> dict[str, Component]:
    return {item.id: item for item in items}


def editors() -> list[dict]:
    os_ = osplatform.current()
    return [{"id": exe, "name": label} for exe, label in _EDITORS if os_.which(exe)]


def terminals() -> list[dict]:
    os_ = osplatform.current()
    found = [{"id": exe, "name": label} for exe, label in _TERMINALS.get(paths.platform(), []) if os_.which(exe)]
    if paths.platform() == "macos":
        found.append({"id": "Terminal.app", "name": "Terminal"})
    return found


def features(cfg: Config | None = None, parts: list[Component] | None = None) -> list[Feature]:
    os_ = osplatform.current()
    info = os_.info()
    have = by_id(parts if parts is not None else components(with_versions=False))
    system = paths.platform()
    out: list[Feature] = []

    def add(ident: str, status: str, reason: str = "", action: str = "", needs: list[str] | None = None) -> None:
        out.append(Feature(ident, status, reason, action, needs or []))

    def say(key: str, **values) -> str:
        return i18n.msg(f"cap.{key}", **values)

    add("workspace", SUPPORTED, say("workspace.ok"))

    if not have["syncthing"].installed:
        add("file_sync", NOT_INSTALLED, say("file_sync.missing"), have["syncthing"].install.get(system, ""), ["syncthing"])
    else:
        add("file_sync", SUPPORTED, say("file_sync.ok"), needs=["syncthing"])

    # Shared keyboard and mouse
    if not have["deskflow"].installed:
        add("peripherals", NOT_INSTALLED, say("peripherals.missing"), have["deskflow"].install.get(system, ""), ["deskflow"])
    elif system == "macos":
        add("peripherals", NEEDS_PERMISSION, say("peripherals.macos"), say("peripherals.macos_action"), ["deskflow"])
    elif system == "linux" and info.session == "wayland":
        add("peripherals", PARTIAL, say("peripherals.wayland"), say("peripherals.wayland_action"), ["deskflow"])
    elif system == "windows":
        add("peripherals", PARTIAL, say("peripherals.windows"), "", ["deskflow"])
    else:
        add("peripherals", SUPPORTED, say("peripherals.ok"), needs=["deskflow"])

    peripherals_state = out[-1].status
    if peripherals_state == NOT_INSTALLED:
        add("shared_clipboard", NOT_INSTALLED, say("shared_clipboard.missing"), needs=["deskflow"])
    elif system == "linux" and info.session == "wayland":
        add("shared_clipboard", PARTIAL, say("shared_clipboard.wayland"), needs=["deskflow"])
    else:
        add("shared_clipboard", peripherals_state if peripherals_state != SUPPORTED else SUPPORTED, say("shared_clipboard.ok"), needs=["deskflow"])

    # Clipboard from a remote terminal session
    terminal = (cfg.get("workstation.terminal", "auto") if cfg else "auto") or "auto"
    if have["wezterm"].installed and terminal in ("auto", "wezterm"):
        add("terminal_clipboard", SUPPORTED, say("terminal_clipboard.ok"), needs=["wezterm"])
    else:
        add("terminal_clipboard", PARTIAL, say("terminal_clipboard.partial"), have["wezterm"].install.get(system, ""), ["wezterm"])

    # Workstation Mode: launching the tools is universal; arranging windows is not.
    desktop = info.desktop.upper()
    if system == "linux" and "GNOME" in desktop:
        add("workstation_layout", SUPPORTED, say("workstation_layout.gnome"))
    elif system == "linux":
        add("workstation_layout", PARTIAL, say("workstation_layout.other_desktop", desktop=info.desktop) if info.desktop else say("workstation_layout.this_desktop"))
    elif system == "macos":
        hammerspoon = os.path.isdir("/Applications/Hammerspoon.app")
        add("workstation_layout", PARTIAL if not hammerspoon else NEEDS_PERMISSION,
            say("workstation_layout.hammerspoon_permission") if hammerspoon else say("workstation_layout.hammerspoon_missing"),
            "" if hammerspoon else "brew install --cask hammerspoon")
    else:
        add("workstation_layout", PARTIAL, say("workstation_layout.windows"))

    # Networking
    if not have["tailscale"].installed:
        add("private_network", PARTIAL, say("private_network.missing"), have["tailscale"].install.get(system, ""), ["tailscale"])
    else:
        add("private_network", SUPPORTED, say("private_network.ok"), needs=["tailscale"])

    # Servers
    if not have["ssh"].installed:
        add("servers", NOT_INSTALLED, say("servers.missing"), have["ssh"].install.get(system, ""), ["ssh"])
        add("cloud_projects", NOT_INSTALLED, say("cloud_projects.needs_ssh"), needs=["ssh", "rsync"])
    else:
        add("servers", SUPPORTED, say("servers.ok"), needs=["ssh"])
        if have["rsync"].installed:
            add("cloud_projects", SUPPORTED, say("cloud_projects.ok"), needs=["ssh", "rsync"])
        else:
            add("cloud_projects", UNAVAILABLE if system == "windows" else NOT_INSTALLED, say("cloud_projects.no_rsync_windows") if system == "windows" else say("cloud_projects.no_rsync"), have["rsync"].install.get(system, ""), ["rsync"])

    backend = os_.credential_backend()
    if backend == "none":
        add("credentials", UNAVAILABLE, say("credentials.none"), say("credentials.none_action") if system == "linux" else "")
    else:
        add("credentials", SUPPORTED, say("credentials.ok", store=backend))

    if os_.service_manager in ("systemd", "launchd"):
        add("background_service", SUPPORTED, say("background_service.ok", manager=os_.service_manager))
    else:
        add("background_service", PARTIAL, say("background_service.partial"))

    auto = os_.autostart()
    add("autostart", SUPPORTED if auto.mechanism != "none" else UNAVAILABLE, say("autostart.uses", mechanism=auto.mechanism))
    notify = system != "linux" or bool(os_.which("notify-send"))
    add("notifications", SUPPORTED if notify else PARTIAL, say("notifications.ok") if notify else say("notifications.partial"))
    add("file_watching", SUPPORTED if system == "linux" else PARTIAL, say("file_watching.ok") if system == "linux" else say("file_watching.timer"))
    add("git", SUPPORTED if have["git"].installed else NOT_INSTALLED, say("git.ok") if have["git"].installed else say("git.missing"), "" if have["git"].installed else have["git"].install.get(system, ""), ["git"])
    add("assistant", SUPPORTED if have["claude"].installed else NOT_INSTALLED,
        say("assistant.ok") if have["claude"].installed else say("assistant.missing"),
        "" if have["claude"].installed else have["claude"].install.get(system, ""), ["claude"])
    return out


def matrix(cfg: Config | None = None) -> dict:
    parts = components()
    os_ = osplatform.current()
    return {
        "platform": os_.info().as_dict(),
        "components": [c.as_dict() for c in parts],
        "features": [f.as_dict() for f in features(cfg, parts)],
        "editors": editors(),
        "terminals": terminals(),
    }
