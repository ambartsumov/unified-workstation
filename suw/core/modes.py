"""Mode state machine: DEFAULT <-> WORKSTATION.

A mode is a user-facing environment, not a system state: toggling it never stops the daemon,
networking, sync or any background service. Both transitions are idempotent and resumable —
an interrupted transition is completed (not repeated) on the next call.
"""

from __future__ import annotations

import contextlib
import time

from . import events, paths, projects, state
from .config import Config

DEFAULT = "DEFAULT"
WORKSTATION = "WORKSTATION"
STARTING = "STARTING_WORKSTATION"
EXITING = "EXITING_WORKSTATION"
PLACEMENT_WINDOW = 90  # seconds during which freshly launched windows are auto-placed


BOUNCE = 1.5  # seconds: a second toggle this soon after a switch is a key repeat, not a decision


@contextlib.contextmanager
def switching():
    """One mode switch at a time. Yields False when another one is in progress (a double key
    press, the shortcut and the panel menu at once): the caller then does nothing, so nothing
    is ever launched twice and two leave dialogs never stack."""
    from .lock import try_lock

    handle = open(paths.ensure(paths.state_dir()) / "mode.lock", "w")
    try:
        if not try_lock(handle):
            yield False
            return
        yield True
    finally:
        handle.close()


def bounced() -> bool:
    return 0 <= time.time() - float(load().get("switched", 0) or 0) < BOUNCE


def load() -> dict:
    data = state.load("mode")
    data.setdefault("mode", DEFAULT)
    data.setdefault("saved", {})
    data.setdefault("launched", False)
    return data


def current() -> str:
    return load()["mode"]


def is_work(mode: str | None = None) -> bool:
    return (mode or current()) in (WORKSTATION, STARTING)


def _write_shell(cfg: Config, mode: str, rules: list[dict], close: list[dict] | None = None) -> None:
    """Hand-off file for the optional GNOME helper extension / macOS menu bar."""
    state.save(
        "shell",
        {
            # Two keys on purpose: a desktop helper that predates ownership tracking reads only
            # `close` and therefore can never close an editor or browser window.
            "close": [rule for rule in close or [] if not rule.get("owned")],
            "close_owned": [{k: v for k, v in rule.items() if k != "owned"} for rule in close or [] if rule.get("owned")],
            "close_id": time.time() if close else 0,
            "mode": mode,
            "label": cfg.get("workstation.indicator", "WORK") if mode == WORKSTATION else "",
            "rules": rules,
            "until": time.time() + PLACEMENT_WINDOW if rules else 0,
            "suw": str(paths.launcher()),
        },
    )


def on(cfg: Config, relaunch: bool = False) -> list[str]:
    from ..integrations import apps, browser, gnome

    data = load()
    steps: list[str] = []
    resumed = data["mode"] == STARTING
    already = data["mode"] == WORKSTATION
    linux_gnome = paths.platform() == "linux" and gnome.available()

    if not already and not resumed:
        data["saved"] = gnome.capture() if linux_gnome else {}
        data["since"] = time.time()
        data["launched"] = False
    data["mode"] = STARTING
    state.save("mode", data)

    if linux_gnome:
        kind, _exe = browser.detect(cfg)
        gnome.apply_workstation(cfg, gnome.dock_for(cfg, apps.editor(cfg), kind))
        steps.append("appearance and workspaces applied")

    rules: list[dict] = []
    if not data["launched"] or relaunch:
        launch = cfg.get("workstation.launch", {})
        project = apps.workdir(cfg) or projects.active(cfg)
        data["preexisting_editor"] = bool(apps.editor(cfg)) and apps.running(apps.editor(cfg)) > 0
        if launch.get("editor", True):
            rule = apps.open_editor(cfg, project, 0)
            steps.append(f"editor: {'opened ' + project.name if rule is not None and project else 'opened' if rule is not None else 'not found'}")
            rules += [rule] if rule else []
        if launch.get("terminal", True):
            rule = apps.open_terminal(cfg, "main", apps.TERMINAL_TITLE, 1, project)
            steps.append("terminal: " + ("opened" if rule is not None else "already attached"))
            rules += [rule] if rule else []
        if launch.get("servers", True) and apps.ensure_servers_session(cfg):
            rule = apps.open_terminal(cfg, "servers", apps.SERVERS_TITLE, 2)
            steps.append("servers: " + ("opened" if rule is not None else "already attached"))
            rules += [rule] if rule else []
        if launch.get("browser", True):
            ok, note = browser.open_session(cfg, "workstation", force=relaunch)
            steps.append(f"browser: {note}")
            kind, _exe = browser.detect(cfg)
            if ok and kind:
                rules.append({"class": {"chrome": "google-chrome"}.get(kind, kind), "workspace": 3})
        if launch.get("files", False):
            rule = apps.open_files(4)
            rules += [rule] if rule else []
        data["launched"] = True
    else:
        steps.append("apps already launched for this session (use --relaunch to reopen)")

    if not rules:  # a repeated call must not cancel the placement of windows still opening
        previous = state.load("shell")
        if previous.get("mode") == WORKSTATION and float(previous.get("until", 0)) > time.time():
            rules = list(previous.get("rules", []))
    _write_shell(cfg, WORKSTATION, rules)
    data["mode"] = WORKSTATION
    data["switched"] = time.time()
    state.save("mode", data)
    if not already:
        events.emit("mode.changed", "Workstation Mode on", mode=WORKSTATION)
        steps += readiness(cfg)
    return steps


