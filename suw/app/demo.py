"""Demo mode: the whole window, driven by fixtures.

Nothing here reads or writes the user's configuration, starts a process, opens a connection or
touches a file. Changes made in the window live in memory and vanish when it closes. Used for
screenshots, onboarding walkthroughs, UI tests and contributors without a second computer.

Every name and address is a placeholder from the documentation ranges (RFC 5737 / RFC 2606).
"""

from __future__ import annotations

import copy
import time

from .. import product
from ..core import i18n, settings
from ..core.config import Config, _flatten, read_toml
from ..core import paths
from .backend import Backend, Problem

_NOW = time.time


def _defaults() -> dict:
    return read_toml(paths.resources() / "config" / "defaults.toml")


FEATURES = [
    {"id": "workspace", "status": "SUPPORTED", "reason": "A normal folder on this computer. It works without any other component.", "action": "", "needs": []},
    {"id": "file_sync", "status": "SUPPORTED", "reason": "Syncthing is installed. Files are sent directly between your paired computers.", "action": "", "needs": ["syncthing"]},
    {"id": "peripherals", "status": "PARTIALLY_SUPPORTED", "reason": "On Wayland, sharing depends on your desktop's input-capture portal. Your desktop asks for approval the first time.", "action": "", "needs": ["deskflow"]},
    {"id": "shared_clipboard", "status": "PARTIALLY_SUPPORTED", "reason": "Text is shared. On Wayland, images and files may not be, depending on the desktop.", "action": "", "needs": ["deskflow"]},
    {"id": "terminal_clipboard", "status": "SUPPORTED", "reason": "Copying inside a server session reaches this computer's clipboard (OSC 52, through WezTerm).", "action": "", "needs": ["wezterm"]},
    {"id": "workstation_layout", "status": "SUPPORTED", "reason": "Editor, terminal and browser open on their own workspaces.", "action": "", "needs": []},
    {"id": "private_network", "status": "SUPPORTED", "reason": "Tailscale is installed: paired computers and servers are reachable from anywhere, privately.", "action": "", "needs": ["tailscale"]},
    {"id": "servers", "status": "SUPPORTED", "reason": "OpenSSH is installed. Server identity is always verified.", "action": "", "needs": ["ssh"]},
    {"id": "cloud_projects", "status": "SUPPORTED", "reason": "Selected projects are sent with rsync over SSH.", "action": "", "needs": ["ssh", "rsync"]},
    {"id": "credentials", "status": "SUPPORTED", "reason": "Passwords are kept in the system keyring, never in configuration files.", "action": "", "needs": []},
    {"id": "background_service", "status": "SUPPORTED", "reason": "Sync and status keep running in the background.", "action": "", "needs": []},
    {"id": "autostart", "status": "SUPPORTED", "reason": "Start at sign-in uses: XDG autostart.", "action": "", "needs": []},
    {"id": "notifications", "status": "SUPPORTED", "reason": "Important events are shown as system notifications.", "action": "", "needs": []},
    {"id": "file_watching", "status": "SUPPORTED", "reason": "Changes are noticed immediately.", "action": "", "needs": []},
    {"id": "git", "status": "SUPPORTED", "reason": "Repository status and history.", "action": "", "needs": ["git"]},
    {"id": "assistant", "status": "NOT_INSTALLED", "reason": "Claude Code is not installed. Everything else works without it.", "action": "See the Claude Code installation guide.", "needs": ["claude"]},
]
COMPONENTS = [
    {"id": ident, "name": name, "purpose": purpose, "installed": installed, "version": version, "install_hint": "" if installed else "See the installation guide.", "path": ""}
    for ident, name, purpose, installed, version in [
        ("git", "Git", "Project history and repository status", True, "2.47.1"),
        ("ssh", "OpenSSH", "Connecting to Home and Cloud servers", True, "9.9"),
        ("syncthing", "Syncthing", "Keeping the work folder identical on every computer", True, "2.0.10"),
        ("deskflow", "Deskflow", "Sharing one keyboard and mouse between computers", True, "1.25.0"),
        ("tailscale", "Tailscale", "A private network between your computers", True, "1.88.3"),
        ("claude", "Claude Code", "AI coding assistant working inside your work folder", False, ""),
        ("wezterm", "WezTerm", "Terminal with clipboard that works over SSH", True, "20240203"),
        ("tmux", "tmux", "Terminal sessions that survive a dropped connection", True, "3.5"),
        ("rsync", "rsync", "Sending a selected project to a Cloud server", True, "3.3.0"),
        ("openssl", "OpenSSL", "Creating the certificate used for keyboard/mouse sharing", True, "3.4.0"),
        ("editor", "Visual Studio Code", "Opening projects", True, ""),
        ("terminal", "WezTerm", "Running commands and server sessions", True, ""),
    ]
]
PERMISSIONS = [
    {"id": "autostart", "title": "Start at sign-in", "what": "An entry in your desktop's autostart list.", "why": "Keeps sync and status running without opening the application first.", "effect": "The background service starts when you sign in. Nothing runs as administrator.", "revoke": "Settings → General → Start at sign-in.", "state": "granted", "settings_url": ""},
    {"id": "input-capture", "title": "Share keyboard and mouse (Wayland)", "what": "Permission for the sharing tool to capture input, asked by your desktop.", "why": "Moving one keyboard and mouse between your computers.", "effect": "Your desktop shows its own prompt the first time sharing starts.", "revoke": "Your desktop's privacy settings.", "state": "unknown", "settings_url": ""},
]
DEMO_CODE = "UW1-DEMO42-AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-FFFFF"


