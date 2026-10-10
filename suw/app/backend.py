"""Everything the application window can ask for or do, as plain data in and out.

The window never touches files or processes itself: it calls one of the public methods of
`Backend`. That keeps the user interface replaceable, lets the demo mode swap the whole class
for fixtures (`demo.DemoBackend`), and gives every failure one shape: a `Problem` that says
what happened, why, and what can be done — never a stack trace or an exit code.
"""

from __future__ import annotations

import base64
import itertools
import os
import secrets as pysecrets
import time
from pathlib import Path
from typing import Any, Callable

from .. import platform as osplatform
from .. import product
from ..core import backup, capabilities, config, events, i18n, inventory, migrate, onboarding, pairing, paths, settings, state, support, update
from ..core.proc import spawn

SYNC_WORDS = {
    "IN_SYNC": "SYNCED",
    "SYNCING": "SYNCING",
    "PEER_OFFLINE": "OFFLINE",
    "CONFLICTS": "CONFLICT",
    "PAUSED": "PAUSED",
    "ERROR": "ERROR",
    "STOPPED": "ERROR",
    "NO_PEER": "NOT_CONFIGURED",
    "NOT_CONFIGURED": "NOT_CONFIGURED",
    "NOT_INSTALLED": "NOT_CONFIGURED",
    "DISABLED": "NOT_CONFIGURED",
}


class Problem(Exception):
    """A failure the user can understand. `code` selects the translated what/why/fix texts
    (`problem.<code>.what` …); `params` fill their placeholders; `actions` name the buttons."""

    def __init__(self, code: str, actions: list[str] | None = None, detail: str = "", **params: Any):
        super().__init__(code)
        self.code, self.actions, self.detail, self.params = code, actions or ["retry"], detail, params

    def as_dict(self) -> dict:
        return {"code": self.code, "actions": self.actions, "detail": events.redact(self.detail)[:600], "params": self.params}


def _said(exc: BaseException) -> str:
    """What an error says, keeping a catalog sentence translatable (`str()` would flatten it)."""
    return exc.args[0] if exc.args and isinstance(exc.args[0], i18n.Msg) else str(exc)


def _quiet(func: Callable[[], Any], default: Any) -> Any:
    """One broken integration must never blank the whole screen."""
    try:
        return func()
    except Exception as exc:
        events.emit("app.error", f"{getattr(func, '__name__', 'call')}: {exc}", "warn")
        return default


