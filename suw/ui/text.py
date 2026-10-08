"""Terminal rendering. State is always carried by a symbol *and* a word, never colour alone."""

from __future__ import annotations

import os
import sys
import time

from ..core.gitsync import ATTENTION, LABELS, Status

_COLORS = {"dim": "2", "bold": "1", "ok": "32", "warn": "33", "err": "31", "accent": "36"}
# component status -> (symbol, style)
MARKS = {
    "ONLINE": ("●", "ok"),
    "SYNCED": ("●", "ok"),
    "OFFLINE": ("○", "dim"),
    "DEGRADED": ("◐", "warn"),
    "SYNCING": ("↻", "accent"),
    "PENDING": ("↻", "accent"),
    "ERROR": ("✗", "err"),
    "NEEDS ATTENTION": ("⚠", "warn"),
    "UNKNOWN": ("◌", "dim"),
    "NOT_CONFIGURED": ("○", "dim"),
    "AUTH_REQUIRED": ("🔑", "warn"),
    "UNTRUSTED": ("✗", "err"),
}


def color_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    return stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def paint(text: str, style: str, enabled: bool | None = None) -> str:
    if enabled is None:
        enabled = color_enabled()
    return f"\033[{_COLORS[style]}m{text}\033[0m" if enabled and style in _COLORS else text


def dot(online: bool | None) -> str:
    if online is None:
        return paint("◌", "dim")
    return paint("●", "ok") if online else paint("○", "err")


def mark(status: str) -> str:
    symbol, style = MARKS.get(status, ("◌", "dim"))
    return paint(symbol, style)


def word(status: str) -> str:
    return status.replace("_", " ")


def pct(value) -> str:
    return f"{int(value)}%" if isinstance(value, (int, float)) else "–"


def ago(ts: float | None) -> str:
    if not ts:
        return ""
    seconds = max(0, int(time.time() - ts))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 129600:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def sync_label(status: str) -> str:
    try:
        key = Status(status)
    except ValueError:
        return status
    symbol, label = LABELS[key]
    if key in (Status.SYNCED, Status.CLEAN):
        style = "ok"
    elif key in (Status.CONFLICT, Status.RECOVERY_REQUIRED, Status.PUSH_FAILED):
        style = "err"
    elif key in ATTENTION:
        style = "warn"
    elif key in (Status.OFFLINE, Status.LOCAL_COMMITTED):
        style = "dim"
    else:
        style = "accent"
    return paint(f"{symbol} {label}", style)


def project_line(row: dict) -> str:
    extra = []
    if row.get("ahead"):
        extra.append(f"↑{row['ahead']}")
    if row.get("behind"):
        extra.append(f"↓{row['behind']}")
    extra.append(f"{row['dirty']} uncommitted" if row.get("dirty") else "clean")
    return " • ".join(extra)


def link_note(count: int) -> str:
    """Whether a terminal on this machine is connected there right now."""
    if count <= 0:
        return paint("not connected", "dim")
    return paint("connected" + (f" ×{count}" if count > 1 else ""), "ok")


def server_line(name: str, server: dict, sessions: int = 0) -> str:
    title = f"{name:<10}"  # the name is what you type: ssh home, ssh mac, ssh cloud
    status = server.get("status", "UNKNOWN")
    if status == "NOT_CONFIGURED":
        return f"{mark(status)} {title} " + paint("not configured" + (" • to rent one, type: ssh cloud" if name == "cloud" else ""), "dim")
    if status == "UNKNOWN":
        return f"{mark(status)} {title} {paint('checking…', 'dim')}"
    if status not in ("ONLINE", "DEGRADED"):
        return f"{mark(status)} {title} {word(status).lower()}"
    bits = [word(status).lower()]
    gpus = server.get("gpus") or []
    if gpus:
        from ..core.health import gpu_label

        bits.append(gpu_label(gpus))
        bits.append(f"{round(max(g['vram_mb'] for g in gpus) / 1024)}GB")
    elif server.get("cpu_pct") is not None:
        bits.append(f"CPU {pct(server.get('cpu_pct'))}")
        bits.append(f"RAM {pct(server.get('ram_used_pct'))}")
    if server.get("disk_used_pct") is not None:
        bits.append(f"Disk {pct(server.get('disk_used_pct'))}")
    if isinstance(server.get("uptime_s"), (int, float)):
        bits.append(f"up {int(server['uptime_s'] // 86400)}d {int(server['uptime_s'] % 86400 // 3600)}h")
    return f"{mark(status)} {title} " + " • ".join([link_note(sessions), *bits])


