"""macOS adapter: LaunchAgents, Keychain, Notification Center, TCC permissions."""

from __future__ import annotations

import os
import platform as pyplatform
import plistlib
from pathlib import Path

from .. import product
from ..core.proc import Result, run, spawn
from .base import Autostart, DesktopInfo, Permission, Platform

_PRIVACY = "x-apple.systempreferences:com.apple.preference.security?Privacy_"


class MacOS(Platform):
    name = "macos"
    service_manager = "launchd"

    def cache_dir(self, home: Path) -> Path:
        return home / "Library" / "Caches" / "suw"

    def extra_bin_dirs(self) -> list[Path]:
        # A process started from Finder or launchd has no Homebrew on PATH.
        return [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path.home() / ".local" / "bin", Path("/Applications/Tailscale.app/Contents/MacOS")]

    def info(self) -> DesktopInfo:
        notes = []
        if (Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Desktop").exists():
            notes.append("iCloud Desktop & Documents is on: a work folder on the Desktop would be synchronised twice")
        return DesktopInfo("macos", pyplatform.mac_ver()[0], pyplatform.machine(), "Aqua", "", "launchd", notes)

    def open_path(self, path: Path) -> bool:
        return spawn(["open", str(path)])

    def open_url(self, url: str) -> bool:
        return spawn(["open", url])

    def notify(self, app: str, title: str, body: str, urgent: bool = False) -> bool:
        script = f"display notification {_osa(body)} with title {_osa(app)} subtitle {_osa(title)}"
        return run(["osascript", "-e", script], timeout=5).ok

    # ── services ────────────────────────────────────────────────────────────
    def service(self, unit: str, label: str, action: str) -> Result:
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{label}"
        if action == "status":
            return run(["launchctl", "print", target], timeout=10)
        if action == "stop":
            return run(["launchctl", "kill", "TERM", target], timeout=15)
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not run(["launchctl", "print", target], timeout=10).ok and plist.exists():
            return run(["launchctl", "bootstrap", domain, str(plist)], timeout=15)
        return run(["launchctl", "kickstart", "-k" if action == "restart" else "-p", target], timeout=15)

    # ── autostart ───────────────────────────────────────────────────────────
    def _agent(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{product.APP_ID}.plist"

    def autostart(self) -> Autostart:
        file = self._agent()
        return Autostart(file.exists(), "LaunchAgent", str(file))

    def set_autostart(self, command: list[str], enabled: bool) -> bool:
        file = self._agent()
        try:
            if not enabled:
                run(["launchctl", "bootout", f"gui/{os.getuid()}/{product.APP_ID}"], timeout=10)
                file.unlink(missing_ok=True)
                return True
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("wb") as handle:
                plistlib.dump({"Label": product.APP_ID, "ProgramArguments": command, "RunAtLoad": True, "ProcessType": "Interactive"}, handle)
            return True
        except OSError:
            return False

    # ── credentials: Keychain ───────────────────────────────────────────────
    def credential_backend(self) -> str:
        return "macOS Keychain"

    def credential_get(self, service: str, name: str) -> str | None:
        res = run(["security", "find-generic-password", "-s", service, "-a", name, "-w"], timeout=10)
        return res.out.strip() if res.ok and res.out.strip() else None

    def credential_put(self, service: str, name: str, value: str) -> bool:
        # -U updates in place. The value travels as an argument to a local, same-user
        # process; `security` offers no stdin mode for generic passwords.
        return run(["security", "add-generic-password", "-U", "-s", service, "-a", name, "-w", value], timeout=10).ok

    def credential_delete(self, service: str, name: str) -> bool:
        return run(["security", "delete-generic-password", "-s", service, "-a", name], timeout=10).ok

    # ── permissions ─────────────────────────────────────────────────────────
    def permissions(self) -> list[Permission]:
        return [
            Permission(
                "accessibility",
                "Accessibility",
                "Lets the keyboard/mouse sharing tool move the pointer and type on this Mac.",
                "Sharing one keyboard and mouse between your computers.",
                "This Mac can be controlled from, and can control, your other paired computers. Nothing is recorded.",
                "System Settings → Privacy & Security → Accessibility → switch the entry off.",
                "unknown",
                _PRIVACY + "Accessibility",
            ),
            Permission(
                "input-monitoring",
                "Input Monitoring",
                "Lets the sharing tool see key presses so it can forward them to the computer the pointer is on.",
                "Using this Mac's keyboard on another paired computer.",
                "Keys are forwarded only to computers you paired, only over your private network.",
                "System Settings → Privacy & Security → Input Monitoring → switch the entry off.",
                "unknown",
                _PRIVACY + "ListenEvent",
            ),
            Permission(
                "local-network",
                "Local Network",
                "Lets the application find and talk to your other computers on the same network.",
                "File sync and keyboard/mouse sharing between computers at home or in the office.",
                "Without it, sync works only through a private network such as Tailscale.",
                "System Settings → Privacy & Security → Local Network → switch the entry off.",
                "unknown",
                _PRIVACY + "LocalNetwork",
            ),
            Permission(
                "autostart",
                "Start at login",
                "A login item for the background service.",
                "Keeps sync and status running without opening the application first.",
                "The service starts when you log in. Nothing runs as administrator.",
                "System Settings → General → Login Items, or Settings → General inside the application.",
                "granted" if self.autostart().enabled else "missing",
                "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
            ),
        ]


def _osa(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
