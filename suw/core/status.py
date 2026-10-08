"""One snapshot that answers: can I work right now, and is everything healthy?

Every component reports exactly one of
    ONLINE  OFFLINE  DEGRADED  SYNCING  ERROR  UNKNOWN  NOT_CONFIGURED
(servers additionally AUTH_REQUIRED / UNTRUSTED), so the snapshot is equally usable by a
person, the Control Center and a script (`suw status --json`).
"""

from __future__ import annotations

import os
import time

from . import health, inventory, modes, paths, projects, state, syncer
from .config import Config
from .gitsync import ATTENTION, LABELS, Status
from .health import DEGRADED, ERROR, NOT_CONFIGURED, OFFLINE, ONLINE, SYNCING, UNKNOWN
from .proc import have

# A powered-off workstation, an empty cloud role or an unset home server is a normal
# state, not a problem: these never make the overall verdict worse.
BENIGN = {ONLINE, OFFLINE, NOT_CONFIGURED, UNKNOWN, SYNCING}


def remote_view(inv: dict, runtime: dict) -> dict:
    servers = {}
    home = inv["devices"].get("home", {})
    home_ok = bool(inventory.device_host(home))
    report = runtime.get("home", {})
    servers["home"] = {"configured": home_ok, **report, "status": health.server_status(home_ok, report)}
    label, node = inventory.cloud_current(inv)
    report = runtime.get("cloud", {})
    cloud = {"configured": node is not None, "label": label or "", **report}
    cloud["status"] = health.server_status(node is not None, report, direct=bool(node) and not node.get("tailscale_name"))
    cloud["identity_changed"] = health.identity_changed(report)
    if node:
        cloud["hardware"] = node.get("hardware", {})
    servers["cloud"] = cloud
    return servers


def sessions(cfg: Config, inv: dict) -> dict:
    """Open terminal connections from this machine, counted live (never from the daemon's cache)."""
    from ..integrations import apps

    names = [n for n, d in sorted(inv["devices"].items()) if n != cfg.device and inventory.device_host(d)]
    if inventory.cloud_current(inv)[1]:
        names.append("cloud")
    try:
        return apps.ssh_sessions(names)
    except Exception:  # the panel must render even where `ps` is unusual
        return {}


def workstations(cfg: Config, inv: dict, tailnet: dict) -> list[dict]:
    from ..integrations import tailscale

    rows = []
    for name, device in sorted(inv["devices"].items()):
        if device.get("role") != "workstation":
            continue
        me = name == cfg.device
        enrolled = me or bool(device.get("tailscale_name"))
        peer = tailscale.find(device.get("tailscale_name", ""), tailnet) if device.get("tailscale_name") else None
        online = True if me else bool(peer and peer.get("online"))
        rows.append(
            {
                "name": name,
                "id": device.get("id", ""),
                "platform": device.get("platform", ""),
                "self": me,
                "online": online,
                "enrolled": enrolled,
                "status": NOT_CONFIGURED if not enrolled else ONLINE if online else OFFLINE,
                "last_seen": 0 if me or not peer else peer.get("last_seen", 0),
            }
        )
    return rows


def sync_component(rows: list[dict], github: str) -> dict:
    counts = syncer.counts(rows)
    attention = {s.value for s in ATTENTION}
    if any(r["status"] in attention for r in rows):
        status, word = ERROR, "NEEDS ATTENTION"
    elif github == OFFLINE:
        status, word = OFFLINE, "OFFLINE"
    elif counts["pending"]:
        status, word = SYNCING, "PENDING"
    elif not rows:
        status, word = NOT_CONFIGURED, "NO PROJECTS"
    else:
        status, word = ONLINE, "SYNCED"
    return {"status": status, "word": word, **counts}


