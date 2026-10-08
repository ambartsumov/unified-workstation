"""Pairing two workstations without a server in between.

Computer A shows a *pairing code*. It is not a secret: it carries A's public identities
(sync device ID, keyboard-sharing certificate fingerprint) and where A can be reached.
Computer B enters the code. Both then show the same six-digit *confirmation number*, derived
from the two public identities; the user compares them and confirms on both sides. Only then
do the computers trust each other. Nothing on the local network is ever trusted automatically.

The code replaces the shared configuration repository of the original private setup: no
account, no cloud service and no hand-edited inventory are involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import zlib
from dataclasses import asdict, dataclass, field

from .. import __version__
from . import events, inventory, paths
from .config import Config

PREFIX = "UW1"
_MAX_CODE = 4096


class PairingError(ValueError):
    pass


@dataclass
class Offer:
    name: str
    device_id: str = ""            # stable product identity (not secret)
    platform: str = ""
    version: str = ""
    syncthing_id: str = ""
    syncthing_port: int = 22000
    deskflow_fp: str = ""
    hosts: list[str] = field(default_factory=list)   # most specific first
    files: int = 0
    bytes: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def local_addresses() -> list[str]:
    """Private addresses this computer answers on. Never a public address."""
    import ipaddress

    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; this only selects the outgoing interface
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(entry[4][0])
    except OSError:
        pass
    out = []
    for address in found:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and address not in out:
            out.append(address)
    return out


def make_offer(cfg: Config) -> Offer:
    from ..integrations import peripherals, tailscale, worksync

    inst = worksync.instance(cfg)
    sync_id = inst.device_id() if inst.configured() else ""
    hosts: list[str] = []
    tail = tailscale.status()
    me = tail.get("self") or {}
    if tail.get("running") and me.get("dns"):
        hosts.append(str(me["dns"]).rstrip("."))
    hosts += local_addresses()
    listing = {"files": 0, "bytes": 0}
    base = worksync.root(cfg)
    if base.is_dir():
        found = worksync.scan(cfg, budget=5.0)
        listing = {"files": found.files, "bytes": found.bytes}
    try:
        port = int(str(cfg.get("work.listen", ["tcp://0.0.0.0:22000"])[0]).rsplit(":", 1)[1])
    except (ValueError, IndexError):
        port = 22000
    return Offer(
        name=cfg.device,
        device_id=str(cfg.get("device.id", "") or ""),
        platform=paths.platform(),
        version=__version__,
        syncthing_id=sync_id,
        syncthing_port=port,
        deskflow_fp=peripherals.fingerprint() if peripherals.enabled(cfg) else "",
        hosts=hosts[:4],
        files=listing["files"],
        bytes=listing["bytes"],
    )


def encode(offer: Offer) -> str:
    raw = zlib.compress(json.dumps(offer.as_dict(), separators=(",", ":"), sort_keys=True).encode(), 9)
    body = base64.b32encode(raw).decode().rstrip("=")
    check = hashlib.sha256(raw).hexdigest()[:6].upper()
    groups = "-".join(body[i : i + 5] for i in range(0, len(body), 5))
    return f"{PREFIX}-{check}-{groups}"


def decode(code: str) -> Offer:
    text = "".join(code.split()).upper()
    if len(text) > _MAX_CODE:
        raise PairingError("That is too long to be a pairing code.")
    parts = text.split("-")
    if len(parts) < 3 or parts[0] != PREFIX:
        raise PairingError("That is not a pairing code. Copy the whole code shown under “Add Workstation” on the other computer.")
    check, body = parts[1], "".join(parts[2:])
    try:
        raw = base64.b32decode(body + "=" * (-len(body) % 8))
    except (ValueError, TypeError) as exc:
        raise PairingError("The pairing code is damaged. Copy it again from the other computer.") from exc
    if hashlib.sha256(raw).hexdigest()[:6].upper() != check:
        raise PairingError("The pairing code is incomplete or was mistyped. Copy it again from the other computer.")
    try:
        data = json.loads(zlib.decompress(raw, bufsize=65536))
    except (zlib.error, ValueError) as exc:
        raise PairingError("The pairing code is damaged. Copy it again from the other computer.") from exc
    return validate(data)


def validate(data: dict) -> Offer:
    from ..integrations import peripherals, worksync

    if not isinstance(data, dict):
        raise PairingError("The pairing code is damaged.")
    name = str(data.get("name", ""))
    if not inventory.valid_label(name):
        raise PairingError("The other computer has a name this version can not use. Rename it there (Settings → Workstations) and create a new code.")
    sync_id = str(data.get("syncthing_id", ""))
    if sync_id and not worksync._DEVICE_ID.match(sync_id):
        raise PairingError("The pairing code holds an invalid sync identity.")
    fingerprint = str(data.get("deskflow_fp", ""))
    if fingerprint and not peripherals._FP.match(fingerprint):
        raise PairingError("The pairing code holds an invalid keyboard-sharing fingerprint.")
    hosts = [str(h) for h in data.get("hosts", []) if isinstance(h, str) and inventory.valid_host(h)][:4]
    try:
        port = int(data.get("syncthing_port", 22000))
        files, size = max(0, int(data.get("files", 0))), max(0, int(data.get("bytes", 0)))
    except (TypeError, ValueError) as exc:
        raise PairingError("The pairing code is damaged.") from exc
    if not 1 <= port <= 65535:
        raise PairingError("The pairing code holds an invalid port.")
    return Offer(name, str(data.get("device_id", ""))[:80], str(data.get("platform", ""))[:16], str(data.get("version", ""))[:32], sync_id, port, fingerprint, hosts, files, size)


def confirmation(mine: Offer, theirs: Offer) -> str:
    """The six digits both screens must show. Order-independent."""
    identities = sorted([mine.syncthing_id + "|" + mine.deskflow_fp, theirs.syncthing_id + "|" + theirs.deskflow_fp])
    digest = hashlib.sha256("\n".join(identities).encode()).digest()
    number = int.from_bytes(digest[:4], "big") % 1_000_000
    return f"{number:06d}"


def review(cfg: Config, theirs: Offer) -> dict:
    """Everything the user must see before trusting the other computer. Changes nothing."""
    mine = make_offer(cfg)
    problems = []
    if theirs.name == mine.name:
        problems.append("Both computers have the same name. Rename one of them first (Settings → Workstations).")
    if theirs.syncthing_id and theirs.syncthing_id == mine.syncthing_id:
        problems.append("This is this computer's own pairing code.")
    if not theirs.syncthing_id and not theirs.deskflow_fp:
        problems.append("The other computer has neither file sync nor keyboard sharing set up yet. Finish its setup, then create a new code there.")
    both_have_files = mine.files > 0 and theirs.files > 0
    return {
        "mine": mine.as_dict(),
        "theirs": theirs.as_dict(),
        "confirmation": confirmation(mine, theirs),
        "problems": problems,
        "sync": bool(theirs.syncthing_id and mine.syncthing_id),
        "peripherals": bool(theirs.deskflow_fp and mine.deskflow_fp),
        "both_have_files": both_have_files,
        "merge_note": (
            "Both folders already contain files. They will be merged: nothing is deleted, and a file that differs on the two computers is kept twice so you can choose."
            if both_have_files
            else "Files will be copied to the computer that does not have them yet. Nothing is deleted."
        ),
        "my_code": encode(mine),
    }


def accept(cfg: Config, theirs: Offer, *, sync: bool = True, share_input: bool = True) -> dict:
    """Trust the other computer. Called only after the user confirmed the number."""
    from ..integrations import peripherals, worksync

    inv = inventory.load()
    inventory.ensure_device(
        inv,
        theirs.name,
        role="workstation",
        platform=theirs.platform,
        id=theirs.device_id,
        host=theirs.hosts[0] if theirs.hosts else "",
        syncthing_id=theirs.syncthing_id,
        syncthing_port=str(theirs.syncthing_port),
        deskflow_fp=theirs.deskflow_fp,
    )
    if cfg.device not in inv["devices"]:
        inventory.ensure_device(inv, cfg.device, role="workstation", platform=paths.platform())
    inventory.save(inv)
    done = {"device": theirs.name, "sync": False, "peripherals": False}
    if sync and theirs.syncthing_id:
        worksync.approve(theirs.syncthing_id)
        if worksync.instance(cfg).configured():
            worksync.refresh(cfg)
        done["sync"] = True
    if share_input and theirs.deskflow_fp:
        peripherals.approve(theirs.deskflow_fp)
        done["peripherals"] = True
    events.emit("pairing.accepted", f"paired with {theirs.name}", device=theirs.name)
    return done


def unpair(cfg: Config, name: str) -> bool:
    """Stop trusting a computer. Its files stay where they are on both sides."""
    from ..integrations import peripherals, worksync

    inv = inventory.load()
    device = inv["devices"].get(name)
    if not device or name == cfg.device:
        return False
    if device.get("syncthing_id"):
        worksync.revoke(str(device["syncthing_id"]))
    if device.get("deskflow_fp"):
        peripherals.revoke(str(device["deskflow_fp"]))
    del inv["devices"][name]
    inventory.save(inv)
    if worksync.instance(cfg).configured():
        worksync.refresh(cfg)
    events.emit("pairing.removed", f"unpaired {name}", device=name)
    return True


def rename(cfg: Config, old: str, new: str) -> None:
    from . import config as configmod

    if not inventory.valid_label(new):
        raise PairingError("Use lowercase letters, digits and dashes for a computer name (for example: studio-laptop).")
    inv = inventory.load()
    if new in inv["devices"]:
        raise PairingError(f"A computer named “{new}” already exists.")
    if old == cfg.device:
        configmod.set_value("device.name", new, "local")
    if old in inv["devices"]:
        inv["devices"][new] = {**inv["devices"].pop(old), "logical_name": new}
        inventory.save(inv)
    events.emit("device.renamed", f"{old} is now {new}")