class Backend:
    demo = False

    def __init__(self) -> None:
        self.downloads: dict[str, tuple[str, bytes, float]] = {}

    # ── dispatch ────────────────────────────────────────────────────────────
    def call(self, method: str, params: dict) -> Any:
        if method.startswith("_") or method in ("call", "take_download"):
            raise Problem("unknown_request", ["close"])
        handler = getattr(self, method, None)
        if not callable(handler):
            raise Problem("unknown_request", ["close"])
        try:
            return i18n.localize(handler(**params), self.language)
        except Problem as problem:
            problem.detail = i18n.render(problem.detail, self.language)
            raise

    @property
    def language(self) -> str:
        return i18n.resolve(str(self.cfg.get("general.language", "auto")))

    def _offer_download(self, name: str, blob: bytes) -> dict:
        now = time.time()
        self.downloads = {k: v for k, v in self.downloads.items() if now - v[2] < 600}
        token = pysecrets.token_urlsafe(18)
        self.downloads[token] = (name, blob, now)
        return {"download": token, "name": name, "bytes": len(blob)}

    def take_download(self, token: str) -> tuple[str, bytes] | None:
        item = self.downloads.pop(token, None)
        return (item[0], item[1]) if item else None

    @property
    def cfg(self) -> config.Config:
        return config.load()

    # ── start-up ────────────────────────────────────────────────────────────
    def bootstrap(self) -> dict:
        cfg = self.cfg
        lang = i18n.resolve(str(cfg.get("general.language", "auto")))
        os_ = osplatform.current()
        return {
            "product": product.as_dict(),
            "demo": self.demo,
            "language": lang,
            "languages": i18n.LANGUAGES,
            "catalog": i18n.merged(lang),
            "theme": str(cfg.get("general.theme", "auto")),
            "onboarding": onboarding.state(cfg),
            "platform": os_.info().as_dict(),
            "migration": _quiet(migrate.run, {"migrated": False}),
            "config_warnings": list(cfg.warnings),
        }

    def set_language(self, language: str) -> dict:
        settings.apply({"general.language": language})
        lang = i18n.resolve(language)
        return {"language": lang, "catalog": i18n.merged(lang)}

    # ── dashboard ───────────────────────────────────────────────────────────
    def dashboard(self) -> dict:
        from ..core import modes
        from ..integrations import peripherals, worksync

        cfg = self.cfg
        inv = inventory.load()
        runtime = state.load("runtime") or {}
        fresh = time.time() - float(runtime.get("ts", 0) or 0) < 600
        work = _quiet(lambda: worksync.status(cfg, deep=False), {"state": "ERROR", "enabled": True})
        base = worksync.root(cfg)
        sync_word = SYNC_WORDS.get(str(work.get("state", "")), "ERROR")
        sync_reason = str(work.get("state", "")).lower()
        if not base.is_dir():
            workspace_state = "MISSING"
        else:
            workspace_state = "READY"
        periph = _quiet(lambda: peripherals.status(cfg), {"state": "NOT_CONFIGURED"})
        periph_word = {"CONNECTED": "CONNECTED", "WAITING": "WAITING", "UNPAIRED": "NOT_CONFIGURED", "STOPPED": "ERROR"}.get(str(periph.get("state")), "NOT_CONFIGURED")
        servers = self._server_rows(cfg, inv, runtime if fresh else {})
        stations = self._workstation_rows(cfg, inv, work)
        attention = []
        if workspace_state == "MISSING":
            attention.append({"code": "workspace_missing", "page": "workspace"})
        if sync_word == "ERROR":
            attention.append({"code": "sync_error", "page": "sync"})
        if sync_word == "CONFLICT":
            attention.append({"code": "sync_conflict", "page": "sync"})
        if sync_word == "PAUSED":
            attention.append({"code": "sync_paused", "page": "sync"})
        if periph_word == "ERROR":
            attention.append({"code": "peripherals_stopped", "page": "peripherals"})
        for row in servers:
            if row["status"] in ("OFFLINE", "AUTH_REQUIRED", "UNTRUSTED", "ERROR"):
                attention.append({"code": f"server_{row['status'].lower()}", "page": "servers", "name": row["role"]})
        for warning in cfg.warnings:
            attention.append({"code": "config_broken", "page": "recovery", "detail": events.redact(warning)[:200]})
        offer = (update.last() or {}).get("offer")
        return {
            "ts": time.time(),
            "ready": not attention,
            "mode": "workstation" if _quiet(modes.is_work, False) else "default",
            "kind": str(cfg.get("onboarding.kind", "personal")),
            "device": cfg.device,
            "workspace": {"state": workspace_state, "path": str(base)},
            "sync": {"state": sync_word, "reason": sync_reason, "need_items": int(work.get("need_items", 0) or 0), "last_sync": work.get("last_sync", 0), "peers": len(work.get("peers", []) or [])},
            "workstations": stations,
            "peripherals": {"state": periph_word, "role": periph.get("role", ""), "links": periph.get("links", 0)},
            "servers": servers,
            "updates": {"current": product.VERSION, "channel": product.channel(), "available": offer.get("version") if offer else ""},
            "attention": attention,
            "background": bool(fresh),
        }

    def _workstation_rows(self, cfg, inv: dict, work: dict) -> list[dict]:
        peers = {p.get("name"): p for p in (work.get("peers") or [])}
        rows = []
        for name, device in sorted(inv["devices"].items()):
            if device.get("role") != "workstation":
                continue
            me = name == cfg.device
            peer = peers.get(name, {})
            rows.append(
                {
                    "name": name,
                    "self": me,
                    "platform": device.get("platform", "") or (paths.platform() if me else ""),
                    "version": product.VERSION if me else device.get("version", ""),
                    "online": True if me else bool(peer.get("connected")),
                    "paired_sync": me or bool(device.get("syncthing_id")),
                    "paired_input": me or bool(device.get("deskflow_fp")),
                    "last_seen": 0 if me else peer.get("last_seen", ""),
                    "completion": peer.get("completion"),
                }
            )
        if not any(row["self"] for row in rows):
            rows.insert(0, {"name": cfg.device, "self": True, "platform": paths.platform(), "version": product.VERSION, "online": True, "paired_sync": True, "paired_input": True, "last_seen": 0, "completion": None})
        return rows

    def _server_rows(self, cfg, inv: dict, runtime: dict) -> list[dict]:
        from ..core import health

        home = inv["devices"].get("home", {})
        home_ok = bool(inventory.device_host(home))
        label, node = inventory.cloud_current(inv)
        rows = [
            {"role": "home", "configured": home_ok, "status": health.server_status(home_ok, runtime.get("home")), "host": inventory.device_host(home), "user": home.get("ssh_user", ""), "port": int(home.get("ssh_port", 22) or 22), "report": _slim(runtime.get("home"))},
            {
                "role": "cloud",
                "configured": node is not None,
                "status": health.server_status(node is not None, runtime.get("cloud"), direct=bool(node) and not node.get("tailscale_name")),
                "host": (node or {}).get("tailscale_name") or (node or {}).get("endpoint", ""),
                "user": (node or {}).get("user", ""),
                "port": int((node or {}).get("port", 22) or 22),
                "label": label or "",
                "hardware": (node or {}).get("hardware", {}),
                "report": _slim(runtime.get("cloud")),
            },
        ]
        return rows

    def set_mode(self, mode: str, save: bool = False, close: bool = False) -> dict:
        from ..core import modes

        cfg = self.cfg
        with modes.switching() as mine:
            if not mine:
                raise Problem("mode_busy", ["close"])
            steps = modes.on(cfg) if mode == "workstation" else modes.off(cfg, save=save, close=close)
        return {"mode": "workstation" if modes.is_work() else "default", "steps": steps}

    # ── capabilities / health ───────────────────────────────────────────────
    def capabilities(self) -> dict:
        data = capabilities.matrix(self.cfg)
        data["permissions"] = [p.as_dict() for p in osplatform.current().permissions()]
        return data

    def open_permission(self, permission: str) -> dict:
        target = next((p for p in osplatform.current().permissions() if p.id == permission), None)
        if not target or not target.settings_url:
            raise Problem("no_settings_link", ["close"])
        return {"opened": osplatform.current().open_url(target.settings_url)}

    def selftest(self) -> dict:
        """The first-run / on-demand system check: one row per thing the user relies on."""
        from ..core import doctor
        from ..integrations import worksync

        cfg = self.cfg
        lang = self.language
        rows: list[dict] = []

        def row(ident: str, status: str, detail: str = "", fix: str = "") -> None:
            # rendered here, before redaction and truncation turn the sentence into plain text
            rows.append({"id": ident, "status": status, "detail": events.redact(i18n.render(detail, lang))[:300], "fix": fix})

        row("application", "pass", f"{product.NAME} {product.VERSION} ({product.channel()})")
        base = worksync.root(cfg)
        if not base.is_dir():
            row("workspace", "fail", str(base), "create_workspace")
        elif not os.access(base, os.W_OK | os.R_OK):
            row("workspace", "fail", str(base), "")
        else:
            probe = base / f".uw-write-test-{pysecrets.token_hex(4)}"
            try:
                probe.write_text("ok")
                probe.unlink()
                row("workspace", "pass", str(base))
            except OSError as exc:
                row("workspace", "fail", str(exc))
        features = {f["id"]: f for f in capabilities.matrix(cfg)["features"]}
        wanted = str(cfg.get("onboarding.kind", "personal")) == "workstation"
        level = {"SUPPORTED": "pass", "PARTIALLY_SUPPORTED": "warn", "REQUIRES_PERMISSION": "warn", "NOT_INSTALLED": "skip", "UNAVAILABLE": "skip"}
        for ident, feature, enabled in (
            ("sync", "file_sync", wanted and bool(cfg.get("work.enabled", True))),
            ("peripherals", "peripherals", wanted and bool(cfg.get("peripherals.enabled", False))),
            ("clipboard", "shared_clipboard", wanted and bool(cfg.get("peripherals.enabled", False))),
            ("terminal", "terminal_clipboard", wanted),
            # Optional tools: their absence is information, never a failed check.
            ("git", "git", False),
            ("ssh", "servers", False),
            ("network", "private_network", False),
            ("credentials", "credentials", False),
            ("assistant", "assistant", False),
        ):
            item = features[feature]
            status = level[item["status"]]
            if status == "skip" and enabled:
                status = "fail"   # the user asked for this and it can not work yet
            row(ident, status, item["reason"], "install" if item["status"] == "NOT_INSTALLED" else "")
        if wanted and bool(cfg.get("work.enabled", True)) and features["file_sync"]["status"] == "SUPPORTED":
            snap = _quiet(lambda: worksync.status(cfg, deep=False), {"state": "ERROR"})
            word = SYNC_WORDS.get(str(snap.get("state")), "ERROR")
            row("sync_engine", "pass" if word in ("SYNCED", "SYNCING", "OFFLINE", "NOT_CONFIGURED") else "fail", i18n.msg(f"state.{word}"), "repair_sync" if word == "ERROR" else "")
        for server in self._server_rows(cfg, inventory.load(), {}):
            if not server["configured"]:
                row(server["role"], "skip", i18n.msg("selftest.optional"))
        last = update.last()
        row("updates", "pass" if not last.get("error") else "warn", last.get("error") or i18n.msg("selftest.channel", channel=i18n.msg(f"choice.{cfg.get('updates.channel', 'stable')}")))
        checks = _quiet(lambda: [c.as_dict() for c in doctor.run_all(cfg)], []) if wanted else []
        verdict = "fail" if any(r["status"] == "fail" for r in rows) else "warn" if any(r["status"] == "warn" for r in rows) else "pass"
        return {"verdict": verdict, "rows": rows, "details": checks}

    def health(self) -> dict:
        from ..core import doctor

        cfg = self.cfg
        lang = self.language
        checks = _quiet(lambda: [c.as_dict() for c in doctor.run_all(cfg)], [])
        for check in checks:
            check["detail"] = events.redact(str(i18n.render(check.get("detail", ""), lang)))
        return {"checks": checks, "verdict": doctor.verdict([doctor.Check(c["level"], c["title"]) for c in checks]) if checks else "UNKNOWN", "state": _quiet(state.health, {})}

    def repair(self, what: str) -> dict:
        """Backup → repair → verify. Never deletes user data."""
        from ..core import bootstrap, supervise
        from ..integrations import peripherals, worksync

        cfg = self.cfg
        backup.snapshot(f"before-repair-{what}")
        done: list[str] = []
        if what == "create_workspace":
            worksync.root(cfg).mkdir(parents=True, exist_ok=True)
            done.append(i18n.msg("repair.done.workspace"))
        elif what == "sync":
            if not worksync.binary():
                raise Problem("component_missing", ["open_capabilities"], component="Syncthing")
            _title, note = bootstrap.step_work(cfg)
            done.append(note)
            if note.startswith("FAILED"):
                raise Problem("repair_failed", ["details", "retry"], detail=note)
        elif what == "peripherals":
            if not peripherals.core():
                raise Problem("component_missing", ["open_capabilities"], component="Deskflow")
            _title, note = bootstrap.step_peripherals(cfg)
            peripherals.service("restart")
            done.append(note)
        elif what == "service":
            _title, note = bootstrap.step_service(cfg)
            done.append(note)
        elif what == "integration":
            for name in ("step_ssh", "step_desktop"):
                done.append(getattr(bootstrap, name)(cfg)[1])
        elif what == "config":
            done += backup.rebuild()
        elif what == "state":
            done += state.recover() or [i18n.msg("repair.done.state")]
        elif what == "background":
            done += [i18n.msg("repair.done.started", name=name) for name in supervise.restore()] or [i18n.msg("repair.done.running")]
        else:
            raise Problem("unknown_request", ["close"])
        events.emit("repair", f"repair '{what}': " + "; ".join(done))
        return {"done": done, "selftest": self.selftest()}

    # ── settings ────────────────────────────────────────────────────────────
    def settings_get(self) -> dict:
        data = settings.describe(self.cfg)
        os_ = osplatform.current()
        data["autostart"] = {"enabled": os_.autostart().enabled, "mechanism": os_.autostart().mechanism}
        data["editors"], data["terminals"] = capabilities.editors(), capabilities.terminals()
        data["credential_store"] = os_.credential_backend()
        data["devices"] = sorted(n for n, d in inventory.load()["devices"].items() if d.get("role") == "workstation") or [self.cfg.device]
        data["locations"] = {"config": str(paths.config_dir()), "state": str(paths.state_dir()), "cache": str(paths.cache_dir())}
        return data

    def settings_check(self, changes: dict) -> dict:
        _clean, errors = settings.check(changes)
        return {"errors": errors}

    def settings_apply(self, changes: dict) -> dict:
        try:
            result = settings.apply(changes)
        except settings.SettingsError as exc:
            return {"errors": exc.errors, "changed": []}
        self._after_settings(result["changed"])
        return {**result, "errors": {}}

    def settings_reset(self, keys: list[str]) -> dict:
        result = settings.reset(keys)
        self._after_settings(result["changed"])
        return result

    def _after_settings(self, changed: list[str]) -> None:
        cfg = self.cfg
        if "general.autostart" in changed:
            osplatform.current().set_autostart([str(paths.launcher()), "start"], bool(cfg.get("general.autostart", False)))
        if any(key.startswith("work.") for key in changed):
            from ..integrations import worksync

            if worksync.enabled(cfg) and worksync.instance(cfg).configured():
                _quiet(lambda: worksync.refresh(cfg), None)
        if any(key.startswith("peripherals.") for key in changed):
            from ..integrations import peripherals

            if peripherals.enabled(cfg) and peripherals.core():
                _quiet(lambda: (peripherals.prepare(cfg), peripherals.service("restart")), None)

    def browse(self, path: str = "") -> dict:
        """Folder picker: the folders inside `path` (the home folder when empty). Read-only."""
        base = paths.expand(path) if path else paths.home()
        if not base.is_dir():
            base = paths.home()
        base = base.resolve()
        folders = []
        try:
            for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                folders.append(entry.name)
        except OSError:
            pass
        return {"path": str(base), "parent": str(base.parent) if base.parent != base else "", "folders": folders[:500], "home": str(paths.home()), "problem": settings.workspace_problem(str(base))}

    def make_folder(self, parent: str, name: str) -> dict:
        name = name.strip()
        if not name or any(ch in name for ch in '/\\:*?"<>|') or name in (".", ".."):
            raise Problem("bad_folder_name", ["close"])
        target = paths.expand(parent) / name
        try:
            target.mkdir(parents=False, exist_ok=True)
        except OSError as exc:
            raise Problem("folder_not_created", ["close"], detail=str(exc)) from exc
        return self.browse(str(target))

    def open_path(self, what: str = "workspace") -> dict:
        from ..integrations import worksync

        target = {"workspace": worksync.root(self.cfg), "logs": paths.state_dir(), "config": paths.config_dir()}.get(what)
        if target is None or not target.exists():
            raise Problem("folder_missing", ["close"])
        return {"opened": osplatform.current().open_path(target)}

    # ── first run ───────────────────────────────────────────────────────────
    def onboarding_plan(self, choices: dict) -> dict:
        cfg = self.cfg
        workspace = str(choices.get("workspace") or onboarding.suggested_workspace(cfg))
        return {"steps": [s.as_dict() for s in onboarding.plan(choices, cfg)], "workspace": workspace, "workspace_problem": settings.workspace_problem(workspace), "existing": _workspace_summary(workspace)}

    def onboarding_apply(self, choices: dict) -> dict:
        return onboarding.apply(choices)

    def onboarding_reset(self) -> dict:
        onboarding.reset()
        return {"ok": True}

    # ── workstations and pairing ────────────────────────────────────────────
    def workstations(self) -> dict:
        from ..integrations import worksync

        cfg = self.cfg
        work = _quiet(lambda: worksync.status(cfg, deep=False), {})
        return {"rows": self._workstation_rows(cfg, inventory.load(), work), "device": cfg.device, "capabilities": [f.as_dict() for f in capabilities.features(cfg)]}

    def pair_offer(self) -> dict:
        cfg = self.cfg
        offer = pairing.make_offer(cfg)
        if not offer.syncthing_id and not offer.deskflow_fp:
            raise Problem("pairing_not_ready", ["open_setup"])
        return {"code": pairing.encode(offer), "offer": offer.as_dict()}

    def pair_review(self, code: str) -> dict:
        try:
            theirs = pairing.decode(code)
        except pairing.PairingError as exc:
            raise Problem("pairing_code_invalid", ["close"], detail=_said(exc)) from exc
        return pairing.review(self.cfg, theirs)

    def pair_accept(self, code: str, confirmation: str, sync: bool = True, share_input: bool = True, merge_confirmed: bool = False) -> dict:
        cfg = self.cfg
        try:
            theirs = pairing.decode(code)
        except pairing.PairingError as exc:
            raise Problem("pairing_code_invalid", ["close"], detail=_said(exc)) from exc
        review = pairing.review(cfg, theirs)
        if review["problems"]:
            raise Problem("pairing_blocked", ["close"], detail=review["problems"][0])
        if not pysecrets.compare_digest(str(confirmation).strip(), review["confirmation"]):
            raise Problem("pairing_number_mismatch", ["close"])
        if sync and review["both_have_files"] and not merge_confirmed:
            raise Problem("pairing_merge_unconfirmed", ["close"])
        return pairing.accept(cfg, theirs, sync=sync, share_input=share_input)

    def unpair(self, name: str) -> dict:
        if not pairing.unpair(self.cfg, name):
            raise Problem("device_unknown", ["close"])
        return {"ok": True}

    def rename_device(self, old: str, new: str) -> dict:
        try:
            pairing.rename(self.cfg, old, new.strip().lower())
        except pairing.PairingError as exc:
            raise Problem("device_name_invalid", ["close"], detail=_said(exc)) from exc
        return {"ok": True}

    # ── workspace and sync ──────────────────────────────────────────────────
    def workspace(self) -> dict:
        from ..integrations import worksync

        cfg = self.cfg
        base = worksync.root(cfg)
        found = _quiet(lambda: worksync.scan(cfg, budget=8.0).as_dict(), {}) if base.is_dir() else {}
        threshold = float(cfg.get("work.large_file_mb", 2048) or 0)
        return {
            "path": str(base),
            "exists": base.is_dir(),
            "files": found.get("files", 0),
            "bytes": found.get("bytes", 0),
            "partial": found.get("partial", False),
            "large": found.get("large", [])[:100],
            "large_threshold_mb": threshold,
            "large_policy": cfg.get("work.large_file_policy", "hold"),
            "ignored": found.get("ignored", [])[:100] if isinstance(found.get("ignored"), list) else found.get("ignored", 0),
            "repos": found.get("repos", [])[:100],
            "rules": _quiet(lambda: sorted(worksync.ignore_rules(cfg)), []),
            "boundary": {"shared": "workspace", "local": [i18n.msg(f"boundary.local.{item}") for item in ("credentials", "host_keys", "network", "sessions", "identity")]},
        }

    def sync_status(self) -> dict:
        from ..integrations import worksync

        cfg = self.cfg
        snap = _quiet(lambda: worksync.status(cfg, deep=True), {"state": "ERROR", "errors": [{"path": "", "error": i18n.msg("sync.status_unavailable")}]})
        return {
            "state": SYNC_WORDS.get(str(snap.get("state")), "ERROR"),
            "reason": str(snap.get("state", "")).lower(),
            "installed": bool(snap.get("installed")),
            "enabled": bool(snap.get("enabled")),
            "running": bool(snap.get("running")),
            "paused": bool(snap.get("paused")),
            "path": snap.get("path", ""),
            "peers": snap.get("peers", []),
            "need_items": snap.get("need_items", 0),
            "need_bytes": snap.get("need_bytes", 0),
            "outgoing_items": snap.get("outgoing_items", 0),
            "last_sync": snap.get("last_sync", 0),
            "errors": [{"path": e.get("path", ""), "error": events.redact(str(e.get("error", "")))} for e in snap.get("errors", [])][:50],
            "conflicts": snap.get("conflicts", [])[:200],
            "large": snap.get("large", [])[:100],
            "files": snap.get("files", 0),
            "bytes": snap.get("bytes", 0),
            "versions": len(_quiet(lambda: worksync.versions(cfg), [])),
        }

    def sync_action(self, action: str) -> dict:
        from ..integrations import worksync

        cfg = self.cfg
        inst = worksync.instance(cfg)
        if not worksync.binary():
            raise Problem("component_missing", ["open_capabilities"], component="Syncthing")
        if not inst.configured():
            raise Problem("sync_not_set_up", ["repair"])
        if action in ("scan", "sync_now"):
            if not inst.running():
                worksync.service("start")
                inst.wait_running(20)
            ok = inst.rescan()
        elif action == "pause":
            ok = inst.set_paused(True)
        elif action == "resume":
            ok = inst.set_paused(False)
        elif action == "restart":
            ok = worksync.service("restart") and inst.wait_running(30)
        else:
            raise Problem("unknown_request", ["close"])
        if not ok:
            raise Problem("sync_not_running", ["repair", "retry"])
        events.emit("sync.action", f"sync: {action}")
        return self.sync_status()

    def sync_resolve(self, conflict: str, keep: str) -> dict:
        from ..integrations import worksync

        try:
            moved = worksync.resolve_conflict(self.cfg, conflict, keep)
        except (ValueError, OSError) as exc:
            raise Problem("conflict_not_resolved", ["close"], detail=str(exc)) from exc
        return {"kept": keep, "other_version_moved_to": moved}

    def sync_versions(self, prefix: str = "") -> dict:
        from ..integrations import worksync

        return {"versions": _quiet(lambda: worksync.versions(self.cfg, prefix), [])[:500]}

    def sync_restore(self, path: str) -> dict:
        from ..integrations import worksync

        try:
            return {"restored_as": worksync.restore(self.cfg, path)}
        except (ValueError, OSError) as exc:
            raise Problem("version_not_restored", ["close"], detail=str(exc)) from exc

    # ── keyboard, mouse, clipboard ──────────────────────────────────────────
    def peripherals_status(self) -> dict:
        from ..integrations import peripherals

        cfg = self.cfg
        snap = _quiet(lambda: peripherals.status(cfg), {"state": "NOT_CONFIGURED"})
        inv = inventory.load()
        features = {f.id: f.as_dict() for f in capabilities.features(cfg)}
        return {
            **{k: v for k, v in snap.items() if k != "fingerprint"},
            "fingerprint": peripherals.pretty(snap["fingerprint"]) if snap.get("fingerprint") else "",
            "screens": peripherals.workstations(inv),
            "server_device": str(cfg.get("peripherals.server", "")),
            "peer_side": str(cfg.get("peripherals.peer_side", "right")),
            "clipboard": bool(cfg.get("peripherals.clipboard", True)),
            "exposed": _quiet(lambda: peripherals.exposed(cfg), False),
            "feature": features["peripherals"],
            "clipboard_feature": features["shared_clipboard"],
            "permissions": [p.as_dict() for p in osplatform.current().permissions() if p.id in ("accessibility", "input-monitoring", "input-capture", "elevated-windows")],
        }

    def peripherals_action(self, action: str) -> dict:
        from ..core import bootstrap
        from ..integrations import peripherals

        cfg = self.cfg
        if not peripherals.core():
            raise Problem("component_missing", ["open_capabilities"], component="Deskflow")
        if action == "setup":
            if not cfg.get("peripherals.server"):
                settings.apply({"peripherals.server": cfg.device})
            settings.apply({"peripherals.enabled": True})
            _title, note = bootstrap.step_peripherals(self.cfg)
            if note.startswith("FAILED"):
                raise Problem("repair_failed", ["details", "retry"], detail=note)
        elif action in ("start", "stop", "restart"):
            if not peripherals.service(action):
                raise Problem("peripherals_not_started", ["repair", "retry"])
        else:
            raise Problem("unknown_request", ["close"])
        return self.peripherals_status()

    # ── servers ─────────────────────────────────────────────────────────────
    def servers(self) -> dict:
        cfg = self.cfg
        inv = inventory.load()
        runtime = state.load("runtime") or {}
        history = [{"label": label, "state": node.get("state", ""), "provider": node.get("provider", ""), "enrolled_at": node.get("enrolled_at", ""), "retired_at": node.get("retired_at", "")} for label, node in sorted(inv["cloud"]["nodes"].items(), reverse=True)]
        return {"rows": self._server_rows(cfg, inv, runtime), "cloud_history": history[:20], "ssh": capabilities.by_id(capabilities.components(False))["ssh"].installed, "keys": _ssh_keys(), "user": os.environ.get("USER") or os.environ.get("USERNAME") or ""}

    def server_scan(self, host: str, port: int = 22) -> dict:
        """Read the server's identity so the user can compare it — the step before any trust."""
        from ..cloud import CloudError, scan_host_key

        host = host.strip()
        if not inventory.valid_host(host):
            raise Problem("host_invalid", ["close"])
        try:
            key, fingerprint = scan_host_key(host, int(port))
        except CloudError as exc:
            raise Problem("server_unreachable", ["retry", "close"], detail=str(exc)) from exc
        known = paths.ssh_dir() / "known_hosts"
        lines = known.read_text(errors="replace").splitlines() if known.exists() else []
        blob = key.split()[1]
        already = any(blob in line for line in lines)
        changed = any(line.split() and line.split()[0] in (host, f"[{host}]:{port}") and blob not in line and key.split()[0] in line for line in lines if line.strip() and not line.startswith(("#", "|")))
        return {"host": host, "port": int(port), "fingerprint": fingerprint, "key_type": key.split()[0], "already_trusted": already, "identity_changed": changed}

    def _trust(self, host: str, port: int, fingerprint: str) -> str:
        from ..cloud import CloudError, fingerprints_match, scan_host_key
        from ..core import journal

        try:
            key, actual = scan_host_key(host, port)
        except CloudError as exc:
            raise Problem("server_unreachable", ["retry", "close"], detail=str(exc)) from exc
        if not fingerprints_match(fingerprint, actual):
            raise Problem("fingerprint_changed", ["review_fingerprint", "close"])
        known = paths.ssh_dir() / "known_hosts"
        paths.ensure(paths.ssh_dir())
        lines = known.read_text(errors="replace").splitlines() if known.exists() else []
        name = host if port == 22 else f"[{host}]:{port}"
        if not any(line.split()[:1] == [name] and key.split()[1] in line for line in lines if line.strip()):
            if known.exists():
                journal.backup_file(known)
            with known.open("a") as handle:
                handle.write(f"{name} {key}\n")
            try:
                known.chmod(0o600)
            except OSError:
                pass
            events.emit("ssh.trust", f"server identity trusted for {name}", fingerprint=actual)
        return key

    def home_save(self, host: str, user: str, port: int = 22, identity: str = "", fingerprint: str = "") -> dict:
        from ..integrations import ssh as sshcfg

        host, user = host.strip(), user.strip()
        if not inventory.valid_host(host):
            raise Problem("host_invalid", ["close"])
        if not inventory.valid_user(user):
            raise Problem("user_invalid", ["close"])
        if identity and not paths.expand(identity).is_file():
            raise Problem("key_missing", ["close"])
        if not fingerprint:
            raise Problem("fingerprint_unconfirmed", ["review_fingerprint"])
        self._trust(host, int(port), fingerprint)
        cfg = self.cfg
        backup.snapshot("before-home-change")
        inv = inventory.load()
        device = inventory.ensure_device(inv, "home", role="home", ssh_user=user, ssh_identity=identity)
        device["host"], device["ssh_port"] = host, str(int(port))
        device.pop("tailscale_name", None)
        if host.endswith(".ts.net"):
            device["tailscale_name"] = host
        inventory.save(inv)
        sshcfg.apply(inv, cfg.device)
        events.emit("home.changed", "Home server endpoint saved")
        return self.server_test("home")

    def home_remove(self) -> dict:
        from ..integrations import ssh as sshcfg

        backup.snapshot("before-home-remove")
        inv = inventory.load()
        if inv["devices"].pop("home", None) is None:
            raise Problem("server_not_configured", ["close"])
        inventory.save(inv)
        sshcfg.apply(inv, self.cfg.device)
        events.emit("home.removed", "Home server removed (its data was left untouched)")
        return {"ok": True}

    def server_test(self, role: str) -> dict:
        from ..core import health

        cfg = self.cfg
        if role not in ("home", "cloud"):
            raise Problem("unknown_request", ["close"])
        report = health.probe_remote(role, list(cfg.get("home.services", [])) if role == "home" else None)
        runtime = state.load("runtime") or {}
        runtime[role] = report
        runtime.setdefault("ts", time.time())
        state.save("runtime", runtime)
        row = next(r for r in self._server_rows(cfg, inventory.load(), runtime) if r["role"] == role)
        if row["status"] in ("OFFLINE", "AUTH_REQUIRED", "UNTRUSTED", "ERROR") or health.identity_changed(report):
            code = "fingerprint_changed" if health.identity_changed(report) else f"server_{row['status'].lower()}"
            raise Problem(code, ["review_fingerprint", "retry", "close"] if code == "fingerprint_changed" else ["retry", "close"], detail=str(report.get("error", "")), role=role)
        return row

    def server_connect(self, role: str) -> dict:
        """Open a terminal connected to the server. Uses the standard `ssh <role>` name."""
        from ..integrations import apps

        cfg = self.cfg
        terminal = apps.terminal(cfg)
        if not terminal:
            raise Problem("component_missing", ["open_capabilities"], component="terminal")
        command = {"wezterm": ["wezterm", "start", "--", "ssh", role], "gnome-terminal": ["gnome-terminal", "--", "ssh", role], "konsole": ["konsole", "-e", "ssh", role], "kitty": ["kitty", "ssh", role], "alacritty": ["alacritty", "-e", "ssh", role], "Terminal.app": ["osascript", "-e", f'tell application "Terminal" to do script "ssh {role}"'], "wt": ["wt", "ssh", role], "powershell": ["powershell", "-NoExit", "-Command", f"ssh {role}"]}.get(Path(terminal).stem if terminal != "Terminal.app" else terminal, [terminal, "-e", "ssh", role])
        if not spawn(command):
            raise Problem("terminal_not_opened", ["close"])
        return {"opened": True}

    def cloud_enroll(self, host: str, user: str, port: int = 22, fingerprint: str = "", provider: str = "custom") -> dict:
        """Transactional: reachability → fingerprint → authentication → health check → activate.
        Any failure leaves the previous Cloud server active."""
        from .. import cloud

        host, user = host.strip(), user.strip()
        if not inventory.valid_host(host):
            raise Problem("host_invalid", ["close"])
        if not inventory.valid_user(user):
            raise Problem("user_invalid", ["close"])
        if not fingerprint:
            raise Problem("fingerprint_unconfirmed", ["review_fingerprint"])
        cfg = self.cfg
        lang = self.language
        prov = cloud.provider("custom")
        stages: list[dict] = []
        try:
            prov.validate_endpoint(host, user, int(port))
            key, actual = cloud.scan_host_key(host, int(port))
            stages.append({"id": "reachability", "ok": True})
            if not cloud.fingerprints_match(fingerprint, actual):
                raise Problem("fingerprint_changed", ["review_fingerprint", "close"])
            stages.append({"id": "fingerprint", "ok": True})
            inv = inventory.load()
            previous = inventory.cloud_current(inv)[0]
            label = inventory.new_cloud_label(inv)
            target = cloud.Target(host, user, int(port), label, key)
            node = {"provider": provider[:40], "role": "compute", "endpoint": host, "port": int(port), "user": user, "host_key": key, "fingerprint": actual}
            backup.snapshot("before-cloud-change")
            try:
                result = cloud.enroll(cfg, prov, target, node, say=lambda message: stages.append({"id": "step", "ok": not message.startswith("!"), "note": events.redact(i18n.render(message, lang).lstrip("! "))}))
            finally:
                target.close()
        except cloud.CloudError as exc:
            text = str(exc)
            code = "cloud_auth_failed" if "authentication failed" in text else "cloud_not_activated"
            raise Problem(code, ["retry", "close"], detail=text, kept=inventory.cloud_current(inventory.load())[0] or "") from exc
        return {"label": result["label"], "retired": result["retired"] or "", "previous": previous or "", "stages": stages, "hardware": result["node"].get("hardware", {})}

    def cloud_remove(self) -> dict:
        from .. import cloud

        backup.snapshot("before-cloud-remove")
        released = cloud.release(self.cfg)
        if not released:
            raise Problem("server_not_configured", ["close"])
        return {"released": released}

    def cloud_projects(self) -> dict:
        from ..cloud import project

        cfg = self.cfg
        return {"candidates": _quiet(lambda: project.candidates(cfg), []), "sent": _quiet(lambda: project.status(cfg), []), "confirm_mb": cfg.get("cloud.project_confirm_mb", 500), "configured": inventory.cloud_current(inventory.load())[1] is not None}

    def cloud_plan(self, name: str) -> dict:
        """Review before transfer: project, files, size, target, exclusions, credential impact."""
        from ..cloud import project

        cfg = self.cfg
        try:
            wanted = project.plan(cfg, name)
            data = project.as_dict(wanted)
            data["needs_confirmation"] = project.needs_confirmation(cfg, wanted, project.load(name))
            data["summary"] = project.summary(wanted, project.current_label())
        except project.ProjectError as exc:
            raise Problem("project_not_planned", ["close"], detail=str(exc)) from exc
        return data

    def cloud_push(self, name: str, confirmed: bool = False) -> dict:
        from ..cloud import project

        cfg = self.cfg
        try:
            wanted = project.plan(cfg, name)
            if project.needs_confirmation(cfg, wanted, project.load(name)) and not confirmed:
                raise Problem("transfer_unconfirmed", ["close"])
            return project.push(cfg, name)
        except project.ProjectError as exc:
            raise Problem("transfer_failed", ["retry", "close"], detail=str(exc)) from exc

    def cloud_pull(self, name: str) -> dict:
        from ..cloud import project

        try:
            return project.pull(self.cfg, name)
        except project.ProjectError as exc:
            raise Problem("transfer_failed", ["retry", "close"], detail=str(exc)) from exc

    # ── assistants and Git ──────────────────────────────────────────────────
    def assistants(self) -> dict:
        from ..integrations import assistants

        return {"assistants": _quiet(lambda: assistants.reports(self.cfg), [])}

    def assistant_share_memory(self, project: str, dry: bool = True) -> dict:
        from ..integrations import claudestate

        cfg = self.cfg
        try:
            return claudestate.share(claudestate.locate(cfg, project), dry=dry)
        except claudestate.ClaudeStateError as exc:
            raise Problem("assistant_state_not_changed", ["close"], detail=str(exc)) from exc

    def git_projects(self) -> dict:
        """Read-only: repositories and their state. Nothing here ever resets, cleans or force-pushes."""
        from ..core import gitsync
        from ..integrations import worksync

        cfg = self.cfg
        base = worksync.root(cfg)
        found = _quiet(lambda: worksync.scan(cfg, budget=6.0).repos, []) if base.is_dir() else []
        rows = []
        for rel in found[:80]:
            row = _quiet(lambda rel=rel: gitsync.summarize(base / rel), None)
            if row:
                rows.append({"name": rel, "branch": row.get("branch", ""), "status": row.get("status", ""), "dirty": row.get("dirty", 0), "ahead": row.get("ahead", 0), "behind": row.get("behind", 0), "upstream": bool(row.get("upstream"))})
        return {"installed": capabilities.by_id(capabilities.components(False))["git"].installed, "rows": rows, "autosync": bool(cfg.get("autosync.enabled", False))}

    # ── recovery ────────────────────────────────────────────────────────────
    def recovery(self) -> dict:
        from ..core import journal, supervise
        from ..integrations import worksync

        cfg = self.cfg
        return {
            "snapshots": backup.snapshots()[:40],
            "versions": len(_quiet(lambda: worksync.versions(cfg), [])),
            "trash_days": cfg.get("work.trash_days", 30),
            "conflicts": len(_quiet(lambda: worksync.scan(cfg, budget=4.0).conflicts, []) if worksync.root(cfg).is_dir() else []),
            "installers": update.previous_installers(),
            "changes": _quiet(lambda: journal.rollback(dry_run=True), []),
            "supervised": sorted(state.load("supervised", {}) or {}) if supervise.needed() else [],
            "config_warnings": list(cfg.warnings),
            "export": backup.export_preview(),
        }

    def snapshot_create(self) -> dict:
        saved = backup.snapshot("manual")
        return {"id": saved.stem if saved else ""}

    def snapshot_restore(self, snapshot: str) -> dict:
        try:
            return {"restored": backup.restore(snapshot)}
        except backup.BackupError as exc:
            raise Problem("snapshot_not_restored", ["close"], detail=str(exc)) from exc

    def config_reset(self) -> dict:
        saved = backup.reset()
        return {"snapshot": saved.stem if saved else ""}

    def config_export(self, include_machine: bool = False, include_devices: bool = False) -> dict:
        return self._offer_download(f"{product.SLUG}-settings-{time.strftime('%Y%m%d')}.zip", backup.export(include_machine, include_devices))

    def config_import_preview(self, data: str) -> dict:
        try:
            return backup.import_preview(base64.b64decode(data))
        except (backup.BackupError, ValueError) as exc:
            raise Problem("import_rejected", ["close"], detail=str(exc)) from exc

    def config_import(self, data: str, include_machine: bool = False) -> dict:
        try:
            return backup.import_(base64.b64decode(data), include_machine)
        except (backup.BackupError, ValueError) as exc:
            raise Problem("import_rejected", ["close"], detail=str(exc)) from exc

    def support_bundle(self) -> dict:
        return self._offer_download(support.filename(), support.build())

    def uninstall_preview(self) -> dict:
        from ..core import journal
        from ..integrations import worksync

        return {
            "removed": _quiet(lambda: journal.rollback(dry_run=True), []),
            "kept": [str(worksync.root(self.cfg)), *(i18n.msg(f"uninstall.kept.{item}") for item in ("projects", "servers", "tools", "passwords"))],
            "optional": [str(paths.config_dir()), str(paths.state_dir())],
        }

    # ── updates and history ─────────────────────────────────────────────────
    def update_status(self) -> dict:
        return {**(update.last() or {"current": product.VERSION, "offer": None, "error": "", "checked": 0}), "current": product.VERSION, "channel_running": product.channel(), "install_kind": update.install_kind(), "installers": update.previous_installers()}

    def update_check(self) -> dict:
        update.check(self.cfg)
        return self.update_status()

    def update_download(self) -> dict:
        offer = (update.last() or {}).get("offer")
        if not offer:
            raise Problem("no_update", ["close"])
        try:
            path = update.download(update.Offer(**offer))
        except update.UpdateError as exc:
            raise Problem("update_rejected", ["retry", "close"], detail=str(exc)) from exc
        return {"file": str(path), "opened": osplatform.current().open_path(path.parent)}

    # ── help ────────────────────────────────────────────────────────────────
    LINKS = {
        "docs": product.DOCS,
        "getting_started": f"{product.HOMEPAGE}/blob/main/docs/getting-started.md",
        "troubleshooting": f"{product.HOMEPAGE}/blob/main/docs/troubleshooting.md",
        "platforms": f"{product.HOMEPAGE}/blob/main/docs/supported-platforms.md",
        "report": f"{product.ISSUES}/new/choose",
        "security": f"{product.HOMEPAGE}/security/advisories/new",
        "releases": product.RELEASES,
    }

    def help_info(self) -> dict:
        os_ = osplatform.current().info()
        return {"version": product.VERSION, "channel": product.channel(), "platform": os_.as_dict(), "links": dict(self.LINKS), "license": product.LICENSE}

    def open_link(self, which: str) -> dict:
        url = self.LINKS.get(which)
        if not url:
            raise Problem("unknown_request", ["close"])
        return {"opened": osplatform.current().open_url(url), "url": url}

    def history(self, search: str = "", level: str = "", limit: int = 200) -> dict:
        rows = events.read(max(1, min(int(limit), 1000)), grep=search or None, level=level or None)
        return {"rows": [{"timestamp": r.get("timestamp", ""), "event": r.get("event", ""), "level": r.get("level", "info"), "message": events.redact(str(r.get("message", "")))} for r in reversed(rows)]}


def _slim(report: dict | None) -> dict:
    if not report:
        return {}
    return {k: report.get(k) for k in ("online", "cpu_pct", "ram_used_pct", "disk_used_pct", "disk_free_gb", "os", "error", "checked") if k in report}


def _ssh_keys() -> list[str]:
    folder = paths.ssh_dir()
    try:
        return sorted(str(p) for p in folder.glob("id_*") if p.is_file() and not p.name.endswith(".pub"))[:20]
    except OSError:
        return []


def _workspace_summary(workspace: str) -> dict:
    target = paths.expand(workspace)
    if not target.is_dir():
        return {"exists": False, "entries": 0}
    try:
        entries = sum(1 for _ in itertools.islice(target.iterdir(), 2000))
    except OSError:
        entries = 0
    return {"exists": True, "entries": entries}
