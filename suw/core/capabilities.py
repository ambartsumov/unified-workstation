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
from . import paths
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


# id, display name, purpose, candidate executables, version arguments, install guidance
_CATALOG: list[tuple[str, str, str, list[str], list[str], dict[str, str]]] = [
    ("git", "Git", "Project history and repository status", ["git"], ["--version"],
     {"linux": "Install the “git” package from your distribution's software centre.", "macos": "Install the Xcode Command Line Tools (macOS offers this the first time Git is used) or Homebrew: brew install git.", "windows": "Install “Git for Windows” (winget install Git.Git)."}),
    ("ssh", "OpenSSH", "Connecting to Home and Cloud servers", ["ssh"], ["-V"],
     {"linux": "Install the “openssh-client” package.", "macos": "Included with macOS.", "windows": "Settings → System → Optional features → OpenSSH Client."}),
    ("syncthing", "Syncthing", "Keeping the work folder identical on every computer", ["syncthing"], ["--version"],
     {"linux": "Install the “syncthing” package (apt, dnf, pacman) or from syncthing.net.", "macos": "brew install syncthing", "windows": "winget install Syncthing.Syncthing"}),
    ("deskflow", "Deskflow", "Sharing one keyboard and mouse between computers", ["deskflow-core", "deskflow"], ["--version"],
     {"linux": "Install Deskflow from Flathub (org.deskflow.deskflow) or your distribution.", "macos": "brew tap deskflow/tap && brew install --cask deskflow", "windows": "winget install Deskflow.Deskflow"}),
    ("tailscale", "Tailscale", "A private network between your computers when they are not on the same Wi-Fi", ["tailscale"], ["version"],
     {"linux": "See tailscale.com/download/linux.", "macos": "Install Tailscale from the Mac App Store or tailscale.com.", "windows": "winget install Tailscale.Tailscale"}),
    ("claude", "Claude Code", "AI coding assistant working inside your work folder", ["claude"], ["--version"],
     {"linux": "See the Claude Code installation guide.", "macos": "See the Claude Code installation guide.", "windows": "See the Claude Code installation guide."}),
    ("wezterm", "WezTerm", "Terminal with clipboard that works over SSH", ["wezterm"], ["--version"],
     {"linux": "Install WezTerm from wezterm.org or Flathub.", "macos": "brew install --cask wezterm", "windows": "winget install wez.wezterm"}),
    ("tmux", "tmux", "Terminal sessions that survive a dropped connection", ["tmux"], ["-V"],
     {"linux": "Install the “tmux” package.", "macos": "brew install tmux", "windows": "Not available natively; sessions use plain terminal windows."}),
    ("rsync", "rsync", "Sending a selected project to a Cloud server", ["rsync"], ["--version"],
     {"linux": "Install the “rsync” package.", "macos": "Included with macOS.", "windows": "Not included with Windows; install through WSL or cwRsync."}),
    ("openssl", "OpenSSL", "Creating the certificate used for keyboard/mouse sharing", ["openssl"], ["version"],
     {"linux": "Install the “openssl” package.", "macos": "Included with macOS.", "windows": "Included with Git for Windows."}),
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
    for ident, name, purpose, exes, args, install in _CATALOG:
        path = next((found for found in (os_.which(exe) for exe in exes) if found), "")
        out.append(Component(ident, name, purpose, bool(path), path, _version(path, args) if path and with_versions else "", install=install))
    editor = next(((os_.which(exe), label) for exe, label in _EDITORS if os_.which(exe)), ("", ""))
    out.append(Component("editor", editor[1] or "Code editor", "Opening projects", bool(editor[0]), editor[0],
                         install={k: "Install any code editor (Visual Studio Code, Cursor, Zed…)." for k in ("linux", "macos", "windows")}))
    terminals = _TERMINALS.get(paths.platform(), [])
    terminal = next(((os_.which(exe), label) for exe, label in terminals if os_.which(exe)), ("", ""))
    if not terminal[0] and paths.platform() == "macos":
        terminal = ("/System/Applications/Utilities/Terminal.app", "Terminal")
    out.append(Component("terminal", terminal[1] or "Terminal", "Running commands and server sessions", bool(terminal[0]), terminal[0],
                         install={k: "Any terminal works; WezTerm is recommended." for k in ("linux", "macos", "windows")}))
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

    add("workspace", SUPPORTED, "A normal folder on this computer. It works without any other component.")

    if not have["syncthing"].installed:
        add("file_sync", NOT_INSTALLED, "File sync is done by Syncthing, which is not installed on this computer.", have["syncthing"].install.get(system, ""), ["syncthing"])
    else:
        add("file_sync", SUPPORTED, "Syncthing is installed. Files are sent directly between your paired computers.", needs=["syncthing"])

    # Shared keyboard and mouse
    if not have["deskflow"].installed:
        add("peripherals", NOT_INSTALLED, "Keyboard and mouse sharing is done by Deskflow, which is not installed.", have["deskflow"].install.get(system, ""), ["deskflow"])
    elif system == "macos":
        add("peripherals", NEEDS_PERMISSION, "macOS requires Accessibility and Input Monitoring permission for the sharing tool. Until both are granted, this Mac can not send or receive keyboard and mouse input.", "Open Health → Permissions and follow the two steps.", ["deskflow"])
    elif system == "linux" and info.session == "wayland":
        add("peripherals", PARTIAL, "On Wayland, sharing depends on your desktop's input-capture portal (GNOME 46+, KDE Plasma 6.1+). Your desktop asks for approval the first time; on older desktops this computer can only be controlled, not control others.", "If sharing does not start, sign in with an X11 session or update the desktop.", ["deskflow"])
    elif system == "windows":
        add("peripherals", PARTIAL, "Works in normal windows. Windows blocks shared input inside windows that run as administrator and on the sign-in screen.", "", ["deskflow"])
    else:
        add("peripherals", SUPPORTED, "Deskflow is installed.", needs=["deskflow"])

    peripherals_state = out[-1].status
    if peripherals_state == NOT_INSTALLED:
        add("shared_clipboard", NOT_INSTALLED, "The clipboard travels with the shared keyboard and mouse, which needs Deskflow.", needs=["deskflow"])
    elif system == "linux" and info.session == "wayland":
        add("shared_clipboard", PARTIAL, "Text is shared. On Wayland, images and files may not be, depending on the desktop.", needs=["deskflow"])
    else:
        add("shared_clipboard", peripherals_state if peripherals_state != SUPPORTED else SUPPORTED, "Text and images copied on one computer can be pasted on the other.", needs=["deskflow"])

    # Clipboard from a remote terminal session
    terminal = (cfg.get("workstation.terminal", "auto") if cfg else "auto") or "auto"
    if have["wezterm"].installed and terminal in ("auto", "wezterm"):
        add("terminal_clipboard", SUPPORTED, "Copying inside a server session reaches this computer's clipboard (OSC 52, through WezTerm).", needs=["wezterm"])
    else:
        add("terminal_clipboard", PARTIAL, "Copying from a server session to this computer depends on the terminal. WezTerm supports it; many built-in terminals do not.", have["wezterm"].install.get(system, ""), ["wezterm"])

    # Workstation Mode: launching the tools is universal; arranging windows is not.
    desktop = info.desktop.upper()
    if system == "linux" and "GNOME" in desktop:
        add("workstation_layout", SUPPORTED, "Editor, terminal and browser open on their own workspaces (GNOME, through a small helper extension).")
    elif system == "linux":
        add("workstation_layout", PARTIAL, f"Your tools are opened for you. Placing each window on its own workspace is implemented for GNOME only; on {info.desktop or 'this desktop'} the windows open where the desktop puts them.")
    elif system == "macos":
        hammerspoon = os.path.isdir("/Applications/Hammerspoon.app")
        add("workstation_layout", PARTIAL if not hammerspoon else NEEDS_PERMISSION,
            "Your tools are opened for you. Arranging them on Spaces and the global shortcut use Hammerspoon" + (", which needs Accessibility permission." if hammerspoon else ", which is not installed."),
            "" if hammerspoon else "brew install --cask hammerspoon")
    else:
        add("workstation_layout", PARTIAL, "Your tools are opened for you. Arranging windows on virtual desktops and a global shortcut are not implemented on Windows yet.")

    # Networking
    if not have["tailscale"].installed:
        add("private_network", PARTIAL, "Computers on the same home or office network find each other directly. Reaching them from elsewhere needs a private network such as Tailscale, which is not installed.", have["tailscale"].install.get(system, ""), ["tailscale"])
    else:
        add("private_network", SUPPORTED, "Tailscale is installed: paired computers and servers are reachable from anywhere, privately.", needs=["tailscale"])

    # Servers
    if not have["ssh"].installed:
        add("servers", NOT_INSTALLED, "Home and Cloud servers are reached over SSH, which is not installed.", have["ssh"].install.get(system, ""), ["ssh"])
        add("cloud_projects", NOT_INSTALLED, "Needs SSH.", needs=["ssh", "rsync"])
    else:
        add("servers", SUPPORTED, "OpenSSH is installed. Server identity is always verified.", needs=["ssh"])
        if have["rsync"].installed:
            add("cloud_projects", SUPPORTED, "Selected projects are sent with rsync over SSH.", needs=["ssh", "rsync"])
        else:
            add("cloud_projects", UNAVAILABLE if system == "windows" else NOT_INSTALLED, "Sending a project to a Cloud server uses rsync, which is not installed." + (" Windows does not include it." if system == "windows" else ""), have["rsync"].install.get(system, ""), ["rsync"])

    backend = os_.credential_backend()
    if backend == "none":
        add("credentials", UNAVAILABLE, "No system credential store was found. Passwords can not be saved; you will be asked each time.", "Install and unlock a keyring (for example GNOME Keyring or KWallet's Secret Service)." if system == "linux" else "")
    else:
        add("credentials", SUPPORTED, f"Passwords are kept in {backend}, never in configuration files.")

    if os_.service_manager in ("systemd", "launchd"):
        add("background_service", SUPPORTED, f"Sync and status keep running in the background ({os_.service_manager}).")
    else:
        add("background_service", PARTIAL, "This session has no service manager the product integrates with, so background components are started by the application and restored at sign-in.")

    auto = os_.autostart()
    add("autostart", SUPPORTED if auto.mechanism != "none" else UNAVAILABLE, f"Start at sign-in uses: {auto.mechanism}.")
    add("notifications", SUPPORTED if (system != "linux" or bool(os_.which("notify-send"))) else PARTIAL,
        "Important events are shown as system notifications." if (system != "linux" or os_.which("notify-send")) else "notify-send is not installed, so events appear inside the application only.")
    add("file_watching", SUPPORTED if system == "linux" else PARTIAL,
        "Changes are noticed immediately." if system == "linux" else "Project status is refreshed on a timer on this operating system (file sync itself is immediate).")
    add("git", SUPPORTED if have["git"].installed else NOT_INSTALLED, "Repository status and history." if have["git"].installed else "Git is not installed; project status is not shown.", "" if have["git"].installed else have["git"].install.get(system, ""), ["git"])
    add("assistant", SUPPORTED if have["claude"].installed else NOT_INSTALLED,
        "Claude Code is installed. Project context stored inside the work folder follows the project." if have["claude"].installed else "Claude Code is not installed. Everything else works without it.",
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
