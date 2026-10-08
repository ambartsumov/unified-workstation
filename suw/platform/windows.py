"""Windows adapter: known folders, per-user Run key, Credential Manager, toast notifications.

Everything here is per-user: nothing needs elevation. Background work is started from the
per-user Run key rather than a Windows service, so the product installs without administrator
rights.
"""

from __future__ import annotations

import os
import platform as pyplatform
import subprocess
from pathlib import Path

from .. import product
from ..core.proc import Result, run
from .base import Autostart, DesktopInfo, Permission, Platform

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _env_dir(name: str, fallback: Path) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value else fallback


def command_line(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


class Windows(Platform):
    name = "windows"
    service_manager = "windows"

    def config_dir(self, home: Path) -> Path:
        return _env_dir("APPDATA", home / "AppData" / "Roaming") / product.WINDOWS_DIR

    def state_dir(self, home: Path) -> Path:
        return _env_dir("LOCALAPPDATA", home / "AppData" / "Local") / product.WINDOWS_DIR / "State"

    def cache_dir(self, home: Path) -> Path:
        return _env_dir("LOCALAPPDATA", home / "AppData" / "Local") / product.WINDOWS_DIR / "Cache"

    def default_workspace(self, home: Path) -> Path:
        # A Desktop redirected into OneDrive would be synchronised twice; stay beside it.
        return home / "Desktop" / "Work"

    def executable_suffixes(self) -> list[str]:
        return [".exe", ".cmd", ".bat", ""]

    def extra_bin_dirs(self) -> list[Path]:
        local = _env_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        programs = _env_dir("ProgramFiles", Path(r"C:\Program Files"))
        return [
            local / "Microsoft" / "WinGet" / "Links",
            local / "Programs" / "Syncthing",
            programs / "Syncthing",
            programs / "Deskflow",
            programs / "Tailscale",
            programs / "WezTerm",
            programs / "Git" / "cmd",
        ]

    def info(self) -> DesktopInfo:
        notes = []
        if "OneDrive" in str(Path.home() / "Desktop") or os.environ.get("OneDrive"):
            notes.append("OneDrive may back up the Desktop: keep the work folder out of a OneDrive-managed folder")
        return DesktopInfo("windows", pyplatform.version(), pyplatform.machine(), "Explorer", "", "windows", notes)

    def open_path(self, path: Path) -> bool:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        except (OSError, AttributeError):
            return False

    def open_url(self, url: str) -> bool:
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except (OSError, AttributeError):
            return super().open_url(url)

    def notify(self, app: str, title: str, body: str, urgent: bool = False) -> bool:
        def quote(text: str) -> str:
            return "'" + text.replace("'", "''") + "'"

        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$n = $t.GetElementsByTagName('text');"
            f"$n.Item(0).AppendChild($t.CreateTextNode({quote(title)})) > $null;"
            f"$n.Item(1).AppendChild($t.CreateTextNode({quote(body)})) > $null;"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({quote(product.WINDOWS_APP_USER_MODEL_ID)})"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=10).ok

    # ── services: a scheduled task per background component ─────────────────
    def service(self, unit: str, label: str, action: str) -> Result:
        task = f"\\{product.WINDOWS_DIR}\\{label}"
        if action == "status":
            res = run(["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"], timeout=15)
            return Result(0 if res.ok and "Running" in res.out else 3, res.out, res.err)
        if action == "stop":
            return run(["schtasks", "/End", "/TN", task], timeout=15)
        if action == "restart":
            run(["schtasks", "/End", "/TN", task], timeout=15)
        return run(["schtasks", "/Run", "/TN", task], timeout=15)

    # ── autostart: HKCU Run key (no elevation) ──────────────────────────────
    def autostart(self) -> Autostart:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, product.WINDOWS_DIR)
            return Autostart(True, "Startup apps (current user)", str(value))
        except (ImportError, OSError):
            return Autostart(False, "Startup apps (current user)")

    def set_autostart(self, command: list[str], enabled: bool) -> bool:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, product.WINDOWS_DIR, 0, winreg.REG_SZ, command_line(command))
                else:
                    try:
                        winreg.DeleteValue(key, product.WINDOWS_DIR)
                    except FileNotFoundError:
                        pass
            return True
        except (ImportError, OSError):
            return False

    # ── credentials: Credential Manager (DPAPI-protected, per user) ─────────
    def _advapi(self):
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        advapi.CredWriteW.restype = wintypes.BOOL
        advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi.CredDeleteW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        return ctypes, advapi, CREDENTIAL

    @staticmethod
    def _target(service: str, name: str) -> str:
        return f"{service}:{name}"

    def credential_backend(self) -> str:
        try:
            self._advapi()
            return "Windows Credential Manager"
        except (ImportError, AttributeError, OSError):
            return "none"

    def credential_get(self, service: str, name: str) -> str | None:
        try:
            ctypes, advapi, CREDENTIAL = self._advapi()
        except (ImportError, AttributeError, OSError):
            return None
        pointer = ctypes.POINTER(CREDENTIAL)()
        if not advapi.CredReadW(self._target(service, name), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None
        try:
            cred = pointer.contents
            return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-16-le")
        finally:
            advapi.CredFree(pointer)

    def credential_put(self, service: str, name: str, value: str) -> bool:
        try:
            ctypes, advapi, CREDENTIAL = self._advapi()
        except (ImportError, AttributeError, OSError):
            return False
        blob = value.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else (ctypes.c_ubyte * 1)()
        cred = CREDENTIAL()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = self._target(service, name)
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = name
        cred.Comment = f"{product.NAME} credential"
        return bool(advapi.CredWriteW(ctypes.byref(cred), 0))

    def credential_delete(self, service: str, name: str) -> bool:
        try:
            _, advapi, _ = self._advapi()
        except (ImportError, AttributeError, OSError):
            return False
        return bool(advapi.CredDeleteW(self._target(service, name), _CRED_TYPE_GENERIC, 0))

    # ── permissions ─────────────────────────────────────────────────────────
    def permissions(self) -> list[Permission]:
        return [
            Permission(
                "firewall",
                "Windows Firewall (private networks)",
                "An allow rule for file sync and keyboard/mouse sharing on private networks.",
                "Other paired computers on your home or office network connecting to this one.",
                "Windows asks once, the first time sync starts. Choose Private networks only; public networks stay blocked.",
                "Windows Security → Firewall & network protection → Allow an app through firewall → remove the entry.",
                "unknown",
                "ms-settings:network",
            ),
            Permission(
                "elevated-windows",
                "Controlling administrator windows",
                "Nothing is granted by default.",
                "Using a shared keyboard inside a window that runs as administrator (for example an elevated terminal).",
                "Windows blocks input from a normal program into elevated windows. Shared keyboard and mouse pause there until you click back to a normal window.",
                "Nothing to revoke.",
                "not_needed",
            ),
            Permission(
                "autostart",
                "Start with Windows",
                "An entry under Startup apps for your user account.",
                "Keeps sync and status running without opening the application first.",
                "The background component starts when you sign in. No administrator rights are used.",
                "Settings → Apps → Startup, or Settings → General inside the application.",
                "granted" if self.autostart().enabled else "missing",
                "ms-settings:startupapps",
            ),
        ]
