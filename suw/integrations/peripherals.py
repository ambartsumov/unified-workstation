"""One keyboard, one mouse and one clipboard across the workstations.

The sharing itself is Deskflow's job (a mature tool of the Synergy family); SUW never
speaks that protocol. What SUW owns is small and checkable:
  * the roles: which workstation has the physical keyboard (server) and which follows (client),
    under stable screen names `workstation-<device>` — no IP address is stored anywhere;
  * a private Deskflow profile in `~/.config/suw/deskflow/` in the format Deskflow 1.27 reads:
    `Deskflow.conf` (Qt INI: names, port, TLS, the computers), `deskflow-server.conf`
    (links + options only) and `tls/` (this machine's certificate, the trusted peers);
  * trust: each workstation publishes the fingerprint of its certificate to the inventory;
    it is used only after a person approved it on this machine (`suw peripherals pair`);
  * running the headless core (`deskflow-core server|client`) as a per-user service;
  * an honest health check: installed, running, link to the other workstation established.

Deskflow retries a lost connection itself at a fixed slow interval and the service manager
restarts a dead core with a growing delay; SUW adds no loop of its own. When the peer is away
each machine simply keeps its own keyboard and mouse.
"""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import tempfile
from pathlib import Path

from ..core import inventory, journal, paths, state
from ..core.config import Config
from ..core.proc import have, run
from . import tailscale

FLATPAK_ID = "org.deskflow.deskflow"
MAC_CORE = Path("/Applications/Deskflow.app/Contents/MacOS/deskflow-core")
SIDES = {"left": "right", "right": "left", "up": "down", "down": "up"}
SERVICE = "suw-deskflow.service"
LABEL = "io.github.ambartsumov.deskflow"
TEMPFAIL = 75  # exit code of `suw peripherals run` when it cannot start yet (the service retries)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_FP = re.compile(r"^[0-9a-f]{64}$")


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("peripherals.enabled", True))


def screen_name(device: str) -> str:
    return f"workstation-{device}"


def role(cfg: Config) -> str:
    """server = the keyboard and mouse are plugged in here; client = they arrive over the network."""
    return "server" if str(cfg.get("peripherals.server", "ubuntu")) == cfg.device else "client"


def profile_dir() -> Path:
    return paths.config_dir() / "deskflow"


def settings_file() -> Path:
    return profile_dir() / "Deskflow.conf"


def layout_file() -> Path:
    return profile_dir() / "deskflow-server.conf"


def certificate() -> Path:
    return profile_dir() / "tls" / "deskflow.pem"


def core() -> list[str]:
    """The command prefix that runs the headless Deskflow core here, or [] when it is not installed."""
    if paths.platform() == "macos":
        return [str(MAC_CORE)] if MAC_CORE.exists() else []
    if have("deskflow-core"):
        return ["deskflow-core"]
    if have("flatpak") and run(["flatpak", "info", FLATPAK_ID], timeout=10).ok:
        # the sandbox sees nothing of the home directory except the profile we hand it
        return ["flatpak", "run", f"--filesystem={profile_dir()}", "--command=deskflow-core", FLATPAK_ID]
    return []


def command(cfg: Config) -> list[str]:
    start = core()
    return [*start, role(cfg), "-s", str(settings_file())] if start else []


def workstations(inv: dict) -> list[str]:
    return [name for name, dev in inv["devices"].items() if dev.get("role") == "workstation" and _NAME.match(name)]


def server_host(cfg: Config, inv: dict | None = None) -> str:
    inv = inv or inventory.load()
    return inventory.device_host(inv["devices"].get(str(cfg.get("peripherals.server", "ubuntu")), {}))


# ── policy: pure text in, text out ────────────────────────────────────────────────────


def layout(server: str, others: list[str], side: str, clipboard: bool = True) -> str:
    """The server's screen map. Deskflow 1.27 takes the computers from the settings file and
    accepts only `links` and `options` here."""
    me = screen_name(server)
    lines = ["section: links", f"\t{me}:"]
    lines += [f"\t\t{side} = {screen_name(name)}" for name in others[:1]]
    for name in others[:1]:
        lines += [f"\t{screen_name(name)}:", f"\t\t{SIDES[side]} = {me}"]
    lines += ["end", "", "section: options", f"\tclipboardSharing = {'true' if clipboard else 'false'}", "end", ""]
    return "\n".join(lines)


