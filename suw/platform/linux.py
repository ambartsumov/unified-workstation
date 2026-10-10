"""Linux adapter: XDG directories, systemd user services (when systemd is the session
manager), XDG autostart, Secret Service credentials, freedesktop notifications."""

from __future__ import annotations

import os
import platform as pyplatform
import shlex
from pathlib import Path

from .. import product
from ..core import i18n
from ..core.proc import Result, have, run, spawn
from .base import Autostart, DesktopInfo, Permission, Platform


def _xdg(name: str, default: Path) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value.startswith("/") else default


class Linux(Platform):
    name = "linux"

    @property
    def service_manager(self) -> str:  # type: ignore[override]
        return "systemd" if have("systemctl") and Path("/run/systemd/system").is_dir() else "none"

    # The configuration and state directories keep the same layout as macOS on purpose
    # (documented paths match on both); cache follows XDG.
    def cache_dir(self, home: Path) -> Path:
        return _xdg("XDG_CACHE_HOME", home / ".cache") / "suw"

    def default_workspace(self, home: Path) -> Path:
        desktop = ""
        if have("xdg-user-dir") and home == Path.home():
            desktop = run(["xdg-user-dir", "DESKTOP"], timeout=5).out.strip()
        base = Path(desktop) if desktop.startswith("/") and Path(desktop) != home else home / "Desktop"
        return base / "Work"

    def extra_bin_dirs(self) -> list[Path]:
        home = Path.home()
        return [home / ".local" / "bin", Path("/usr/local/bin"), Path("/snap/bin"), Path("/var/lib/flatpak/exports/bin"), home / ".local/share/flatpak/exports/bin"]

    def info(self) -> DesktopInfo:
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
        session = os.environ.get("XDG_SESSION_TYPE", "") or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11" if os.environ.get("DISPLAY") else "")
        release = ""
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    release = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        notes = []
        if os.environ.get("FLATPAK_ID"):
            notes.append("running inside Flatpak: host tools are reached through the sandbox portal")
        return DesktopInfo("linux", release, pyplatform.machine(), desktop, session, self.service_manager, notes)

    def open_path(self, path: Path) -> bool:
        return have("xdg-open") and spawn(["xdg-open", str(path)])

    def open_url(self, url: str) -> bool:
        if have("xdg-open"):
            return spawn(["xdg-open", url])
        return super().open_url(url)

    def notify(self, app: str, title: str, body: str, urgent: bool = False) -> bool:
        if not have("notify-send"):
            return False
        cmd = ["notify-send", "--app-name", app, "--icon", "dialog-warning" if urgent else "dialog-information"]
        if urgent:
            cmd += ["--urgency", "critical"]
        return run([*cmd, title, body], timeout=5).ok

    # ── services ────────────────────────────────────────────────────────────
    def service(self, unit: str, label: str, action: str) -> Result:
        if self.service_manager != "systemd":
            return Result(127, "", "systemd is not managing this session; the service is started with the application instead")
        if action == "status":
            res = run(["systemctl", "--user", "is-active", unit], timeout=10)
            return Result(0 if res.out.strip() == "active" else 3, res.out, res.err)
        return run(["systemctl", "--user", action, unit], timeout=30)

    # ── autostart ───────────────────────────────────────────────────────────
    def _autostart_file(self) -> Path:
        return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "autostart" / f"{product.APP_ID}.desktop"

    def autostart(self) -> Autostart:
        file = self._autostart_file()
        return Autostart(file.exists(), i18n.msg("autostart.xdg"), str(file))

    def set_autostart(self, command: list[str], enabled: bool) -> bool:
        file = self._autostart_file()
        try:
            if not enabled:
                file.unlink(missing_ok=True)
                return True
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(
                "[Desktop Entry]\nType=Application\n"
                f"Name={product.NAME}\nExec={shlex.join(command)}\nIcon={product.APP_ID}\n"
                "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n"
            )
            return True
        except OSError:
            return False

    # ── credentials: Secret Service ─────────────────────────────────────────
    def _libsecret(self):
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        schema = Secret.Schema.new(f"{product.NAMESPACE}.Secret", Secret.SchemaFlags.NONE, {"name": Secret.SchemaAttributeType.STRING})
        return Secret, schema

    def credential_backend(self) -> str:
        try:
            self._libsecret()
            return i18n.msg("credstore.secret_service")
        except Exception:
            return "secret-tool" if have("secret-tool") else "none"

    def credential_get(self, service: str, name: str) -> str | None:
        try:
            Secret, schema = self._libsecret()
            return Secret.password_lookup_sync(schema, {"name": name}, None)
        except Exception:
            pass
        if have("secret-tool"):
            res = run(["secret-tool", "lookup", "service", service, "name", name], timeout=10)
            return res.out.strip() if res.ok and res.out.strip() else None
        return None

    def credential_put(self, service: str, name: str, value: str) -> bool:
        try:
            Secret, schema = self._libsecret()
            if Secret.password_store_sync(schema, {"name": name}, Secret.COLLECTION_DEFAULT, f"{product.SHORT} {name}", value, None):
                return True
        except Exception:
            pass
        if have("secret-tool"):
            return run(["secret-tool", "store", "--label", f"{product.SHORT} {name}", "service", service, "name", name], stdin=value, timeout=10).ok
        return False

    def credential_delete(self, service: str, name: str) -> bool:
        try:
            Secret, schema = self._libsecret()
            return bool(Secret.password_clear_sync(schema, {"name": name}, None))
        except Exception:
            pass
        if have("secret-tool"):
            return run(["secret-tool", "clear", "service", service, "name", name], timeout=10).ok
        return False

    # ── permissions ─────────────────────────────────────────────────────────
    def permissions(self) -> list[Permission]:
        info = self.info()
        out = [
            Permission.of("linux", "autostart", "granted" if self.autostart().enabled else "missing")
        ]
        if info.session == "wayland":
            out.append(
                Permission.of("linux", "input-capture", "unknown")
            )
        return out
