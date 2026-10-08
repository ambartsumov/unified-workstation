"""The home server as a permanent third copy of the shared work folder.

Ubuntu and the MacBook are equal, interactive peers. Home is different on purpose:
  * it only receives (Syncthing `receiveonly`): nothing on the server can originate a change
    or win a conflict against a workstation;
  * it still hands what it holds to a workstation that was away, so the two workstations do
    not have to be awake at the same time;
  * it keeps the last `work.replica_keep` replaced or deleted versions of every file for
    `work.replica_days` — the
    recovery copy that plain synchronisation cannot be (a deletion syncs, a version stays).

Everything on the server is done over the pinned `ssh home` alias with one fixed script.
The server's Syncthing identity is accepted because it arrives over that verified channel,
at the explicit request of the person running `suw home replica setup`.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

from ..core import health, inventory, paths
from ..core.config import Config
from ..core.proc import ssh_cmd
from . import worksync

SCRIPT = Path(__file__).resolve().parents[1] / "cloud" / "scripts" / "work_replica.sh"
_FOLDER = re.compile(r"^(~/|/)[A-Za-z0-9._/-]+$")
_STAMP = re.compile(r"~\d{8}-\d{6}")
_TAILNET = re.compile(r"^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
ALIAS = "home"
RELEASE = "v2.1.6"  # the official build `setup --install` puts into the server user's ~/.local/bin


class ReplicaError(RuntimeError):
    pass


def folder(cfg: Config) -> str:
    value = str(cfg.get("work.replica_path", "~/work"))
    if not _FOLDER.match(value) or ".." in value.split("/"):
        raise ReplicaError(f"work.replica_path '{value}' is not a plain path")
    return value


def port(cfg: Config) -> int:
    return int(cfg.get("work.replica_port", 22000))


def remote(action: str, where: str, *extra: str, timeout: float = 60):
    return health.ssh(ALIAS, ["sh", "-s", "--", action, shlex.quote(where), *(shlex.quote(e) for e in extra)], stdin=SCRIPT.read_text(), timeout=timeout)


def fields(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line == "---config---":
            break
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", key):
            out[key] = value.strip()
    return out


def configured(inv: dict | None = None) -> bool:
    inv = inv or inventory.load()
    return bool(inv["devices"].get(ALIAS, {}).get("syncthing_id"))


def shaped(cfg: Config, config_xml: str, home_id: str, where: str, peers: list[worksync.Peer], bind: str = "") -> str:
    """The replica's configuration: receive-only, dated versions, the workstations as peers. Pure.

    `bind` is the server's tailnet address. With it the replica is reachable from the tailnet
    only; without it (no Tailscale on the server) it listens on every interface, where the
    device-ID check is the only gate — `setup` says so."""
    listen = port(cfg)
    host = bind if _TAILNET.match(bind) else "0.0.0.0"
    inst = worksync.Instance(Path("/nonexistent"), PurePosixPath(where), gui="127.0.0.1:8385", listen=[f"tcp://{host}:{listen}", f"quic://{host}:{listen}"], name=ALIAS, local_discovery=False)
    return inst.shape_text(config_xml, home_id, peers, replica_days=max(1, int(cfg.get("work.replica_days", 365))), replica_keep=max(1, int(cfg.get("work.replica_keep", 20))))


def workstation_peers(cfg: Config, inv: dict) -> list[worksync.Peer]:
    """This workstation plus every workstation approved here: the only devices Home will talk to."""
    mine = worksync.instance(cfg).device_id()
    host = inventory.device_host(inv["devices"].get(cfg.device, {}))
    out = [worksync.Peer(cfg.device, mine, ([f"tcp://{host}:22000"] if host else []) + ["dynamic"])] if mine else []
    return out + [peer for peer in worksync.peers(cfg, inv) if peer.name != ALIAS]


def setup(cfg: Config, install: bool = False) -> dict:
    """Create or refresh the replica. Idempotent; safe to re-run after pairing another workstation.

    `install` lets a server without Syncthing get the official release in the SSH user's own
    home (checksum verified, no administrator rights). It is never done unasked."""
    inv = inventory.load()
    if not inventory.device_host(inv["devices"].get(ALIAS, {})):
        raise ReplicaError("the home server is not configured (suw home set <name>)")
    peers = workstation_peers(cfg, inv)
    if not peers:
        raise ReplicaError("the work folder is not set up on this workstation yet (suw work setup)")
    where = folder(cfg)
    first = remote("prepare", where, timeout=90)
    info = fields(first.out)
    if info.get("missing") == "syncthing" and install:
        got = remote("install", where, RELEASE, timeout=330)
        if fields(got.out).get("installed") != "yes":
            raise ReplicaError("Syncthing could not be installed on the home server: " + (got.err.strip().splitlines() or ["no answer"])[-1].removeprefix("error: ")[:200])
        first = remote("prepare", where, timeout=90)
        info = fields(first.out)
    if info.get("missing") == "syncthing":
        raise ReplicaError("Syncthing is not installed on the home server. Either there, once:  sudo apt install syncthing\n  or from here, without administrator rights:  suw home replica setup --install")
    if not first.ok or "---config---" not in first.out or not info.get("id"):
        raise ReplicaError((first.err.strip().splitlines() or ["the home server did not answer"])[-1][:200])
    home_id, absolute = info["id"], info.get("folder", "")
    if not re.fullmatch(r"[A-Z0-9-]{50,70}", home_id) or not absolute.startswith("/"):
        raise ReplicaError("the home server returned an unexpected answer")
    config_xml = first.out.split("---config---\n", 1)[1]
    bind = info.get("tailnet", "") if _TAILNET.match(info.get("tailnet", "")) else ""
    wanted = shaped(cfg, config_xml, home_id, absolute, peers, bind)
    put = health.ssh(
        ALIAS,
        ["sh", "-c", shlex.quote('umask 077; f="$HOME/.local/state/suw/syncthing/config.xml"; cat > "$f.suw-new" && { [ ! -f "$f" ] || cp -p "$f" "$f.suw-prev"; } && mv -f "$f.suw-new" "$f"')],
        stdin=wanted,
        timeout=30,
    )
    if not put.ok:
        raise ReplicaError("could not write the replica's configuration: " + (put.err.strip().splitlines() or ["?"])[-1][:160])
    started = fields(remote("start", where, timeout=60).out)
    if started.get("active") != "active":
        raise ReplicaError("the replica service did not start on the home server (ssh home systemctl --user status suw-syncthing)")
    inventory.ensure_device(inv, ALIAS, role="home", syncthing_id=home_id, syncthing_port=str(port(cfg)))
    inventory.save(inv)
    worksync.approve(home_id)
    worksync.refresh(cfg)
    return {"id": home_id, "folder": absolute, "peers": [p.name for p in peers], "linger": started.get("linger", "unknown"), "private": bool(bind)}


def status(cfg: Config) -> dict:
    """What the server says about itself. The sync progress comes from the local instance's
    view of the peer named 'home' (`suw work status`), not from here."""
    if not configured():
        return {"state": "NOT_CONFIGURED"}
    res = remote("status", folder(cfg), timeout=25)
    if not res.ok:
        return {"state": health.failure_status(res.err), "error": (res.err.strip().splitlines() or ["unreachable"])[-1][:160]}
    info = fields(res.out)
    return {
        "state": "ONLINE" if info.get("active") == "active" else "STOPPED",
        "linger": info.get("linger", "unknown"),
        "free_bytes": int(info.get("free_kb", "0") or 0) * 1024,
        "versions": int(info.get("versions", "0") or 0),
    }


def remove(cfg: Config) -> bool:
    """Stop replicating to Home. The files already there are kept; nothing is deleted."""
    inv = inventory.load()
    device = inv["devices"].get(ALIAS, {})
    home_id = device.pop("syncthing_id", "")
    device.pop("syncthing_port", None)
    inventory.save(inv)
    if home_id:
        worksync.revoke(home_id)
        worksync.refresh(cfg)
    return remote("stop", folder(cfg), timeout=30).ok


def original_of(rel: str) -> str:
    """`notes~20261008-150102.md` → `notes.md` (how Syncthing names a dated version)."""
    return _STAMP.sub("", rel)


def parse_versions(text: str, prefix: str = "") -> list[dict]:
    out = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        stamp, size, rel = parts
        if rel.startswith("/") or ".." in rel.split("/") or (prefix and not rel.startswith(prefix)):
            continue
        try:
            out.append({"path": rel, "original": original_of(rel), "bytes": int(size), "mtime": float(stamp)})
        except ValueError:
            continue
    return out


def versions(cfg: Config, prefix: str = "") -> list[dict]:
    res = remote("versions", folder(cfg), timeout=60)
    if not res.ok:
        raise ReplicaError((res.err.strip().splitlines() or ["the home server did not answer"])[-1][:200])
    return parse_versions(res.out, prefix)


def restore(cfg: Config, rel: str) -> str:
    """Bring one dated version back from Home into the work folder. Nothing is overwritten:
    when a file of that name exists, the version arrives next to it under a `.restored-…` name."""
    known = {row["path"] for row in versions(cfg)}
    if rel not in known:
        raise ReplicaError(f"no such saved version on the home server: {rel}")
    base = worksync.root(cfg)
    target = base / original_of(rel)
    if target.exists():
        target = target.with_name(f"{target.stem}.restored-{time.strftime('%Y%m%d-%H%M%S')}{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    remote_path = ('"$HOME"/' + shlex.quote(folder(cfg)[2:].rstrip("/") + "/.stversions/" + rel)) if folder(cfg).startswith("~/") else shlex.quote(folder(cfg).rstrip("/") + "/.stversions/" + rel)
    part = paths.ensure(paths.state_dir() / "tmp") / f"restore-{time.time_ns()}.part"  # outside the shared folder
    try:
        with part.open("wb") as handle:
            done = subprocess.run([*ssh_cmd(), *health.SSH_OPTS, ALIAS, "--", "cat", "--", remote_path], stdout=handle, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=3600)
        if done.returncode != 0:
            raise ReplicaError("could not fetch the version: " + (done.stderr.decode(errors="replace").strip().splitlines() or ["?"])[-1][:160])
        shutil.move(str(part), str(target))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReplicaError(f"could not fetch the version: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)
    return str(target)
