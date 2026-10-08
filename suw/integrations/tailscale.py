"""Tailscale: the network fabric. Read-only here; enrollment lives in suw.cloud."""

from __future__ import annotations

import json

from ..core.proc import have, run


def status() -> dict:
    """Normalised `tailscale status --json`. `installed`/`running` are always present."""
    if not have("tailscale"):
        return {"installed": False, "running": False, "peers": []}
    res = run(["tailscale", "status", "--json"], timeout=8)
    if not res.ok:
        return {"installed": True, "running": False, "peers": [], "error": res.text[:200]}
    try:
        raw = json.loads(res.out)
    except ValueError:
        return {"installed": True, "running": False, "peers": []}
    me = raw.get("Self") or {}
    suffix = raw.get("MagicDNSSuffix") or ""
    peers = [_peer(p) for p in (raw.get("Peer") or {}).values()]
    return {
        "installed": True,
        "running": raw.get("BackendState") == "Running",
        # `running` only says the local service is up. `online` says it can actually reach
        # the tailnet: on a network that blocks it the service keeps "running" while every
        # peer is unreachable.
        "online": raw.get("BackendState") == "Running" and bool(me.get("Online", False)),
        "health": [str(line)[:200] for line in (raw.get("Health") or [])][:3],
        "state": raw.get("BackendState", ""),
        "magic_dns": suffix,
        "self": _peer(me),
        "peers": peers,
    }


def _peer(node: dict) -> dict:
    dns = (node.get("DNSName") or "").rstrip(".")
    return {
        "name": dns.split(".", 1)[0] if dns else (node.get("HostName") or ""),
        "dns": dns,
        "os": node.get("OS", ""),
        "online": bool(node.get("Online", False)),
        "ip": (node.get("TailscaleIPs") or [""])[0],
        "last_seen": _epoch(node.get("LastSeen", "")),
    }


def _epoch(stamp: str) -> float:
    """RFC 3339 -> epoch seconds (0 when unknown; Tailscale reports year 1 for 'never')."""
    from datetime import datetime

    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00")[:19] + "+00:00").timestamp()
    except (ValueError, OverflowError):
        return 0.0
    return value if value > 0 else 0.0


def cut_off(st: dict) -> bool:
    """The service runs but has no path to the tailnet (blocked network, captive portal).
    An older cached snapshot without the `online` field counts as connected."""
    return bool(st.get("running")) and st.get("online") is False


def on_tailnet(host: str, st: dict) -> bool:
    """Is this address one that only the tailnet can reach?"""
    suffix = st.get("magic_dns") or ""
    return bool(host) and (host.startswith("100.") or bool(suffix) and host.endswith(suffix) or "." not in host)


def find(name: str, st: dict | None = None) -> dict | None:
    """Look a machine up by its tailnet name (short or fully qualified)."""
    st = st or status()
    short = name.split(".", 1)[0].lower()
    for peer in [st.get("self") or {}, *st.get("peers", [])]:
        if peer.get("name", "").lower() == short:
            return peer
    return None
