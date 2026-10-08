"""Device inventory: stable logical names on top of replaceable machines.

Lives in the shared configuration repository so every workstation sees the same fleet.
`cloud` is a role, not a machine: `cloud.current` points at whichever node holds it now and
retired nodes stay in the history. Nothing secret is stored here — host *public* keys only.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import config, paths, tomlw

HEADER = (
    "SUW inventory - logical devices and the cloud role.\n"
    "Managed by `suw`; safe to read, edit with care. Never put secrets here."
)
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


def file() -> Path:
    return paths.shared_dir() / "inventory.toml"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load() -> dict:
    data = config.read_toml(file())
    data.setdefault("devices", {})
    cloud = data.setdefault("cloud", {})
    cloud.setdefault("current", "")
    cloud.setdefault("nodes", {})
    return data


def save(data: dict) -> None:
    tomlw.dump(file(), data, HEADER, mode=0o644)


def valid_host(host: str) -> bool:
    """Hostname, IPv4 or IPv6 literal. Rejects anything that could smuggle ssh options."""
    if not host or host.startswith("-"):
        return False
    import ipaddress

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST.match(host))


def valid_user(user: str) -> bool:
    return bool(_USER.match(user))


def valid_label(label: str) -> bool:
    return bool(_LABEL.match(label))


def ensure_device(data: dict, name: str, **fields: str) -> dict:
    device = data["devices"].setdefault(name, {})
    device.setdefault("logical_name", name)
    for key, value in fields.items():
        if value is not None and value != "":
            device[key] = value
    return device


def new_device_id(name: str) -> str:
    """Stable internal identity: `<name>-<random>`. Never derived from user name or IP."""
    import secrets as pysecrets

    return f"{name}-{pysecrets.token_hex(4)}"


def device_host(device: dict) -> str:
    """Address to connect to: Tailscale/MagicDNS name first, explicit host otherwise."""
    return device.get("tailscale_name") or device.get("host") or ""


# ── cloud role ──────────────────────────────────────────────────────────────


def cloud_current(data: dict) -> tuple[str, dict] | tuple[None, None]:
    label = data["cloud"].get("current") or ""
    node = data["cloud"]["nodes"].get(label)
    return (label, node) if node else (None, None)


def new_cloud_label(data: dict, when: datetime | None = None) -> str:
    """Deterministic, sortable label: cloud-YYYYMMDD[-n]."""
    stamp = (when or datetime.now()).strftime("%Y%m%d")
    base = f"cloud-{stamp}"
    label, index = base, 2
    while label in data["cloud"]["nodes"]:
        label = f"{base}-{index}"
        index += 1
    return label


def cloud_assign(data: dict, label: str, node: dict) -> str | None:
    """Give the cloud role to `label`; the previous holder is retired, never deleted.

    Returns the label of the node that was retired, if any.
    """
    previous, old = cloud_current(data)
    if old is not None and previous != label:
        old["state"] = "retired"
        old["retired_at"] = now()
    node["state"] = "active"
    node.setdefault("enrolled_at", now())
    data["cloud"]["nodes"][label] = node
    data["cloud"]["current"] = label
    return previous if previous != label else None


def cloud_release(data: dict) -> str | None:
    """Retire the current node and leave the role empty."""
    label, node = cloud_current(data)
    if node is None:
        return None
    node["state"] = "retired"
    node["retired_at"] = now()
    data["cloud"]["current"] = ""
    return label


def capabilities(hardware: dict) -> list[str]:
    """What a node is suitable for, derived from hardware rather than brand names."""
    out: list[str] = []
    vram = float(hardware.get("gpu_vram_gb") or 0)
    ram = float(hardware.get("ram_gb") or 0)
    cores = int(hardware.get("cores") or 0)
    if hardware.get("gpu") and hardware.get("cuda"):
        if vram >= 16:
            out.append("training")
        out.append("inference")
        out.append("embeddings")
    if cores >= 4 and ram >= 8:
        out.append("batch workloads")
    if not out:
        out.append("lightweight services")
    return out


def recommend_profile(hardware: dict) -> str:
    if hardware.get("gpu"):
        vram = float(hardware.get("gpu_vram_gb") or 0)
        return "training" if vram >= 16 else "inference"
    cores = int(hardware.get("cores") or 0)
    return "compute" if cores >= 4 else "minimal"