def readiness(cfg: Config) -> list[str]:
    """One line per thing the workstation leans on, from what is already known (no waiting on
    the network: entering the mode must be instant even when everything else is down)."""
    from ..integrations import worksync

    lines = []
    try:
        work = worksync.status(cfg, deep=False)
        if work["state"] not in ("DISABLED",):
            lines.append(f"work folder: {worksync.WORDS.get(work['state'], work['state'].lower())}")
    except Exception as exc:
        lines.append(f"work folder: could not be checked ({exc})")
    runtime = state.load("runtime")
    if time.time() - runtime.get("ts", 0) < 600:
        for name in ("home", "cloud"):
            report = runtime.get(name)
            if report is not None:
                lines.append(f"{name}: {'reachable' if report.get('online') else 'not reachable (local work is unaffected)'}")
    return lines


def leave_summary(cfg: Config) -> dict:
    """What is in flight right now — shown before leaving. Uncommitted work is normal, not an error."""
    from . import syncer
    from ..integrations import apps, worksync

    out = {"uncommitted": 0, "claude": 0, "ssh": 0, "pending_sync": 0, "conflicts": 0}
    try:
        out["uncommitted"] = sum(1 for row in syncer.local_rows(cfg) if row.get("dirty") and row.get("path") != str(paths.shared_dir()))
    except Exception:
        pass
    out["claude"] = apps.running("claude")
    out["ssh"] = apps.running("ssh")
    try:
        work = worksync.status(cfg, deep=True)
        out["pending_sync"] = int(work.get("need_items", 0)) + int(work.get("outgoing_items", 0))
        out["conflicts"] = len(work.get("conflicts", []))
        out["work_state"] = work["state"]
    except Exception:
        pass
    return out


def close_report(since: float, wait: float = 6.0) -> list[str] | None:
    """Titles of workstation windows that did not close (they are asking about unsaved work).
    None when no desktop helper answered."""
    import json

    file = state.path("shell").with_name("shell-report.json")
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            report = json.loads(file.read_text())
            if float(report.get("close_id", 0)) >= since:
                return [str(title) for title in report.get("remaining", [])]
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    return None


def save_all(cfg: Config) -> str:
    """Checkpoint and push every managed project now. Returns a one-line summary."""
    from . import syncer
    from ..daemon import client

    reply = client.call("sync", timeout=200)
    rows = reply["projects"] if reply and reply.get("ok") else syncer.sync_all(cfg, do_fetch=True, force_checkpoint=True)
    counts = syncer.counts(rows)
    waiting = [r["name"] for r in rows if r["status"] not in ("SYNCED", "CLEAN", "LOCAL_COMMITTED") or r.get("dirty")]
    events.emit("mode.saved", f"saved on leaving Workstation Mode: {counts['managed']} project(s), {len(waiting)} not fully synced")
    if not waiting:
        return f"saved: {counts['managed']} project(s) committed and pushed"
    return f"saved what could be saved; still waiting: {', '.join(waiting[:4])} (suw sync status)"


def off(cfg: Config, *, save: bool = False, close: bool = False) -> list[str]:
    """Leave Workstation Mode. `save` checkpoints and pushes every managed project first;
    `close` then asks the workstation windows to close (applications prompt for their own
    unsaved documents; tmux sessions and background services keep running)."""
    from ..integrations import apps, gnome

    data = load()
    steps = []
    if save:
        try:
            steps.append(save_all(cfg))
        except Exception as exc:  # leaving the mode must not fail because a remote is down
            steps.append(f"save incomplete: {exc}")
        try:  # hand the latest edits to the work folder sync now instead of at its next scan
            from ..integrations import worksync

            if worksync.instance(cfg).rescan():
                steps.append("work folder: latest changes queued for the other workstation")
        except Exception:
            pass  # the sync service picks them up on its own
    closing = apps.close_rules(cfg) if close else []
    if data.get("preexisting_editor"):
        # The editor was already open before Workstation Mode started: it is the user's, not ours.
        closing = [rule for rule in closing if not rule.get("owned")]
    if close:
        detached = apps.detach_terminals()
        steps.append(f"closing workstation windows ({detached} terminal(s) detached; tmux sessions keep running)")
        if paths.platform() == "linux" and gnome.available() and gnome.extension_state() != "active" and any(rule.get("owned") for rule in closing):
            steps.append("the editor was left open: the desktop helper that tracks which windows are ours loads at the next login")
    if data["mode"] == DEFAULT and not data["saved"]:
        _write_shell(cfg, DEFAULT, [], closing)
        return [*steps, "already in Default Mode"]
    was_work = data["mode"] != DEFAULT
    data["mode"] = EXITING
    state.save("mode", data)
    if data["saved"] and paths.platform() == "linux":
        gnome.restore(data["saved"])
        steps.append("desktop settings restored")
    _write_shell(cfg, DEFAULT, [], closing)
    state.save("mode", {"mode": DEFAULT, "saved": {}, "launched": False, "since": time.time(), "switched": time.time()})
    if was_work:
        events.emit("mode.changed", "Workstation Mode off", mode=DEFAULT)
    steps.append("background services left running" if close else "apps, browser tabs and background services left running")
    return steps


def toggle(cfg: Config) -> tuple[str, list[str]]:
    if is_work():
        return DEFAULT, off(cfg)
    return WORKSTATION, on(cfg)