def workstation_line(ws: dict, sessions: int = 0) -> str:
    title = f"{ws['name']:<10}"
    if ws["self"]:
        return f"{mark('ONLINE')} {title} this machine"
    if not ws["enrolled"]:
        return f"{mark('NOT_CONFIGURED')} {title} {paint('not set up yet', 'dim')}"
    if ws["online"]:
        return f"{mark('ONLINE')} {title} {link_note(sessions)} • online"
    seen = ago(ws.get("last_seen"))
    return f"{mark('OFFLINE')} {title} offline" + (paint(f" • last seen {seen}", "dim") if seen else "")


def _row(label: str, status: str, note: str = "") -> str:
    return f"{label:<11} {mark(status)} {word(status)}" + (paint(f"  {note}", "dim") if note else "")


_WORK = {"IN_SYNC": "SYNCED", "SYNCING": "SYNCING", "PEER_OFFLINE": "SYNCED", "NO_PEER": "NOT_CONFIGURED", "PAUSED": "NEEDS ATTENTION", "CONFLICTS": "NEEDS ATTENTION", "ERROR": "ERROR", "STOPPED": "ERROR"}
_WORK_NOTE = {"PEER_OFFLINE": "other workstation offline; changes queued", "NO_PEER": "no other workstation paired yet; files stay on this machine", "PAUSED": "paused", "CONFLICTS": "conflict copies to review", "STOPPED": "service not running", "NOT_CONFIGURED": "suw work setup", "NOT_INSTALLED": "Syncthing not installed"}
_INPUT = {"CONNECTED": ("ONLINE", "keyboard, mouse and clipboard shared"), "WAITING": ("OFFLINE", "other workstation not connected"), "UNPAIRED": ("NOT_CONFIGURED", "no other workstation approved yet"), "STOPPED": ("OFFLINE", "sharing service not running"), "NOT_CONFIGURED": ("NOT_CONFIGURED", "suw peripherals setup"), "NOT_INSTALLED": ("NOT_CONFIGURED", "Deskflow not installed")}


def extra_rows(snap: dict) -> list[str]:
    """Shared work folder and shared keyboard/mouse, one line each."""
    rows = []
    work = snap.get("work") or {}
    if work.get("state") and work["state"] != "DISABLED":
        rows.append(_row("Work folder", _WORK.get(work["state"], "NOT_CONFIGURED"), _WORK_NOTE.get(work["state"], "")))
    periph = snap.get("peripherals") or {}
    if periph.get("state") in _INPUT:
        status, note = _INPUT[periph["state"]]
        rows.append(_row("Input", status, note))
    return rows


def render_summary(snap: dict) -> str:
    """`suw status`: one line per component, machine-style words, a verdict at the end."""
    work = snap["mode"] != "DEFAULT"
    comp, sync = snap["components"], snap["sync"]
    lines = [paint(snap["brand"].upper(), "bold"), ""]
    lines.append(f"{'Mode':<11} {paint('● WORKSTATION', 'accent') if work else paint('○ DEFAULT', 'dim')}")
    for ws in snap["workstations"]:
        note = "this machine" if ws["self"] else ("last seen " + ago(ws["last_seen"]) if ws["status"] == "OFFLINE" and ws.get("last_seen") else "")
        lines.append(_row(ws["name"], ws["status"], note))
    for name in ("home", "cloud"):
        server = snap["servers"][name]
        note = server.get("label", "") if name == "cloud" and server["status"] in ("ONLINE", "DEGRADED") else ""
        lines.append(_row(name, server["status"], note))
    lines.append(_row("Tailscale", comp["tailscale"], "running, but this network blocks the tailnet" if comp["tailscale"] == "DEGRADED" else ""))
    lines.append(_row("GitHub", comp["github"], "" if comp["github"] != "UNKNOWN" else "not checked"))
    lines.append(f"{'Sync':<11} {mark(sync['word'])} {sync['word']}")
    lines.append(f"{'Conflicts':<11} {sync['conflicts']}")
    lines += extra_rows(snap)
    lines.append(_row("Artifacts", snap["artifacts"]["status"], snap["artifacts"]["store"]))
    lines.append(_row("Daemon", comp["daemon"], "" if snap["daemon"] else "local view only: suw daemon start"))
    lines += ["", "Projects", f"  {sync['managed']} managed", f"  {sync['pending']} pending", f"  {sync['conflicts']} conflicts"]
    lines.append("")
    if snap["attention"]:
        lines.append(paint("⚠ NEEDS ATTENTION", "warn"))
        lines += [f"  {item}" for item in snap["attention"]]
        if snap.get("still_works"):
            lines.append(paint("✓ Unaffected: " + ", ".join(snap["still_works"]), "ok"))
    else:
        lines.append(paint("✓ READY", "ok"))
    return "\n".join(lines)