def merge_ini(text: str, updates: dict[str, dict[str, str]]) -> str:
    """Set `[section] key=value` pairs in a Qt INI file, keeping every other line as it is.
    Deskflow stores things of its own in this file (the desktop's permission token), so the
    file is never rewritten wholesale."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        found = re.match(r"^\[(.+)\]\s*$", line)
        if found:
            sections.append((found.group(1), []))
        else:
            sections[-1][1].append(line)
    for name, pairs in updates.items():
        body = next((lines for section, lines in sections if section == name), None)
        if body is None:
            body = []
            sections.append((name, body))
        for key, value in pairs.items():
            fresh = f"{key}={value}"
            at = next((i for i, line in enumerate(body) if line.split("=", 1)[0].strip() == key and "=" in line), None)
            if at is not None:
                body[at] = fresh
                continue
            end = len(body)
            while end and not body[end - 1].strip():
                end -= 1
            body.insert(end, fresh)
    out: list[str] = []
    for name, body in sections:
        if name:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{name}]")
        out += body
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def settings(device: str, role_: str, names: list[str], port: int, interface: str, remote: str, clipboard: bool) -> dict[str, dict[str, str]]:
    wanted: dict[str, dict[str, str]] = {
        "core": {"computerName": screen_name(device), "port": str(port), "interface": interface if role_ == "server" else ""},
        "security": {"tlsEnabled": "true", "checkPeerFingerprints": "true"},
        "server": {"enableClipboard": "true" if clipboard else "false", "externalConfig": "false"},
    }
    if role_ == "client":
        wanted["client"] = {"remoteHost": remote}
    for name in names:
        wanted[f"computer_{screen_name(name)}"] = {"name": screen_name(name)}
    return wanted


def fingerprint_of(pem: str) -> str:
    """SHA-256 of the certificate inside a PEM bundle, lower-case hex ('' when there is none)."""
    found = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S)
    if not found:
        return ""
    try:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(found.group(0))).hexdigest()
    except ValueError:
        return ""


def trust_lines(fingerprints: list[str]) -> str:
    return "".join(f"v2:sha256:{fp}\n" for fp in sorted(set(fingerprints)) if _FP.match(fp))


def pretty(fp: str) -> str:
    return ":".join(fp[i : i + 2] for i in range(0, len(fp), 2)).upper()


# ── this machine ─────────────────────────────────────────────────────────────────────


def fingerprint() -> str:
    try:
        return fingerprint_of(certificate().read_text())
    except OSError:
        return ""


def make_certificate() -> bool:
    """A self-signed certificate + key in one PEM, as Deskflow expects. Created once; a new one
    would have to be approved again on the other workstation."""
    target = certificate()
    if fingerprint():
        return False
    if not have("openssl"):
        return False
    paths.ensure(target.parent)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        key, crt = Path(tmp) / "key", Path(tmp) / "crt"
        res = run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", "/CN=Deskflow", "-keyout", str(key), "-out", str(crt)],
            timeout=60,
        )
        if not res.ok or not crt.exists():
            return False
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(key.read_text() + crt.read_text())
    os.chmod(target, 0o600)
    journal.record("create", str(target))
    return True


def bind_address(cfg: Config) -> str | None:
    """Where the server listens: '' = every interface, an address, or None = not available yet.
    Default 'tailnet': only machines of the private network can even reach the port."""
    want = str(cfg.get("peripherals.bind", "tailnet"))
    if want == "all":
        return ""
    if want != "tailnet":
        return want
    st = tailscale.status()
    return (st.get("self") or {}).get("ip") or None if st.get("running") else None


def offered(cfg: Config, inv: dict | None = None) -> list[tuple[str, str]]:
    """(device, fingerprint) of the other workstations, as published in the inventory."""
    inv = inv or inventory.load()
    return [(name, str(inv["devices"][name].get("deskflow_fp", ""))) for name in workstations(inv) if name != cfg.device and _FP.match(str(inv["devices"][name].get("deskflow_fp", "")))]


def approved() -> list[str]:
    return [fp for fp in state.load("peripherals").get("trusted", []) if _FP.match(fp)]


def approve(fp: str) -> None:
    data = state.load("peripherals")
    data["trusted"] = sorted({*data.get("trusted", []), fp})
    state.save("peripherals", data)


def revoke(fp: str) -> None:
    data = state.load("peripherals")
    data["trusted"] = [f for f in data.get("trusted", []) if f != fp]
    state.save("peripherals", data)


def unpaired(cfg: Config, inv: dict | None = None) -> list[tuple[str, str]]:
    known = set(approved())
    return [(name, fp) for name, fp in offered(cfg, inv) if fp not in known]


def prepare(cfg: Config, inv: dict | None = None, interface: str | None = None) -> bool:
    """Bring the private Deskflow profile in line with the configuration. Idempotent."""
    inv = inv or inventory.load()
    paths.ensure(profile_dir())
    paths.ensure(profile_dir() / "tls")
    changed = make_certificate()
    names = sorted({*workstations(inv), cfg.device})
    server = str(cfg.get("peripherals.server", "ubuntu"))
    side = str(cfg.get("peripherals.peer_side", "right"))
    clip = bool(cfg.get("peripherals.clipboard", True))
    if interface is None:
        interface = bind_address(cfg) or ""
    wanted = settings(cfg.device, role(cfg), names, int(cfg.get("peripherals.port", 24800)), interface, server_host(cfg, inv), clip)
    target = settings_file()
    before = target.read_text() if target.exists() else ""
    changed = journal.write_file(target, merge_ini(before, wanted), mode=0o600) or changed
    if role(cfg) == "server":
        changed = journal.write_file(layout_file(), layout(server, [n for n in names if n != server], side if side in SIDES else "right", clip)) or changed
    trusted = trust_lines([fp for _, fp in offered(cfg, inv) if fp in set(approved())])
    for name in ("trusted-clients", "trusted-servers"):
        changed = journal.write_file(profile_dir() / "tls" / name, trusted, mode=0o600) or changed
    return changed


def publish(cfg: Config) -> bool:
    """Offer this machine's certificate fingerprint to the other workstation via the inventory."""
    mine = fingerprint()
    inv = inventory.load()
    if not mine or inv["devices"].get(cfg.device, {}).get("deskflow_fp") == mine:
        return False
    inventory.ensure_device(inv, cfg.device, role="workstation", deskflow_fp=mine)
    inventory.save(inv)
    return True