def github_status(rows: list[dict]) -> str:
    """Derived from what the last reconciliation saw; costs no network request."""
    remote = [r for r in rows if r.get("upstream")]
    if not remote:
        return UNKNOWN
    if any(r["status"] == Status.AUTH_REQUIRED.value for r in remote):
        return "AUTH_REQUIRED"
    if all(r["status"] == Status.OFFLINE.value for r in remote):
        return OFFLINE
    if any(r["status"] == Status.CLEAN.value for r in remote) and not any(r["status"] == Status.SYNCED.value for r in remote):
        return UNKNOWN  # no daemon: the remote was not checked
    return ONLINE


def clipboard_view(cfg: Config) -> dict:
    """Static capability view (cheap). The live proof is `suw clipboard test`."""
    from ..integrations import clipboard

    return clipboard.capabilities(cfg)


def artifacts_view(cfg: Config) -> dict:
    kind = str(cfg.get("artifacts.store", "dir"))
    if kind == "ssh":
        return {"status": UNKNOWN, "store": f"{cfg.get('artifacts.host', 'home')}:{cfg.get('artifacts.remote_path', 'suw-artifacts')}"}
    root = paths.expand(str(cfg.get("artifacts.path", "~/Projects/archive/artifacts")))
    writable = os.access(root if root.exists() else root.parent, os.W_OK)
    return {"status": ONLINE if writable else ERROR, "store": str(root)}


def build(cfg: Config, *, live: bool = False) -> dict:
    """Assemble the snapshot from the daemon cache; fall back to local facts without it."""
    from ..integrations import tailscale

    runtime = state.load("runtime")
    fresh = time.time() - runtime.get("ts", 0) < 3 * max(60, int(cfg.get("health.remote_seconds", 120)))
    if not fresh:
        runtime = {}
    inv = inventory.load()
    tailnet = runtime.get("tailscale") or tailscale.status()
    rows = runtime.get("projects") if fresh and not live else None
    if rows is None:
        rows = syncer.local_rows(cfg)
    local = runtime.get("local") if fresh and not live else None
    if local is None:
        local = health.probe_local()
    if live:
        runtime = dict(runtime)
        if inventory.device_host(inv["devices"].get("home", {})):
            runtime["home"] = health.probe_remote("home", list(cfg.get("home.services", [])))
        if inventory.cloud_current(inv)[1]:
            runtime["cloud"] = health.probe_remote("cloud")

    active = projects.active(cfg)
    active_row = next((r for r in rows if active and r["path"] == str(active)), None)
    if active and active_row is None:
        from . import gitsync

        active_row = gitsync.summarize(active)
        active_row["unmanaged"] = True

    from ..integrations import peripherals, worksync

    work = runtime.get("work") if fresh and not live else None
    if work is None:
        work = worksync.status(cfg, deep=live)
    periph = runtime.get("peripherals") if fresh and not live else None
    if periph is None:
        periph = peripherals.status(cfg)
    github = github_status(rows)
    tail = (DEGRADED if tailscale.cut_off(tailnet) else ONLINE) if tailnet.get("running") else (OFFLINE if tailnet.get("installed") else NOT_CONFIGURED)
    snapshot = {
        "ts": time.time(),
        "brand": cfg.brand,
        "device": cfg.device,
        "mode": modes.current(),
        "daemon": fresh,
        "components": {
            "daemon": ONLINE if fresh else OFFLINE,
            "tailscale": tail,
            "github": github,
        },
        "tailscale": {"running": bool(tailnet.get("running")), "installed": bool(tailnet.get("installed")), "cut_off": tailscale.cut_off(tailnet)},
        "workstations": workstations(cfg, inv, tailnet),
        "servers": remote_view(inv, runtime),
        "sessions": sessions(cfg, inv),
        "sync": sync_component(rows, github),
        "projects": rows,
        "project": active_row,
        "work": work,
        "peripherals": periph,
        "clipboard": clipboard_view(cfg),
        "artifacts": artifacts_view(cfg),
        "system": {
            k: local.get(k)
            for k in ("cpu_pct", "ram_used_pct", "ram_mb", "disk_used_pct", "battery_pct", "battery_state", "net")
        },
        "local_gpu": health.gpu_label(local.get("gpus", [])),
    }
    snapshot["attention"] = attention(snapshot)
    snapshot["still_works"] = still_works(snapshot)
    snapshot["overall"] = overall(snapshot)
    return snapshot


