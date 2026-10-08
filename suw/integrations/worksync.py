"""The shared workspace: one directory that is the same on every workstation.

Rule: *inside the work directory = shared, outside = this machine only.*

The transport is Syncthing — a private instance owned by SUW (its own home directory, its own
loopback-only API port), so a Syncthing the user may run for other things is never touched.
SUW does three things on top of it:
  * writes the configuration (one folder, the paired workstations, trash-can versioning);
  * owns a managed block in `.stignore` — only things that are unsafe or pointless to copy
    (live `.git` databases, virtualenvs, caches, OS droppings) plus files above the size limit;
  * reads the state back so `suw work status` can say, in words, what is and is not shared.

Nothing here deletes or overwrites user files. Conflict copies are never removed: "resolving"
one moves the losing version into a trash directory in the SUW state directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from ..core import config as configmod
from ..core import inventory, paths, state, supervise
from ..core.config import Config
from ..core.proc import run

FOLDER_ID = "suw-work"
BEGIN = "// >>> suw managed block (suw work ignored) — edit [work] in the SUW config, not here >>>"
END = "// <<< suw managed block <<<"
CONFLICT_MARK = ".sync-conflict-"
VERSIONS_DIR = ".stversions"
SERVICE = "suw-syncthing.service"
LABEL = "io.github.ambartsumov.syncthing"

# Names that are never copied, wherever they appear. Each has a reason the user can read.
DEFAULT_IGNORE: dict[str, str] = {
    ".git": "Git database: history travels through Git/GitHub, never as copied files",
    "node_modules": "installed dependencies: rebuilt per machine (npm install)",
    ".venv": "Python virtualenv: contains absolute paths, rebuilt per machine",
    "venv": "Python virtualenv: contains absolute paths, rebuilt per machine",
    "__pycache__": "Python bytecode cache",
    ".mypy_cache": "tool cache",
    ".pytest_cache": "tool cache",
    ".ruff_cache": "tool cache",
    ".tox": "tool cache",
    ".ipynb_checkpoints": "notebook autosave cache",
    ".next": "build cache",
    ".turbo": "build cache",
    ".parcel-cache": "build cache",
    ".gradle": "build cache",
    ".DS_Store": "macOS folder metadata",
    "._*": "macOS resource fork",
    ".Spotlight-V100": "macOS index",
    ".fseventsd": "macOS index",
    ".Trash-*": "desktop trash",
    "Thumbs.db": "Windows thumbnail cache",
    "*.swp": "editor swap file",
    "*.swx": "editor swap file",
    ".#*": "editor lock file",
    "*.crdownload": "unfinished download",
    "*.part": "unfinished download",
}
OWN = {VERSIONS_DIR, ".stfolder", ".stignore"}


# ── small helpers ───────────────────────────────────────────────────────────


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("work.enabled", True))


def root(cfg: Config) -> Path:
    return paths.expand(str(cfg.get("work.path", "~/Desktop/Work")))


def binary() -> str:
    found = shutil.which("syncthing")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/syncthing", "/usr/local/bin/syncthing", str(paths.home() / ".local/bin/syncthing")):
        if os.access(candidate, os.X_OK):
            return candidate
    return ""


def ignore_rules(cfg: Config) -> dict[str, str]:
    rules = dict(DEFAULT_IGNORE)
    for name in cfg.get("work.sync_anyway", []):
        rules.pop(str(name), None)
    for name in cfg.get("work.ignore", []):
        rules.setdefault(str(name), "listed in work.ignore")
    return rules


def escape(pattern: str) -> str:
    """A literal path as a Syncthing ignore pattern."""
    return "".join("\\" + ch if ch in "*?[]{}\\" else ch for ch in pattern)


def _matches(name: str, rules: dict[str, str]) -> str:
    for pattern, reason in rules.items():
        if name == pattern or (any(ch in pattern for ch in "*?") and fnmatch(name, pattern)):
            return reason
    return ""


# ── what is in the folder ───────────────────────────────────────────────────


@dataclass
class Scan:
    files: int = 0
    bytes: int = 0
    ignored: list[dict] = field(default_factory=list)  # {"path", "reason"}
    large: list[dict] = field(default_factory=list)  # {"path", "mb"}
    unsupported: list[dict] = field(default_factory=list)  # {"path", "reason"}
    conflicts: list[dict] = field(default_factory=list)  # {"path", "original", "mtime"}
    repos: list[str] = field(default_factory=list)
    partial: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def scan(cfg: Config, base: Path | None = None, budget: float = 20.0) -> Scan:
    """One walk of the work directory. Ignored directories are reported, not entered."""
    base = base or root(cfg)
    rules = ignore_rules(cfg)
    limit = float(cfg.get("work.large_file_mb", 2048)) * 1024 * 1024
    found = Scan()
    deadline = time.monotonic() + budget
    stack = [base]
    while stack:
        if time.monotonic() > deadline:
            found.partial = True
            break
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        seen_lower: dict[str, str] = {}
        for entry in entries:
            rel = os.path.relpath(entry.path, base)
            if current == base and entry.name in OWN:
                continue
            reason = _matches(entry.name, rules)
            if reason:
                if entry.name == ".git":
                    found.repos.append(os.path.relpath(current, base))
                found.ignored.append({"path": rel, "reason": reason})
                continue
            clash = seen_lower.setdefault(entry.name.lower(), entry.name)
            if clash != entry.name:
                found.unsupported.append({"path": rel, "reason": f"differs from '{clash}' only by letter case: macOS cannot hold both"})
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                found.files += 1
                found.bytes += info.st_size
                if CONFLICT_MARK in entry.name:
                    found.conflicts.append({"path": rel, "original": original_of(rel), "mtime": info.st_mtime})
                if limit and info.st_size > limit:
                    found.large.append({"path": rel, "mb": round(info.st_size / 1048576)})
            elif not stat.S_ISLNK(info.st_mode):
                kind = "socket" if stat.S_ISSOCK(info.st_mode) else "named pipe" if stat.S_ISFIFO(info.st_mode) else "device file"
                found.unsupported.append({"path": rel, "reason": f"{kind}: not a file that can be copied"})
    return found


def original_of(conflict: str) -> str:
    """`notes.sync-conflict-20260101-120000-ABCDEFG.md` -> `notes.md`."""
    head, _, tail = conflict.partition(CONFLICT_MARK)
    ext = tail[tail.index(".") :] if "." in tail else ""
    return head + ext


def held_large(cfg: Config, found: Scan) -> list[str]:
    """Files above the limit are held back unless the user said to sync them anyway."""
    if str(cfg.get("work.large_file_policy", "hold")) != "hold":
        return []
    allowed = {str(p) for p in cfg.get("work.large_allow", [])}
    return [item["path"] for item in found.large if item["path"] not in allowed]


def render_ignore(cfg: Config, held: list[str]) -> str:
    lines = [BEGIN, "// Not copied between workstations. Everything else in this folder is shared."]
    for pattern in ignore_rules(cfg):
        lines.append(f"(?d){pattern}")
    if held:
        lines.append("// Larger than work.large_file_mb — held back (suw work status explains; suw work allow <path>):")
        lines += ["/" + escape(path) for path in sorted(held)]
    lines.append(END)
    return "\n".join(lines) + "\n"


def write_ignore(cfg: Config, held: list[str], base: Path | None = None) -> bool:
    """Replace only the managed block; whatever the user wrote around it stays."""
    file = (base or root(cfg)) / ".stignore"
    block = render_ignore(cfg, held)
    try:
        old = file.read_text()
    except OSError:
        old = ""
    if BEGIN in old and END in old:
        head, rest = old.split(BEGIN, 1)
        new = head + block.rstrip("\n") + rest.split(END, 1)[1]
    else:
        new = block + ("\n" + old if old.strip() else "\n// Your own patterns go below this line.\n")
    if new == old:
        return False
    tmp = file.with_name(".stignore.suw-tmp")
    tmp.write_text(new)
    os.replace(tmp, file)
    return True


# ── first pairing: look before joining two folders ──────────────────────────


def manifest(base: Path, cfg: Config, cap: int = 60000) -> dict:
    """A compact description of the shared part of the folder: path -> size and content hash."""
    rules = ignore_rules(cfg)
    entries: dict[str, str] = {}
    total = 0
    stack = [base]
    while stack and len(entries) < cap:
        current = stack.pop()
        try:
            listing = list(os.scandir(current))
        except OSError:
            continue
        for entry in listing:
            if _matches(entry.name, rules) or (current == base and entry.name in OWN):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                digest = ""
                if info.st_size <= 8 * 1048576:
                    try:
                        digest = hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()[:16]
                    except OSError:
                        digest = ""
                entries[os.path.relpath(entry.path, base)] = f"{info.st_size}:{digest}"
    return {"generated": time.time(), "files": len(entries), "bytes": total, "truncated": bool(stack), "entries": entries}


def compare(mine: dict, theirs: dict) -> dict:
    """What joining the two folders would do. Nothing is ever deleted by a first join."""
    a, b = mine.get("entries", {}), theirs.get("entries", {})
    both = set(a) & set(b)
    differing = sorted(p for p in both if a[p] != b[p] or a[p].endswith(":"))
    return {
        "only_here": len(set(a) - set(b)),
        "only_there": len(set(b) - set(a)),
        "identical": len(both) - len(differing),
        "differing": differing,
    }


def manifest_file(device: str) -> Path:
    return paths.shared_dir() / "work" / f"{device}.manifest.json"


def publish_manifest(cfg: Config) -> dict:
    data = manifest(root(cfg), cfg)
    file = manifest_file(cfg.device)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))
    os.replace(tmp, file)
    return data


def peer_manifest(device: str) -> dict | None:
    try:
        return json.loads(manifest_file(device).read_text())
    except (OSError, ValueError):
        return None


# ── the Syncthing instance ──────────────────────────────────────────────────


@dataclass
class Peer:
    name: str
    device_id: str
    addresses: list[str] = field(default_factory=lambda: ["dynamic"])


_DEVICE_ID = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")


class Instance:
    """One Syncthing home directory and the folder it shares."""

    def __init__(self, home: Path, folder: Path, gui: str = "127.0.0.1:8385", listen: list[str] | None = None, name: str = "", local_discovery: bool = True, exe: str = ""):
        self.home, self.folder, self.gui, self.name = Path(home), Path(folder), gui, name
        self.exe = exe  # a specific Syncthing binary; the installed one when empty
        self.reconnect_s = 60  # how often a lost peer is dialled again (Syncthing's own default)
        self.listen = listen or ["tcp://0.0.0.0:22000", "quic://0.0.0.0:22000"]
        self.local_discovery = local_discovery

    @property
    def config_file(self) -> Path:
        return self.home / "config.xml"

    def configured(self) -> bool:
        return self.config_file.exists()

    def generate(self) -> bool:
        """Create the key pair and a bare configuration. Returns True if it did anything."""
        if self.configured():
            return False
        paths.ensure(self.home)
        # Only what Syncthing 1.x and 2.x both accept (2.x renamed or dropped the other
        # flags). A 1.x default folder is suppressed through the environment; ports and
        # folders are written by `shape` anyway.
        res = run([(self.exe or binary()), "generate", f"--home={self.home}"], env={"STNODEFAULTFOLDER": "1"}, timeout=60)
        if not res.ok or not self.configured():
            raise RuntimeError(f"syncthing could not create its configuration: {res.text[-200:]}")
        self.config_file.chmod(0o600)
        return True

    def _tree(self) -> ET.ElementTree:
        return ET.parse(self.config_file)

    def device_id(self) -> str:
        if not self.configured():
            return ""
        res = run([(self.exe or binary()), "device-id", f"--home={self.home}"], timeout=20)  # Syncthing 2.x
        if not res.ok or not _DEVICE_ID.match(res.out.strip()):
            res = run([(self.exe or binary()), f"--home={self.home}", "--device-id"], timeout=20)  # Syncthing 1.x
        return res.out.strip() if res.ok and _DEVICE_ID.match(res.out.strip()) else ""

    def api_key(self) -> str:
        try:
            return self._tree().getroot().findtext("gui/apikey") or ""
        except (OSError, ET.ParseError):
            return ""

    def apply(self, peers: list[Peer], *, trash_days: int = 30, paused: bool | None = None) -> bool:
        """Write the folder, the peers and the privacy options. Returns True when the file changed."""
        tree = self._tree()
        top = tree.getroot()
        before = ET.tostring(top)
        self.shape(top, self.device_id(), peers, trash_days=trash_days, paused=paused)
        if ET.tostring(top) == before:
            return False
        tmp = self.config_file.with_suffix(".xml.suw-tmp")
        tree.write(tmp, encoding="unicode")
        tmp.chmod(0o600)
        os.replace(tmp, self.config_file)
        return True

    def shape_text(self, xml: str, me: str, peers: list[Peer], **options) -> str:
        """`shape` for a configuration that lives on another machine (the home replica)."""
        top = ET.fromstring(xml)
        self.shape(top, me, peers, **options)
        return ET.tostring(top, encoding="unicode")

    def shape(self, top: ET.Element, me: str, peers: list[Peer], *, trash_days: int = 30, paused: bool | None = None, replica_days: int = 0, replica_keep: int = 20) -> None:
        """The folder, the peers and the privacy options, written into a parsed configuration.

        The instance never announces itself to public discovery servers, never uses public
        relays and never opens a port through the router: machines find each other on the
        LAN or by the stable (tailnet) name stored in the inventory.

        `replica_days` > 0 makes this a storage replica (the home server): it only receives,
        never originates a change, and keeps the last `replica_keep` replaced or deleted versions
        of every file for that many days.
        """

        def setting(parent: ET.Element, tag: str, values: list[str]) -> None:
            for old in parent.findall(tag):
                parent.remove(old)
            for value in values:
                ET.SubElement(parent, tag).text = value

        gui = top.find("gui")
        setting(gui, "address", [self.gui])
        gui.set("tls", "false")
        options = top.find("options")
        setting(options, "listenAddress", self.listen)
        for tag, value in (
            ("globalAnnounceEnabled", "false"),
            ("relaysEnabled", "false"),
            ("natEnabled", "false"),
            ("localAnnounceEnabled", "true" if self.local_discovery else "false"),
            ("startBrowser", "false"),
            ("urAccepted", "-1"),
            ("autoUpgradeIntervalH", "0"),
            ("crashReportingEnabled", "false"),
            ("reconnectionIntervalS", str(self.reconnect_s)),
        ):
            setting(options, tag, [value])

        wanted = {peer.device_id: peer for peer in peers if peer.device_id and peer.device_id != me}
        for device in top.findall("device"):
            if device.get("id") != me and device.get("id") not in wanted:
                top.remove(device)
        known = {device.get("id"): device for device in top.findall("device")}
        if me in known and self.name:
            known[me].set("name", self.name)
        for device_id, peer in wanted.items():
            device = known.get(device_id)
            if device is None:
                device = ET.Element("device", {"id": device_id, "compression": "metadata", "introducer": "false"})
                top.insert(list(top).index(gui), device)
            device.set("name", peer.name)
            setting(device, "address", peer.addresses)
            setting(device, "autoAcceptFolders", ["false"])

        folder = next((f for f in top.findall("folder") if f.get("id") == FOLDER_ID), None)
        if folder is None:
            folder = ET.Element("folder", {"id": FOLDER_ID})
            top.insert(0, folder)
        folder.attrib.update(
            {
                "label": "Work",
                "path": str(self.folder),
                "type": "receiveonly" if replica_days else "sendreceive",
                "rescanIntervalS": "3600",
                "fsWatcherEnabled": "true",
                "fsWatcherDelayS": "10",
                "ignorePerms": "false",
                "autoNormalize": "true",
            }
        )
        for old in folder.findall("device"):
            folder.remove(old)
        for device_id in [me, *wanted]:
            ET.SubElement(folder, "device", {"id": device_id, "introducedBy": ""})
        for old in folder.findall("versioning"):
            folder.remove(old)
        if replica_days:
            # "simple", not "staggered": staggered thins to one version per 30 s and keeps the
            # OLDER of two close ones, so a file edited and then deleted within half a minute
            # would lose exactly the content that was deleted.
            versioning = ET.SubElement(folder, "versioning", {"type": "simple"})
            ET.SubElement(versioning, "param", {"key": "keep", "val": str(replica_keep)})
            ET.SubElement(versioning, "param", {"key": "cleanoutDays", "val": str(replica_days)})
        else:
            versioning = ET.SubElement(folder, "versioning", {"type": "trashcan"})
            ET.SubElement(versioning, "param", {"key": "cleanoutDays", "val": str(trash_days)})
        setting(versioning, "cleanupIntervalS", ["3600"])
        setting(folder, "maxConflicts", ["-1"])  # keep every conflict copy
        setting(folder, "ignoreDelete", ["false"])
        if paused is None:
            paused = (folder.findtext("paused") or "false") == "true"
        setting(folder, "paused", ["true" if paused else "false"])
        for other in top.findall("folder"):
            if other.get("id") != FOLDER_ID:
                top.remove(other)  # this instance exists for one folder only

    def serve_command(self) -> list[str]:
        return [(self.exe or binary()), "serve", "--no-browser", "--no-restart", "--no-upgrade", f"--home={self.home}"]

    # REST (loopback only, API key from the 0600 config file)

    def api(self, method: str, path: str, body: dict | None = None, timeout: float = 5.0):
        key = self.api_key()
        if not key:
            return None
        request = urllib.request.Request(
            f"http://{self.gui}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as reply:
                raw = reply.read()
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def running(self) -> bool:
        status = self.api("GET", "/rest/system/status", timeout=2.0)
        return bool(status and status.get("myID"))

    def wait_running(self, seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.running():
                return True
            time.sleep(0.25)
        return False

    def set_paused(self, paused: bool) -> bool:
        return self.api("PATCH", f"/rest/config/folders/{FOLDER_ID}", {"paused": paused}) is not None

    def rescan(self) -> bool:
        return self.api("POST", f"/rest/db/scan?folder={FOLDER_ID}") is not None

    def snapshot(self) -> dict:
        """The live state, flattened. `state` is one word a person can act on."""
        out: dict = {"running": False, "state": "STOPPED", "peers": [], "need_items": 0, "need_bytes": 0, "errors": [], "paused": False}
        if not self.configured():
            out["state"] = "NOT_CONFIGURED"
            return out
        system = self.api("GET", "/rest/system/status", timeout=2.0)
        if not system or not system.get("myID"):
            return out
        out.update(running=True, device_id=system["myID"])
        conf = self.api("GET", "/rest/config") or {}
        folder = next((f for f in conf.get("folders", []) if f.get("id") == FOLDER_ID), None)
        if folder is None:
            out["state"] = "NOT_CONFIGURED"
            return out
        out["paused"] = bool(folder.get("paused"))
        db = self.api("GET", f"/rest/db/status?folder={FOLDER_ID}", timeout=15) or {}
        out["folder_state"] = db.get("state", "")
        out["need_items"] = int(db.get("needFiles", 0)) + int(db.get("needDirectories", 0)) + int(db.get("needSymlinks", 0)) + int(db.get("needDeletes", 0))
        out["need_bytes"] = int(db.get("needBytes", 0))
        out["local_files"] = int(db.get("localFiles", 0))
        if db.get("error"):
            out["errors"].append({"path": "", "error": str(db["error"])})
        if int(db.get("pullErrors", 0)):
            failed = self.api("GET", f"/rest/folder/errors?folder={FOLDER_ID}") or {}
            out["errors"] += [{"path": e.get("path", ""), "error": e.get("error", "")} for e in (failed.get("errors") or [])[:50]]
        connections = (self.api("GET", "/rest/system/connections") or {}).get("connections", {})
        seen = self.api("GET", "/rest/stats/device") or {}
        names = {d.get("deviceID"): d for d in conf.get("devices", [])}
        outgoing = 0
        for entry in folder.get("devices", []):
            device_id = entry.get("deviceID")
            if device_id == system["myID"]:
                continue
            link = connections.get(device_id, {})
            done = self.api("GET", f"/rest/db/completion?folder={FOLDER_ID}&device={device_id}") or {}
            need = int(done.get("needItems", 0)) + int(done.get("needDeletes", 0))
            outgoing += need if link.get("connected") else 0
            out["peers"].append(
                {
                    "name": names.get(device_id, {}).get("name", device_id[:7]),
                    "id": device_id,
                    "connected": bool(link.get("connected")),
                    "paused": bool(names.get(device_id, {}).get("paused")),
                    "completion": round(float(done.get("completion", 0)), 1),
                    "need_items": need,
                    "last_seen": (seen.get(device_id) or {}).get("lastSeen", ""),
                }
            )
        out["outgoing_items"] = outgoing
        online = [p for p in out["peers"] if p["connected"]]
        if out["paused"]:
            word = "PAUSED"
        elif out["errors"] or out["folder_state"] == "error":
            word = "ERROR"
        elif not out["peers"]:
            word = "NO_PEER"
        elif out["folder_state"] in ("scanning", "scan-waiting", "syncing", "sync-waiting", "sync-preparing", "cleaning", "clean-waiting") or out["need_items"] or outgoing:
            word = "SYNCING"
        elif not online:
            word = "PEER_OFFLINE"
        else:
            word = "IN_SYNC"
        out["state"] = word
        return out


# ── the instance described by the configuration ─────────────────────────────


def home_dir() -> Path:
    return paths.state_dir() / "syncthing"


def instance(cfg: Config) -> Instance:
    return Instance(
        home_dir(),
        root(cfg),
        gui=str(cfg.get("work.api_address", "127.0.0.1:8385")),
        listen=[str(a) for a in cfg.get("work.listen", ["tcp://0.0.0.0:22000", "quic://0.0.0.0:22000"])],
        name=cfg.device,
    )


def peers(cfg: Config, inv: dict | None = None) -> list[Peer]:
    """Other workstations that published a Syncthing identity AND were approved here."""
    inv = inv or inventory.load()
    approved = set(state.load("work").get("paired", []))
    out = []
    for name, device in inv["devices"].items():
        device_id = str(device.get("syncthing_id", ""))
        if name == cfg.device or not device_id or device_id not in approved:
            continue
        host = inventory.device_host(device)
        port = int(device.get("syncthing_port", 22000))
        out.append(Peer(name, device_id, ([f"tcp://{host}:{port}"] if host else []) + ["dynamic"]))
    return out


def unpaired(cfg: Config, inv: dict | None = None) -> list[tuple[str, str]]:
    """Workstations in the inventory that offer a Syncthing identity not yet approved here."""
    inv = inv or inventory.load()
    approved = set(state.load("work").get("paired", []))
    return [
        (name, str(device["syncthing_id"]))
        for name, device in inv["devices"].items()
        if name != cfg.device and device.get("syncthing_id") and device["syncthing_id"] not in approved
    ]


def approve(device_id: str) -> None:
    data = state.load("work")
    data["paired"] = sorted({*data.get("paired", []), device_id})
    state.save("work", data)


def revoke(device_id: str) -> None:
    data = state.load("work")
    data["paired"] = [d for d in data.get("paired", []) if d != device_id]
    state.save("work", data)


def service_running() -> bool:
    if supervise.needed():
        return supervise.running("syncthing")
    if paths.platform() == "macos":
        return run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], timeout=10).ok
    return run(["systemctl", "--user", "is-active", SERVICE], timeout=10).out.strip() == "active"


def service(action: str) -> bool:
    """start | stop | restart the private Syncthing instance through the session manager."""
    if supervise.needed():
        if action in ("stop", "restart"):
            supervise.stop("syncthing", forget=action == "stop")
        if action in ("start", "restart"):
            exe = binary()
            return bool(exe) and supervise.start("syncthing", [exe, "serve", "--no-browser", "--no-restart", "--no-upgrade", f"--home={home_dir()}"], {"STNODEFAULTFOLDER": "1"})
        return True
    if paths.platform() == "macos":
        target = f"gui/{os.getuid()}/{LABEL}"
        plist = paths.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if action in ("stop", "restart"):
            run(["launchctl", "bootout", target], timeout=15)
        if action in ("start", "restart"):
            return run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], timeout=15).ok or service_running()
        return True
    return run(["systemctl", "--user", action, SERVICE], timeout=30).ok


def refresh(cfg: Config, *, rescan: bool = True) -> dict:
    """Bring ignore rules and configuration in line with the SUW config. Idempotent."""
    inst = instance(cfg)
    found = scan(cfg)
    held = held_large(cfg, found)
    changed_ignore = write_ignore(cfg, held) if root(cfg).is_dir() else False
    changed_config = inst.apply(peers(cfg), trash_days=int(cfg.get("work.trash_days", 30))) if inst.configured() else False
    if changed_config and service_running():
        service("restart")
        inst.wait_running(30)
    elif changed_ignore and rescan:
        inst.rescan()
    return {"ignore_changed": changed_ignore, "config_changed": changed_config, "held": held, "scan": found}


def status(cfg: Config, *, deep: bool = True) -> dict:
    """Everything `suw work status` shows, as data."""
    base = root(cfg)
    out: dict = {"enabled": enabled(cfg), "path": str(base), "exists": base.is_dir(), "installed": bool(binary())}
    if not out["enabled"]:
        return {**out, "state": "DISABLED", "peers": []}
    if not out["installed"]:
        return {**out, "state": "NOT_INSTALLED", "peers": []}
    out.update(instance(cfg).snapshot())
    if not out["exists"] and out["state"] not in ("NOT_CONFIGURED",):
        out["state"] = "ERROR"
        out["errors"] = [{"path": "", "error": f"{base} does not exist"}]
    memory = state.load("work")
    if out["state"] == "IN_SYNC":
        memory["last_sync"] = time.time()
        state.save("work", memory)
    out["last_sync"] = memory.get("last_sync", 0)
    out["unpaired"] = [name for name, _ in unpaired(cfg)]
    if deep and out["exists"]:
        found = scan(cfg)
        held = set(held_large(cfg, found))
        out.update(
            files=found.files,
            bytes=found.bytes,
            conflicts=found.conflicts,
            ignored=found.ignored,
            large=[{**item, "held": item["path"] in held} for item in found.large],
            unsupported=found.unsupported,
            repos=found.repos,
            partial=found.partial,
        )
        if found.conflicts and out["state"] in ("IN_SYNC", "PEER_OFFLINE", "NO_PEER", "SYNCING"):
            out["state"] = "CONFLICTS"
    return out


WORDS = {
    "DISABLED": "switched off (work.enabled = false)",
    "NOT_INSTALLED": "Syncthing is not installed",
    "NOT_CONFIGURED": "not set up yet",
    "STOPPED": "the sync service is not running",
    "PAUSED": "paused",
    "ERROR": "needs attention",
    "NO_PEER": "ready; no other workstation paired yet",
    "PEER_OFFLINE": "up to date here; the other workstation is offline",
    "SYNCING": "synchronising",
    "CONFLICTS": "in sync, with conflict copies to review",
    "IN_SYNC": "in sync",
}


# ── conflicts and the trash ─────────────────────────────────────────────────


def trash_dir() -> Path:
    return paths.state_dir() / "work-trash"


def resolve_conflict(cfg: Config, conflict: str, keep: str) -> str:
    """keep = "current" (the file under the original name) or "conflict" (the copy).
    The version that loses is moved to the SUW trash, never deleted."""
    base = root(cfg)
    copy = (base / conflict).resolve()
    if base.resolve() not in copy.parents or CONFLICT_MARK not in copy.name or not copy.is_file():
        raise ValueError(f"not a conflict copy inside the work folder: {conflict}")
    original = base / original_of(os.path.relpath(copy, base.resolve()))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bin_dir = paths.ensure(trash_dir() / stamp)
    if keep == "current":
        target = bin_dir / copy.name
        shutil.move(str(copy), target)
        return str(target)
    if keep != "conflict":
        raise ValueError("keep must be 'current' or 'conflict'")
    target = bin_dir / original.name
    if original.exists():
        shutil.move(str(original), target)
    os.replace(copy, original)
    return str(target)


def versions(cfg: Config, prefix: str = "") -> list[dict]:
    """Files Syncthing moved aside here when the other workstation deleted or replaced them."""
    store = root(cfg) / VERSIONS_DIR
    out = []
    if not store.is_dir():
        return out
    for current, _dirs, names in os.walk(store):
        for name in names:
            full = Path(current) / name
            rel = os.path.relpath(full, store)
            if prefix and not rel.startswith(prefix):
                continue
            try:
                info = full.stat()
            except OSError:
                continue
            out.append({"path": rel, "bytes": info.st_size, "mtime": info.st_mtime})
    return sorted(out, key=lambda item: -item["mtime"])


def restore(cfg: Config, rel: str) -> str:
    """Copy a version back into the folder. An existing file of that name is kept, renamed."""
    base = root(cfg)
    source = (base / VERSIONS_DIR / rel).resolve()
    if (base / VERSIONS_DIR).resolve() not in source.parents or not source.is_file():
        raise ValueError(f"no such saved version: {rel}")
    target = base / rel
    if target.exists():
        target = target.with_name(f"{target.stem}.restored-{time.strftime('%Y%m%d-%H%M%S')}{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(target)


# ── Git repositories inside the shared folder ───────────────────────────────


def repo_file(cfg: Config, device: str) -> Path:
    return root(cfg) / ".suw" / f"repos.{device}.json"


def publish_repos(cfg: Config, repos: list[str]) -> bool:
    """Tell the other workstation which sub-folders are Git repositories and where they come
    from, so it can attach its own `.git` (the database itself is never copied)."""
    from ..core import gitsync

    base = root(cfg)
    listing = {}
    for rel in sorted(repos):
        url = gitsync.remote_url(base / rel)
        branch = gitsync.git(base / rel, "symbolic-ref", "--short", "HEAD").out.strip()
        if url and not gitsync.url_has_credentials(url):
            listing[rel] = {"url": url, "branch": branch or "main"}
    file = repo_file(cfg, cfg.device)
    text = json.dumps(listing, indent=1, sort_keys=True) + "\n"
    try:
        if file.read_text() == text:
            return False
    except OSError:
        if not listing:
            return False
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, file)
    return True


def detached_repos(cfg: Config) -> list[dict]:
    """Folders that are a Git repository on another workstation but have no `.git` here."""
    base = root(cfg)
    out: dict[str, dict] = {}
    for file in sorted((base / ".suw").glob("repos.*.json")):
        device = file.name[len("repos.") : -len(".json")]
        if device == cfg.device:
            continue
        try:
            listing = json.loads(file.read_text())
        except (OSError, ValueError):
            continue
        for rel, info in listing.items():
            target = (base / rel).resolve()
            if base.resolve() in [target, *target.parents] and not (target / ".git").exists() and isinstance(info, dict):
                out[rel] = {"path": rel, "url": str(info.get("url", "")), "branch": str(info.get("branch", "main")), "from": device}
    return list(out.values())


def attach_repo(cfg: Config, rel: str, url: str, branch: str) -> tuple[bool, str]:
    """Give an already-synchronised working tree its own Git database: fetch the history and
    point HEAD and the index at the remote branch. No file in the working tree is touched."""
    from ..core import gitsync

    target = root(cfg) / rel
    if (target / ".git").exists():
        return True, "already a repository"
    if not target.is_dir():
        return False, "folder has not arrived yet"
    if url.startswith("-") or branch.startswith("-") or gitsync.url_has_credentials(url):
        return False, "refusing an unsafe remote"
    steps = (
        ("init", "--quiet", f"--initial-branch={branch}"),
        ("remote", "add", "origin", url),
        ("fetch", "--quiet", "origin"),
    )
    for step in steps:
        res = gitsync.git(target, *step, timeout=600)
        if not res.ok:
            return False, f"git {step[0]}: {res.text[-200:]}"
    if not gitsync.git(target, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}").ok:
        return True, "attached; the remote has no such branch yet"
    for step in (
        ("update-ref", f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"),
        ("read-tree", "HEAD"),
        ("branch", "--quiet", f"--set-upstream-to=origin/{branch}", branch),
    ):
        res = gitsync.git(target, *step, timeout=120)
        if not res.ok:
            return False, f"git {step[0]}: {res.text[-200:]}"
    return True, "attached; files left exactly as they were"


def inside(cfg: Config, path: Path) -> bool:
    """Is this path within the shared work folder?"""
    try:
        base = root(cfg).resolve()
        target = Path(path).resolve()
    except OSError:
        return False
    return base == target or base in target.parents


def default_config() -> Config:
    return configmod.load()
