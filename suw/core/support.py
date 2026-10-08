"""Support bundle: what a maintainer needs to help, and nothing that identifies or exposes
the user beyond that.

Included: product version, operating system, detected components and features, health
checks, recent events, and the *shape* of the configuration.

Never included: files from the work folder, passwords, keys, tokens, credentials. Addresses,
host names, user names and the home directory path are replaced by stable placeholders
(`host-1`, `user-1`, `~`) so a log stays readable without naming anything real.
"""

from __future__ import annotations

import getpass
import io
import json
import re
import socket
import time
import zipfile
from typing import Any

from .. import platform as osplatform
from .. import product
from . import capabilities, config, events, inventory, paths

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
_SYNC_ID = re.compile(r"\b[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}\b")
_FINGERPRINT = re.compile(r"\b(?:SHA256:[A-Za-z0-9+/]{20,}|[0-9a-f]{64}|(?:[0-9A-F]{2}:){15,}[0-9A-F]{2})\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_HOSTLIKE = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:ts\.net|local|lan|internal|home|corp)\b", re.I)
_KEEP_IPS = ("127.0.0.1", "0.0.0.0", "192.0.2.1")
_SENSITIVE_KEYS = ("host", "tailscale_name", "ssh_user", "endpoint", "user", "identity", "host_key", "fingerprint", "syncthing_id", "deskflow_fp", "id")


class Redactor:
    """Replaces identifying strings with stable placeholders, consistently across the bundle."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        home = str(paths.home())
        self.home_forms = sorted({home, home.replace("\\", "/"), str(paths.home().resolve())}, key=len, reverse=True)
        for value, kind in ((_safe(getpass.getuser), "user"), (_safe(socket.gethostname), "host")):
            if value and len(value) > 2:
                self.learn(value, kind)
                self.learn(value.split(".", 1)[0], kind)

    def learn(self, value: str, kind: str) -> str:
        value = str(value)
        if not value or value in _KEEP_IPS:
            return value
        if value not in self.names:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            self.names[value] = f"<{kind}-{self.counters[kind]}>"
        return self.names[value]

    def learn_inventory(self, inv: dict) -> None:
        nodes = list(inv.get("devices", {}).values()) + list(inv.get("cloud", {}).get("nodes", {}).values())
        for node in nodes:
            for key in ("host", "tailscale_name", "endpoint"):
                if node.get(key):
                    self.learn(str(node[key]), "host")
            for key in ("ssh_user", "user"):
                if node.get(key):
                    self.learn(str(node[key]), "user")

    def text(self, value: str) -> str:
        value = events.redact(value)
        for form in self.home_forms:
            if form and form not in ("/", "\\"):
                value = value.replace(form, "~")
        for known in sorted(self.names, key=len, reverse=True):
            if len(known) > 2:  # anywhere it stands as a word of its own, in any letter case
                value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(known)}(?![A-Za-z0-9])", self.names[known], value, flags=re.I)
        value = _SYNC_ID.sub("<sync-id>", value)
        value = _FINGERPRINT.sub("<fingerprint>", value)
        value = _EMAIL.sub("<email>", value)
        value = _HOSTLIKE.sub(lambda m: self.learn(m.group(0), "host"), value)
        value = _IPV4.sub(lambda m: m.group(0) if m.group(0) in _KEEP_IPS else self.learn(m.group(0), "host"), value)
        value = _IPV6.sub(lambda m: self.learn(m.group(0), "host"), value)
        return value

    def data(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: self.data(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(v, key) for v in value]
        if isinstance(value, str):
            if key in _SENSITIVE_KEYS and value:
                kind = "user" if "user" in key else "id" if key in ("id", "syncthing_id", "deskflow_fp", "fingerprint", "host_key", "identity") else "host"
                return self.learn(value, kind) if kind != "id" else "<redacted>"
            return self.text(value)
        return value


def _safe(func) -> str:
    try:
        return str(func())
    except Exception:
        return ""


def collect(include_events: int = 400) -> dict[str, Any]:
    """The bundle as data (already redacted). Pure enough to test."""
    from . import doctor

    redactor = Redactor()
    cfg = config.load()
    inv = inventory.load()
    redactor.learn_inventory(inv)
    redactor.learn(cfg.device, "host")
    for name in inv["devices"]:
        redactor.learn(name, "host")
    os_ = osplatform.current()
    parts = capabilities.components()
    try:
        checks = [c.as_dict() for c in doctor.run_all(cfg)]
    except Exception as exc:
        checks = [{"level": "WARN", "title": "Health checks could not run", "detail": str(exc)[:200]}]
    flat = config._flatten({k: v for k, v in cfg.data.items() if k not in ("secrets",)})
    settings = {key: ("<set>" if isinstance(value, str) and ("://" in value or key.startswith("urls.")) else value) for key, value in flat.items()}
    devices = {
        f"device-{index}": {"role": device.get("role", ""), "platform": device.get("platform", ""), "self": name == cfg.device, "has_sync_identity": bool(device.get("syncthing_id")), "has_input_identity": bool(device.get("deskflow_fp")), "reachable_by": "tailscale" if device.get("tailscale_name") else "host" if device.get("host") else "none"}
        for index, (name, device) in enumerate(sorted(inv["devices"].items()), 1)
    }
    return {
        "bundle.json": {"created": time.time(), "product": product.as_dict(), "frozen": paths.frozen(), "note": "Addresses, names and identities are replaced by placeholders. No work files, passwords, keys or tokens are included."},
        "platform.json": redactor.data({**os_.info().as_dict(), "credential_store": os_.credential_backend(), "autostart": os_.autostart().mechanism, "permissions": [{"id": p.id, "state": p.state} for p in os_.permissions()]}),
        "components.json": redactor.data([{k: v for k, v in c.as_dict().items() if k in ("id", "installed", "version")} for c in parts]),
        "features.json": redactor.data([f.as_dict() for f in capabilities.features(cfg, parts)]),
        "health.json": redactor.data(checks),
        "settings.json": redactor.data(settings),
        "config-warnings.json": redactor.data(list(cfg.warnings)),
        "devices.json": devices,
        "events.jsonl": "\n".join(json.dumps(redactor.data({k: v for k, v in record.items() if k != "device"}), sort_keys=True) for record in events.read(include_events)) + "\n",
    }


def build() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in collect().items():
            archive.writestr(name, content if isinstance(content, str) else json.dumps(content, indent=2, sort_keys=True, default=str))
        archive.writestr("README.txt", "Unified Workstation support bundle.\nReview it before sharing: it is plain text inside a zip archive.\nIt contains no work files, passwords, keys or tokens; names and addresses are placeholders.\n")
    events.emit("support.bundle", "support bundle created")
    return buffer.getvalue()


def filename() -> str:
    return f"{product.SLUG}-support-{time.strftime('%Y%m%d-%H%M%S')}.zip"