def render_status(snap: dict, width: int = 47) -> str:
    """The compact Control Center panel, as text."""
    rule = paint("─" * width, "dim")
    work = snap["mode"] != "DEFAULT"
    system, sync, comp, clip = snap["system"], snap["sync"], snap["components"], snap["clipboard"]
    live = snap.get("sessions") or {}
    lines = [paint(snap["brand"].upper(), "bold") + paint(time.strftime("   updated %H:%M:%S"), "dim"), "", paint("MODE", "dim")]
    lines.append(paint("● WORKSTATION", "accent") if work else paint("○ DEFAULT", "dim"))
    lines += [rule, paint("MACHINES", "dim")]
    for ws in snap["workstations"]:
        lines.append(workstation_line(ws, live.get(ws["name"], 0)))
    for name in ("home", "cloud"):
        lines.append(server_line(name, snap["servers"][name], live.get(name, 0)))
    lines += [rule, paint("THIS MACHINE", "dim")]
    usage = f"CPU {pct(system.get('cpu_pct'))}   RAM {pct(system.get('ram_used_pct'))}   Disk {pct(system.get('disk_used_pct'))}"
    if system.get("battery_pct") is not None:
        usage += f"   Bat {pct(system.get('battery_pct'))}"
    if snap.get("local_gpu"):
        usage += f"   GPU {snap['local_gpu']}"
    lines.append(usage)
    lines += [rule, paint("SYNC", "dim")]
    lines.append(f"{mark(comp['github'])} GitHub" + (paint(f"  {word(comp['github']).lower()}", "dim") if comp["github"] != "ONLINE" else ""))
    lines.append(f"{mark(sync['word'])} Projects {sync['managed']}   Pending {sync['pending']}   Conflicts {sync['conflicts']}")
    row = snap.get("project")
    if row:
        lines.append(f"{paint(row['name'], 'bold')} {paint(row.get('branch') or '–', 'dim')}  {sync_label(row['status'])} • {project_line(row)}")
    busy = [r for r in snap["projects"] if r["status"] not in ("SYNCED", "CLEAN") and (not row or r["path"] != row["path"])]
    for other in busy[:3]:
        lines.append(f"{other['name']}  {sync_label(other['status'])}")
    lines += [row.replace("Work folder", "Work").replace("  ", " ", 1) for row in extra_rows(snap)]
    lines += [rule, paint("NETWORK", "dim"), f"{mark(comp['tailscale'])} Tailscale" + ("" if comp["tailscale"] == "ONLINE" else paint(f"  {word(comp['tailscale']).lower()}", "dim"))]
    lines += [rule, paint("CLIPBOARD", "dim")]
    lines.append(f"{mark(clip['local'])} Local")
    ssh_note = "" if clip["ssh"] == "ONLINE" else paint("  terminal lacks OSC 52" if clip["ssh"] == "DEGRADED" else "  run: suw clipboard test", "dim")
    lines.append(f"{mark(clip['ssh'])} SSH{ssh_note}")
    for ws in snap["workstations"]:
        if not ws["self"]:
            state = "not set up" if not ws["enrolled"] else ("offline" if not ws["online"] else "see docs/clipboard.md")
            lines.append(f"{mark('ONLINE' if ws['online'] and ws['enrolled'] else 'OFFLINE')} {ws['name']} {state}")
    lines.append(rule)
    if snap["attention"]:
        lines.append(paint("⚠ Needs attention", "warn"))
        lines += [f"  {item}" for item in snap["attention"]]
        if snap.get("still_works"):
            lines.append(paint("✓ Unaffected: " + ", ".join(snap["still_works"]), "ok"))
    else:
        lines.append(paint("✓ Ready", "ok") + ("" if snap.get("daemon") else paint("  (daemon not running: local view)", "dim")))
    return "\n".join(lines)