class DemoBackend(Backend):
    demo = True

    def __init__(self) -> None:
        super().__init__()
        self.data = copy.deepcopy(_defaults())
        self.data.update({"onboarding": {"done": False, "kind": "personal"}})
        self.mode = "default"
        self.paused = False
        self.conflicts = ["notes/meeting.sync-conflict-20260114-101502-WKSTN2A.md", "research/plan.sync-conflict-20260113-184410-WKSTN2A.txt"]
        self.stations = [
            {"name": "workstation-1", "self": True, "platform": "linux", "version": product.VERSION, "online": True, "paired_sync": True, "paired_input": True, "last_seen": 0, "completion": None},
            {"name": "workstation-2", "self": False, "platform": "macos", "version": product.VERSION, "online": True, "paired_sync": True, "paired_input": True, "last_seen": "", "completion": 100.0},
        ]
        self.home = {"role": "home", "configured": True, "status": "ONLINE", "host": "home.example.net", "user": "demo", "port": 22, "report": {"online": True, "cpu_pct": 6, "ram_used_pct": 38, "disk_used_pct": 41, "os": "Linux"}}
        self.cloud = {"role": "cloud", "configured": False, "status": "NOT_CONFIGURED", "host": "", "user": "", "port": 22, "label": "", "hardware": {}, "report": {}}
        self.snaps = [{"id": "20260114-090000-before-settings-change", "created": _NOW() - 86400, "reason": "before-settings-change", "version": product.VERSION, "files": ["config.toml"], "bytes": 812}]
        self.log = [
            {"timestamp": "2026-01-14T10:15:02+00:00", "event": "work.conflict", "level": "warn", "message": "two versions of notes/meeting.md were kept"},
            {"timestamp": "2026-01-14T09:58:40+00:00", "event": "pairing.accepted", "level": "info", "message": "paired with workstation-2"},
            {"timestamp": "2026-01-14T09:41:11+00:00", "event": "onboarding.completed", "level": "info", "message": "first-run setup finished (workstation)"},
        ]

    @property
    def cfg(self) -> Config:  # type: ignore[override]
        return Config(self.data)

    # start-up
    def bootstrap(self) -> dict:
        lang = i18n.resolve(str(self.cfg.get("general.language", "auto")))
        return {
            "product": product.as_dict(), "demo": True, "language": lang, "languages": i18n.LANGUAGES, "catalog": i18n.merged(lang),
            "theme": str(self.cfg.get("general.theme", "auto")),
            "onboarding": {"done": bool(self.cfg.get("onboarding.done")), "kind": self.cfg.get("onboarding.kind"), "existing_setup": False, "suggested_workspace": "~/Desktop/Work", "device": "workstation-1"},
            "platform": {"os": "linux", "os_version": "Demo Linux", "arch": "x86_64", "desktop": "GNOME", "session": "wayland", "service_manager": "systemd", "notes": []},
            "migration": {"migrated": False}, "config_warnings": [],
        }

    def set_language(self, language: str) -> dict:
        self._set("general.language", language)
        lang = i18n.resolve(language)
        return {"language": lang, "catalog": i18n.merged(lang)}

    def _set(self, key: str, value) -> None:
        node = self.data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # dashboard
    def dashboard(self) -> dict:
        workstation = self.cfg.get("onboarding.kind") == "workstation"
        sync = "NOT_CONFIGURED" if not workstation else "PAUSED" if self.paused else "CONFLICT" if self.conflicts else "SYNCED"
        attention = []
        if sync == "CONFLICT":
            attention.append({"code": "sync_conflict", "page": "sync"})
        if sync == "PAUSED":
            attention.append({"code": "sync_paused", "page": "sync"})
        return {
            "ts": _NOW(), "ready": not attention, "mode": self.mode, "kind": self.cfg.get("onboarding.kind"), "device": "workstation-1",
            "workspace": {"state": "READY", "path": str(self.cfg.get("work.path"))},
            "sync": {"state": sync, "reason": sync.lower(), "need_items": 0, "last_sync": _NOW() - 42, "peers": len(self.stations) - 1 if workstation else 0},
            "workstations": self.stations if workstation else self.stations[:1],
            "peripherals": {"state": "CONNECTED" if workstation and len(self.stations) > 1 else "NOT_CONFIGURED", "role": "server", "links": 1},
            "servers": [self.home, self.cloud],
            "updates": {"current": product.VERSION, "channel": product.channel(), "available": ""},
            "attention": attention, "background": workstation,
        }

    def set_mode(self, mode: str, save: bool = False, close: bool = False) -> dict:
        self.mode = "workstation" if mode == "workstation" else "default"
        return {"mode": self.mode, "steps": ["editor: opened demo-project", "terminal: opened", "browser: 2 tabs"] if self.mode == "workstation" else ["Workstation windows left open"]}

    def capabilities(self) -> dict:
        return {"platform": self.bootstrap()["platform"], "components": COMPONENTS, "features": FEATURES, "editors": [{"id": "code", "name": "Visual Studio Code"}], "terminals": [{"id": "wezterm", "name": "WezTerm"}], "permissions": PERMISSIONS}

    def open_permission(self, permission: str) -> dict:
        return {"opened": False}

    def selftest(self) -> dict:
        rows = [
            {"id": "application", "status": "pass", "detail": f"{product.NAME} {product.VERSION} (demo)", "fix": ""},
            {"id": "workspace", "status": "pass", "detail": "~/Desktop/Work", "fix": ""},
            {"id": "sync", "status": "pass", "detail": "Syncthing is installed.", "fix": ""},
            {"id": "peripherals", "status": "warn", "detail": "On Wayland your desktop asks for approval the first time.", "fix": ""},
            {"id": "clipboard", "status": "warn", "detail": "Text is shared; images depend on the desktop.", "fix": ""},
            {"id": "terminal", "status": "pass", "detail": "WezTerm", "fix": ""},
            {"id": "git", "status": "pass", "detail": "Git 2.47.1", "fix": ""},
            {"id": "ssh", "status": "pass", "detail": "OpenSSH 9.9", "fix": ""},
            {"id": "network", "status": "pass", "detail": "Tailscale connected", "fix": ""},
            {"id": "credentials", "status": "pass", "detail": "system keyring", "fix": ""},
            {"id": "assistant", "status": "skip", "detail": "Claude Code is not installed. Everything else works without it.", "fix": "install"},
            {"id": "home", "status": "pass", "detail": "online", "fix": ""},
            {"id": "cloud", "status": "skip", "detail": "not set up (optional)", "fix": ""},
            {"id": "updates", "status": "pass", "detail": "channel: stable", "fix": ""},
        ]
        return {"verdict": "warn", "rows": rows, "details": []}

    def health(self) -> dict:
        return {"checks": [{"level": "PASS", "title": "Demo Linux", "detail": "device 'workstation-1'", "fix": "", "section": "Workstation"}, {"level": "PASS", "title": "Work folder in sync", "detail": "2 computers", "fix": "", "section": "Work folder"}, {"level": "WARN", "title": "2 conflict copies to review", "detail": "both versions kept", "fix": "", "section": "Work folder"}], "verdict": "READY WITH WARNINGS", "state": {}}

    def repair(self, what: str) -> dict:
        return {"done": [f"demo: '{what}' would be repaired here (backup → repair → verify)"], "selftest": self.selftest()}

    # settings
    def settings_get(self) -> dict:
        data = settings.describe(self.cfg)
        data.update(autostart={"enabled": True, "mechanism": "XDG autostart"}, editors=[{"id": "code", "name": "Visual Studio Code"}], terminals=[{"id": "wezterm", "name": "WezTerm"}], credential_store="Secret Service (system keyring)", devices=[s["name"] for s in self.stations], locations={"config": "~/.config/suw", "state": "~/.local/state/suw", "cache": "~/.cache/suw"})
        return data

    def settings_check(self, changes: dict) -> dict:
        return {"errors": self._errors(changes)[1]}

    def _errors(self, changes: dict) -> tuple[dict, dict]:
        clean, errors = {}, {}
        from ..core import config

        for key, raw in changes.items():
            field = settings.BY_KEY.get(key)
            if field is None or field.readonly:
                errors[key] = "this setting can not be changed here"
                continue
            try:
                clean[key] = settings.coerce(field, raw)
            except ValueError as exc:
                errors[key] = str(exc)
        candidate: dict = {}
        for key, value in clean.items():
            config._set_path(candidate, key, value)
        for message in config.validate(candidate)[0]:
            errors[message.split(":", 1)[0]] = message.split(":", 1)[1].strip()
        return clean, errors

    def settings_apply(self, changes: dict) -> dict:
        clean, errors = self._errors(changes)
        if errors:
            return {"errors": errors, "changed": []}
        for key, value in clean.items():
            self._set(key, value)
        return {"errors": {}, "changed": sorted(clean), "snapshot": ""}

    def settings_reset(self, keys: list[str]) -> dict:
        defaults = _flatten(_defaults())
        for key in keys:
            if key in defaults:
                self._set(key, defaults[key])
        return {"changed": sorted(keys), "snapshot": ""}

    def browse(self, path: str = "") -> dict:
        tree = {"~": ["Desktop", "Documents", "Projects"], "~/Desktop": ["Work"], "~/Documents": ["Notes"], "~/Projects": ["demo-project"], "~/Desktop/Work": ["notes", "research", "demo-project"]}
        path = path if path in tree else "~"
        return {"path": path, "parent": path.rsplit("/", 1)[0] if "/" in path else "", "folders": tree.get(path, []), "home": "~", "problem": "the whole home folder (or a folder above it) can not be shared: it holds system identity and credentials" if path == "~" else ""}

    def make_folder(self, parent: str, name: str) -> dict:
        return {**self.browse(parent), "path": f"{parent}/{name}", "folders": [], "problem": ""}

    def open_path(self, what: str = "workspace") -> dict:
        return {"opened": False}

    # first run
    def onboarding_plan(self, choices: dict) -> dict:
        steps = [{"id": "settings", "touches": ["~/.config/suw", "~/.local/state/suw"], "optional": False, "available": True, "reason": ""}, {"id": "workspace", "touches": [choices.get("workspace") or "~/Desktop/Work"], "optional": False, "available": True, "reason": ""}]
        if choices.get("kind") == "workstation":
            if choices.get("sync", True):
                steps.append({"id": "sync", "touches": ["a private Syncthing configuration inside the application's data folder", "a background sync service for your user account"], "optional": True, "available": True, "reason": ""})
            if choices.get("peripherals"):
                steps.append({"id": "peripherals", "touches": ["a private Deskflow profile and certificate", "a background sharing service for your user account"], "optional": True, "available": True, "reason": ""})
            steps.append({"id": "service", "touches": ["a background service for your user account (status, health, reconnect)"], "optional": False, "available": True, "reason": ""})
            for ident in choices.get("integrations") or ["ssh", "desktop"]:
                steps.append({"id": f"integration.{ident}", "touches": ["a marked block in one configuration file"], "optional": True, "available": True, "reason": ""})
        return {"steps": steps, "workspace": choices.get("workspace") or "~/Desktop/Work", "workspace_problem": "", "existing": {"exists": False, "entries": 0}}

    def onboarding_apply(self, choices: dict) -> dict:
        kind = "workstation" if choices.get("kind") == "workstation" else "personal"
        self._set("onboarding.done", True)
        self._set("onboarding.kind", kind)
        self._set("work.path", choices.get("workspace") or "~/Desktop/Work")
        self._set("peripherals.enabled", bool(choices.get("peripherals")))
        return {"ok": True, "kind": kind, "steps": [{"id": s["id"], "status": "ok", "note": ""} for s in self.onboarding_plan(choices)["steps"]]}

    def onboarding_reset(self) -> dict:
        self._set("onboarding.done", False)
        return {"ok": True}

    # workstations
    def workstations(self) -> dict:
        return {"rows": self.stations, "device": "workstation-1", "capabilities": FEATURES}

    def pair_offer(self) -> dict:
        return {"code": DEMO_CODE, "offer": {"name": "workstation-1", "platform": "linux", "files": 128, "bytes": 48_300_000, "hosts": ["192.0.2.10"]}}

    def pair_review(self, code: str) -> dict:
        if not code.strip().upper().startswith("UW1-"):
            raise Problem("pairing_code_invalid", ["close"], detail="That is not a pairing code. Copy the whole code shown under “Add Workstation” on the other computer.")
        return {"mine": {"name": "workstation-1", "files": 128, "bytes": 48_300_000}, "theirs": {"name": "workstation-3", "platform": "windows", "version": product.VERSION, "files": 12, "bytes": 2_100_000, "hosts": ["192.0.2.30"]}, "confirmation": "418207", "problems": [], "sync": True, "peripherals": True, "both_have_files": True, "merge_note": "Both folders already contain files. They will be merged: nothing is deleted, and a file that differs on the two computers is kept twice so you can choose.", "my_code": DEMO_CODE}

    def pair_accept(self, code: str, confirmation: str, sync: bool = True, share_input: bool = True, merge_confirmed: bool = False) -> dict:
        if confirmation.strip() != "418207":
            raise Problem("pairing_number_mismatch", ["close"])
        if not merge_confirmed:
            raise Problem("pairing_merge_unconfirmed", ["close"])
        self.stations.append({"name": "workstation-3", "self": False, "platform": "windows", "version": product.VERSION, "online": True, "paired_sync": sync, "paired_input": share_input, "last_seen": "", "completion": 87.5})
        return {"device": "workstation-3", "sync": sync, "peripherals": share_input}

    def unpair(self, name: str) -> dict:
        self.stations = [s for s in self.stations if s["name"] != name or s["self"]]
        return {"ok": True}

    def rename_device(self, old: str, new: str) -> dict:
        for station in self.stations:
            if station["name"] == old:
                station["name"] = new.strip().lower()
        return {"ok": True}

    # workspace / sync
    def workspace(self) -> dict:
        return {"path": str(self.cfg.get("work.path")), "exists": True, "files": 128, "bytes": 48_300_000, "partial": False, "large": [{"path": "research/dataset.tar", "bytes": 3_400_000_000, "held": True}], "large_threshold_mb": 2048, "large_policy": "hold", "ignored": 3, "repos": ["demo-project"], "rules": [".git", "node_modules", ".venv", "__pycache__"], "boundary": {"shared": "workspace", "local": ["operating system credentials", "SSH host keys", "private network identity", "sign-in sessions", "this computer's identity"]}}

    def sync_status(self) -> dict:
        state = "PAUSED" if self.paused else "CONFLICT" if self.conflicts else "SYNCED"
        return {"state": state, "reason": state.lower(), "installed": True, "enabled": True, "running": True, "paused": self.paused, "path": str(self.cfg.get("work.path")), "peers": [{"name": s["name"], "connected": s["online"], "completion": s["completion"], "need_items": 0, "last_seen": ""} for s in self.stations if not s["self"]], "need_items": 0, "need_bytes": 0, "outgoing_items": 0, "last_sync": _NOW() - 42, "errors": [], "conflicts": list(self.conflicts), "large": [{"path": "research/dataset.tar", "bytes": 3_400_000_000, "held": True}], "files": 128, "bytes": 48_300_000, "versions": 2}

    def sync_action(self, action: str) -> dict:
        if action == "pause":
            self.paused = True
        elif action == "resume":
            self.paused = False
        return self.sync_status()

    def sync_resolve(self, conflict: str, keep: str) -> dict:
        self.conflicts = [c for c in self.conflicts if c != conflict]
        return {"kept": keep, "other_version_moved_to": "the application's trash (kept 30 days)"}

    def sync_versions(self, prefix: str = "") -> dict:
        return {"versions": [{"path": "notes/ideas~20260113-171500.md", "bytes": 4210, "mtime": _NOW() - 90000}, {"path": "research/outline~20260112-093000.txt", "bytes": 1800, "mtime": _NOW() - 180000}]}

    def sync_restore(self, path: str) -> dict:
        return {"restored_as": path.split("~")[0] + ".restored.md"}

    # peripherals
    def peripherals_status(self) -> dict:
        return {"enabled": True, "role": "server", "screen": "workstation-1", "port": 24800, "installed": True, "state": "CONNECTED", "links": 1, "fingerprint": "A1B2 C3D4 E5F6 … 9A0B", "screens": [s["name"] for s in self.stations], "server_device": "workstation-1", "peer_side": str(self.cfg.get("peripherals.peer_side", "right")), "clipboard": True, "exposed": False, "feature": FEATURES[2], "clipboard_feature": FEATURES[3], "permissions": [PERMISSIONS[1]]}

    def peripherals_action(self, action: str) -> dict:
        return self.peripherals_status()

    # servers
    def servers(self) -> dict:
        return {"rows": [self.home, self.cloud], "cloud_history": [], "ssh": True, "keys": ["~/.ssh/id_ed25519"], "user": "demo"}

    def server_scan(self, host: str, port: int = 22) -> dict:
        return {"host": host, "port": port, "fingerprint": "SHA256:DEMOdemoDEMOdemoDEMOdemoDEMOdemoDEMOdemo12", "key_type": "ssh-ed25519", "already_trusted": False, "identity_changed": False}

    def home_save(self, host: str, user: str, port: int = 22, identity: str = "", fingerprint: str = "") -> dict:
        if not fingerprint:
            raise Problem("fingerprint_unconfirmed", ["review_fingerprint"])
        self.home.update(configured=True, status="ONLINE", host=host, user=user, port=port)
        return self.home

    def home_remove(self) -> dict:
        self.home.update(configured=False, status="NOT_CONFIGURED", host="", user="", report={})
        return {"ok": True}

    def server_test(self, role: str) -> dict:
        row = self.home if role == "home" else self.cloud
        if not row["configured"]:
            raise Problem("server_not_configured", ["close"])
        return row

    def server_connect(self, role: str) -> dict:
        return {"opened": False}

    def cloud_enroll(self, host: str, user: str, port: int = 22, fingerprint: str = "", provider: str = "custom") -> dict:
        if not fingerprint:
            raise Problem("fingerprint_unconfirmed", ["review_fingerprint"])
        previous = self.cloud["label"]
        self.cloud.update(configured=True, status="ONLINE", host=host, user=user, port=port, label="cloud-20260114", hardware={"gpu": "1× demo GPU 24GB", "cores": 16, "ram_gb": 64}, report={"online": True, "cpu_pct": 3, "ram_used_pct": 9, "disk_used_pct": 12})
        return {"label": "cloud-20260114", "retired": previous, "previous": previous, "stages": [{"id": "reachability", "ok": True}, {"id": "fingerprint", "ok": True}, {"id": "step", "ok": True, "note": "SSH connection verified"}, {"id": "step", "ok": True, "note": "health check passed"}], "hardware": self.cloud["hardware"]}

    def cloud_remove(self) -> dict:
        self.cloud.update(configured=False, status="NOT_CONFIGURED", host="", label="", hardware={}, report={})
        return {"released": "cloud-20260114"}

    def cloud_projects(self) -> dict:
        return {"candidates": ["demo-project", "research"], "sent": [], "confirm_mb": 500, "configured": self.cloud["configured"]}

    def cloud_plan(self, name: str) -> dict:
        return {"name": name, "files": 96, "bytes": 31_500_000, "target": f"cloud:~/work/{name}", "excluded": [".git", ".claude", "node_modules"], "sensitive": [".env"], "needs_confirmation": ["this project contains a file that looks like credentials: .env"], "summary": []}

    def cloud_push(self, name: str, confirmed: bool = False) -> dict:
        if not confirmed:
            raise Problem("transfer_unconfirmed", ["close"])
        return {"name": name, "files": 96, "bytes": 31_500_000, "verified": True}

    def cloud_pull(self, name: str) -> dict:
        return {"name": name, "changed": 3}

    # assistants / git
    def assistants(self) -> dict:
        return {"assistants": [{"id": "claude", "name": "Claude Code", "installed": False, "version": "", "projects": [{"project": "demo-project", "path": "~/Desktop/Work/demo-project", "shared": False, "shared_files": 0, "local_files": 3, "error": "", "local_overrides": True, "state": [{"path": "CLAUDE.md", "kind": "PROJECT_LOCAL", "portable": True, "present": True, "what": "instructions"}, {"path": ".claude/settings.json", "kind": "PROJECT_LOCAL", "portable": True, "present": True, "what": "settings"}, {"path": ".claude/settings.local.json", "kind": "MACHINE_SPECIFIC", "portable": False, "present": True, "what": "local_settings"}]}], "global_state": [{"path": "~/.claude/CLAUDE.md", "kind": "USER_GLOBAL", "portable": False, "present": True, "what": "user_instructions"}, {"path": "~/.claude/.credentials.json", "kind": "MACHINE_SPECIFIC", "portable": False, "present": True, "what": "credentials"}, {"path": "~/.claude/projects", "kind": "SESSION", "portable": False, "present": True, "what": "transcripts"}], "actions": []}]}

    def assistant_share_memory(self, project: str, dry: bool = True) -> dict:
        return {"project": project, "changed": not dry, "dry": dry}

    def git_projects(self) -> dict:
        return {"installed": True, "rows": [{"name": "demo-project", "branch": "main", "status": "clean", "dirty": 0, "ahead": 0, "behind": 0, "upstream": True}, {"name": "research", "branch": "draft", "status": "dirty", "dirty": 4, "ahead": 1, "behind": 0, "upstream": True}], "autosync": False}

    # recovery
    def recovery(self) -> dict:
        return {"snapshots": self.snaps, "versions": 2, "trash_days": 30, "conflicts": len(self.conflicts), "installers": [], "changes": ["remove block         ~/.ssh/config", "remove               ~/.ssh/suw.conf"], "supervised": [], "config_warnings": [], "export": {"portable": 14, "machine": 4, "secret_references": [], "devices": [s["name"] for s in self.stations], "note": "Passwords and keys are never part of an export. They stay in this computer's credential store."}}

    def snapshot_create(self) -> dict:
        ident = time.strftime("%Y%m%d-%H%M%S") + "-manual"
        self.snaps.insert(0, {"id": ident, "created": _NOW(), "reason": "manual", "version": product.VERSION, "files": ["config.toml"], "bytes": 800})
        return {"id": ident}

    def snapshot_restore(self, snapshot: str) -> dict:
        return {"restored": ["config.toml"]}

    def config_reset(self) -> dict:
        self.data = {**copy.deepcopy(_defaults()), "onboarding": self.data["onboarding"]}
        return {"snapshot": "demo"}

    def config_export(self, include_machine: bool = False, include_devices: bool = False) -> dict:
        return self._offer_download("unified-workstation-settings-demo.zip", b"PK\x05\x06" + b"\x00" * 18)

    def config_import_preview(self, data: str) -> dict:
        return {"from_version": product.VERSION, "changes": ["general.theme"], "machine": [], "secret_references": [], "devices": []}

    def config_import(self, data: str, include_machine: bool = False) -> dict:
        return {"applied": 1, "snapshot": "demo", "secrets_to_reenter": []}

    def support_bundle(self) -> dict:
        return self._offer_download("unified-workstation-support-demo.zip", b"PK\x05\x06" + b"\x00" * 18)

    def uninstall_preview(self) -> dict:
        return {"removed": ["remove block         ~/.ssh/config", "stop                 background service"], "kept": ["~/Desktop/Work", "your projects and Git repositories", "files on your Home and Cloud servers", "installed tools (Syncthing, Deskflow, Tailscale…)", "passwords in the system credential store"], "optional": ["~/.config/suw", "~/.local/state/suw"]}

    # updates / history
    def update_status(self) -> dict:
        return {"current": product.VERSION, "channel": str(self.cfg.get("updates.channel", "stable")), "channel_running": product.channel(), "install_kind": "demo", "offer": None, "error": "", "checked": _NOW() - 3600, "installers": []}

    def update_check(self) -> dict:
        return self.update_status()

    def update_download(self) -> dict:
        raise Problem("no_update", ["close"])

    def help_info(self) -> dict:
        return {"version": product.VERSION, "channel": product.channel(), "platform": self.bootstrap()["platform"], "links": dict(Backend.LINKS), "license": product.LICENSE}

    def open_link(self, which: str) -> dict:
        return {"opened": False, "url": Backend.LINKS.get(which, "")}

    def history(self, search: str = "", level: str = "", limit: int = 200) -> dict:
        rows = [r for r in self.log if (not search or search.lower() in r["message"].lower()) and (not level or r["level"] == level)]
        return {"rows": rows}