def attention(snap: dict) -> list[str]:
    """Short list of things that genuinely need the user. Empty means ready."""
    out = []
    cut_off = bool(snap["tailscale"].get("cut_off"))
    attention_states = {s.value for s in ATTENTION}
    for row in snap["projects"]:
        if row["status"] in attention_states:
            out.append(f"{row['name']}: {LABELS[Status(row['status'])][1].lower()}" + (f" — {row['detail']}" if row.get("detail") else ""))
    for name in ("home", "cloud"):
        server = snap["servers"][name]
        if server.get("identity_changed"):
            out.append(f"{name}: identity changed unexpectedly — connection blocked (suw {name} {'verify' if name == 'cloud' else 'trust'})")
        elif server["status"] == OFFLINE and cut_off:
            pass  # reported once below: the cause is this machine's network, not the server
        elif server["status"] in (OFFLINE, "AUTH_REQUIRED", "UNTRUSTED"):
            out.append(f"{name}: {server['status'].lower().replace('_', ' ')}")
        for unit, st in (server.get("services") or {}).items():
            if st != "active":
                out.append(f"{name}: {unit} is {st}")
    work = snap.get("work") or {}
    if work.get("state") in ("ERROR", "STOPPED"):
        detail = (work.get("errors") or [{}])[0].get("error", "") or "the sync service is not running"
        out.append(f"work folder: {detail} (suw work status)")
    elif work.get("state") == "PAUSED":
        out.append("work folder sync is paused (suw work resume)")
    if work.get("conflicts"):
        out.append(f"work folder: {len(work['conflicts'])} conflict cop{'y' if len(work['conflicts']) == 1 else 'ies'}, both versions kept (suw work conflicts)")
    for name in work.get("unpaired", []):
        out.append(f"workstation '{name}' is waiting to be paired (suw work pair {name})")
    disk = snap["system"].get("disk_used_pct")
    if isinstance(disk, (int, float)) and disk >= 92:
        out.append(f"disk {int(disk)}% full")
    if snap["tailscale"]["installed"] and not snap["tailscale"]["running"]:
        out.append("tailscale is not connected")
    elif cut_off:
        out.append("tailscale: this network blocks the tailnet — home, cloud and the other workstation cannot be reached from here")
    if snap["components"]["github"] == "AUTH_REQUIRED":
        out.append("GitHub sign-in required (git credentials were rejected)")
    return out


def still_works(snap: dict) -> list[str]:
    """Reassurance when something is down: what is unaffected."""
    down = [n for n in ("home", "cloud") if snap["servers"][n]["status"] in (OFFLINE, "AUTH_REQUIRED", "UNTRUSTED")]
    if snap["components"]["github"] == OFFLINE:
        down.append("github")
    if not down:
        return []
    out = ["local work (editing, commits, terminal)"]
    if "github" in down:
        out.append("checkpoints keep being committed locally and are pushed when the network returns")
    else:
        out.append("project sync")
    for name in ("home", "cloud"):
        if name not in down and snap["servers"][name]["status"] in (ONLINE, DEGRADED):
            out.append(name)
    return out


def overall(snap: dict) -> str:
    return "NEEDS ATTENTION" if snap["attention"] else "READY"


def local_clipboard_tool() -> str:
    if paths.platform() == "macos":
        return "pbcopy" if have("pbcopy") else ""
    if os.environ.get("WAYLAND_DISPLAY") and have("wl-copy"):
        return "wl-copy"
    return "xclip" if have("xclip") else ("wl-copy" if have("wl-copy") else "")
