"""Health and hardware discovery through one read-only probe script (local or over SSH)."""

from __future__ import annotations

import re
import time
from pathlib import Path

from .proc import Result, run, ssh_cmd

PROBE = Path(__file__).with_name("probe.sh")
ONLINE, OFFLINE, DEGRADED, SYNCING, ERROR, UNKNOWN, NOT_CONFIGURED = "ONLINE", "OFFLINE", "DEGRADED", "SYNCING", "ERROR", "UNKNOWN", "NOT_CONFIGURED"
AUTH_REQUIRED, UNTRUSTED = "AUTH_REQUIRED", "UNTRUSTED"
_SERVICE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$")  # never starts with "-": it is passed as an argument
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=6",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
    "-o", "StrictHostKeyChecking=yes",
    # Health checks always perform a fresh handshake: a multiplexed connection would keep
    # answering after the host key changed, hiding exactly what the check must detect.
    "-o", "ControlPath=none",
]


def parse(text: str) -> dict:
    """key=value lines -> dict; repeated `gpu` lines and `service.*` are collected."""
    data: dict = {"gpus": [], "services": {}}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if key == "gpu":
            parts = [p.strip() for p in value.split(",")]
            if parts and parts[0]:
                data["gpus"].append(
                    {
                        "name": parts[0],
                        "vram_mb": _num(parts[1]) if len(parts) > 1 else 0,
                        "used_mb": _num(parts[2]) if len(parts) > 2 else 0,
                        "util_pct": _num(parts[3]) if len(parts) > 3 else 0,
                    }
                )
        elif key.startswith("service."):
            data["services"][key[8:]] = value
        elif value != "":
            data[key] = _num(value) if re.fullmatch(r"-?\d+(\.\d+)?", value) else value
    return data


def _num(text: str) -> float | int:
    try:
        number = float(text)
    except ValueError:
        return 0
    return int(number) if number.is_integer() else number


def hardware(report: dict) -> dict:
    """Stable capability facts (for the inventory), as opposed to live load."""
    gpus = report.get("gpus", [])
    out = {
        "os": report.get("os", ""),
        "kernel": report.get("kernel", ""),
        "arch": report.get("arch", ""),
        "cpu_model": report.get("cpu_model", ""),
        "cores": int(report.get("cores", 0) or 0),
        "ram_gb": round(float(report.get("ram_mb", 0) or 0) / 1024),
        "disk_total_gb": int(report.get("disk_total_gb", 0) or 0),
        "gpu": gpu_label(gpus),
        "gpu_count": len(gpus),
        "gpu_vram_gb": round(max((g["vram_mb"] for g in gpus), default=0) / 1024),
        "cuda": str(report.get("cuda", "") or ""),
        "docker": str(report.get("docker", "") or ""),
    }
    return {k: v for k, v in out.items() if v not in ("", 0)}


def gpu_label(gpus: list[dict]) -> str:
    if not gpus:
        return ""
    name = re.sub(r"^(NVIDIA|GeForce|Tesla)\s+", "", gpus[0]["name"]).replace("GeForce ", "")
    return f"{len(gpus)}x {name}" if len(gpus) > 1 else name


def _services(names: list[str]) -> list[str]:
    return [n for n in names if _SERVICE.match(n)]


def route() -> str:
    """The local address the kernel would use to reach the internet, or "" when there is no
    route. Connecting a UDP socket sends nothing; it only asks the routing table."""
    import socket

    for family, target in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 9))
                return str(sock.getsockname()[0])
        except OSError:
            continue
    return ""


def probe_local(services: list[str] | None = None) -> dict:
    res = run(["sh", str(PROBE), *_services(services or [])], timeout=15)
    data = parse(res.out)
    data["online"] = True
    data["checked"] = time.time()
    return data


def ssh(alias: str, remote: list[str], *, stdin: str | None = None, timeout: float = 20) -> Result:
    """Non-interactive SSH to a logical alias from the managed SSH config."""
    return run([*ssh_cmd(), *SSH_OPTS, alias, "--", *remote], stdin=stdin, timeout=timeout)


def probe_remote(alias: str, services: list[str] | None = None, timeout: float = 20) -> dict:
    res = ssh(alias, ["sh", "-s", "--", *_services(services or [])], stdin=PROBE.read_text(), timeout=timeout)
    if not res.ok or "probe=1" not in res.out:
        error = (res.err.strip().splitlines() or ["unreachable"])[-1][:200]
        changed = "identification has changed" in res.err.lower() or "host key for" in res.err.lower() and "has changed" in res.err.lower()
        return {"online": False, "checked": time.time(), "error": error, "status": failure_status(res.err), "identity_changed": changed}
    data = parse(res.out)
    data["online"] = True
    data["checked"] = time.time()
    data["status"] = DEGRADED if any(st != "active" for st in data["services"].values()) else ONLINE
    return data


def failure_status(stderr: str) -> str:
    """Why a server did not answer. Never collapses everything into OFFLINE."""
    text = stderr.lower()
    if "remote host identification has changed" in text or "host key verification failed" in text or "no matching host key" in text or "host key is known" in text:
        return UNTRUSTED
    if "permission denied" in text or "too many authentication failures" in text:
        return AUTH_REQUIRED
    return OFFLINE


def identity_changed(report: dict) -> bool:
    return report.get("status") == UNTRUSTED and bool(report.get("identity_changed"))


def server_status(configured: bool, report: dict | None, *, direct: bool = False) -> str:
    """Component status for a server role (pure)."""
    if not configured:
        return NOT_CONFIGURED
    if not report or "online" not in report:
        return UNKNOWN
    status = report.get("status") or (ONLINE if report.get("online") else OFFLINE)
    if status == ONLINE and direct:
        return DEGRADED  # reachable, but over a public endpoint instead of the tailnet
    return status