def service(action: str) -> bool:
    from ..core import supervise

    if supervise.needed():
        if action in ("stop", "restart"):
            supervise.stop("deskflow", forget=action == "stop")
        if action in ("start", "restart"):
            return supervise.start("deskflow", [str(paths.launcher()), "peripherals", "run"])
        return True
    if paths.platform() == "macos":
        target = f"gui/{os.getuid()}/{LABEL}"
        return run(["launchctl", "kickstart", "-k", target] if action in ("start", "restart") else ["launchctl", "kill", "TERM", target], timeout=15).ok
    return run(["systemctl", "--user", action, SERVICE], timeout=30).ok


def _sockets(port: int) -> tuple[bool, int]:
    """(listening on the port, established connections on it) — from the kernel, not from Deskflow."""
    if paths.platform() == "macos":
        res = run(["lsof", "-nP", f"-iTCP:{port}"], timeout=10)
        lines = res.out.splitlines()[1:] if res.ok else []
        return any("(LISTEN)" in line for line in lines), sum("(ESTABLISHED)" in line for line in lines)
    listening = run(["ss", "-Hltn", f"sport = :{port}"], timeout=5)
    linked = run(["ss", "-Htn", "state", "established", f"( sport = :{port} or dport = :{port} )"], timeout=5)
    return bool(listening.ok and listening.out.strip()), len([line for line in linked.out.splitlines() if line.strip()]) if linked.ok else 0


def running() -> bool:
    return run(["pgrep", "-x", "deskflow-core"], timeout=5).ok


def status(cfg: Config) -> dict:
    """DISABLED · NOT_INSTALLED · NOT_CONFIGURED · STOPPED · UNPAIRED · WAITING · CONNECTED."""
    port = int(cfg.get("peripherals.port", 24800))
    out = {"enabled": enabled(cfg), "role": role(cfg), "screen": screen_name(cfg.device), "port": port, "server": server_host(cfg), "installed": bool(core())}
    if not out["enabled"]:
        return {**out, "state": "DISABLED"}
    if not out["installed"]:
        return {**out, "state": "NOT_INSTALLED"}
    out["fingerprint"] = fingerprint()
    if not settings_file().exists() or not out["fingerprint"]:
        return {**out, "state": "NOT_CONFIGURED"}
    out["waiting"] = [name for name, _ in unpaired(cfg)]
    out["trusted"] = len([fp for _, fp in offered(cfg) if fp in set(approved())])
    if not running():
        return {**out, "state": "STOPPED"}
    listening, links = _sockets(port)
    out.update(listening=listening, links=links)
    out["state"] = "CONNECTED" if links else ("WAITING" if out["trusted"] else "UNPAIRED")
    return out


WORDS = {
    "DISABLED": "switched off (peripherals.enabled = false)",
    "NOT_INSTALLED": "Deskflow is not installed",
    "NOT_CONFIGURED": "not set up on this machine yet",
    "STOPPED": "the sharing service is not running",
    "UNPAIRED": "running; no other workstation is approved yet",
    "WAITING": "running; the other workstation is not connected",
    "CONNECTED": "keyboard, mouse and clipboard are shared",
}


def permissions() -> list[str]:
    """What the operating system asks a person once. SUW cannot click these."""
    if paths.platform() == "macos":
        return [
            "macOS: System Settings → Privacy & Security → Accessibility: allow deskflow-core (required).",
            "macOS: the same page → Input Monitoring: allow deskflow-core. If the firewall is on, allow incoming connections.",
        ]
    return [
        "GNOME asks once to allow sharing the keyboard and mouse; press Share/Allow.",
        "After a re-login it may ask again — that is the desktop's permission dialog, not a fault.",
    ]


def exposed(cfg: Config) -> bool:
    """Is the sharing port listening on every interface (reachable from any network we join)?"""
    port = str(cfg.get("peripherals.port", 24800))
    if paths.platform() != "linux" or not port.isdigit():
        return False
    res = run(["ss", "-Hltn", f"sport = :{port}"], timeout=5)
    return res.ok and any(part in res.out for part in (f"0.0.0.0:{port}", f"*:{port}", f"[::]:{port}"))
