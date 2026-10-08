"""Cloud role lifecycle: enroll, replace, bootstrap, retire.

`cloud` is a logical role held by one disposable machine at a time. Everything that runs on
the remote machine is one of the fixed, reviewable scripts in `suw/cloud/scripts/`; there is
no path for arbitrary remote commands.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from ..core import events, health, inventory, paths
from ..core.config import Config
from ..core.proc import Result, run, ssh_cmd
from ..integrations import ssh as sshcfg

SCRIPTS = Path(__file__).with_name("scripts")
PROFILES = ("minimal", "compute", "cuda", "inference", "training")
KEY_ORDER = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa")
BOOTSTRAP_VERSION = 2  # bump when scripts/bootstrap.sh changes what it installs


class CloudError(RuntimeError):
    """Expected failure with a message fit to show the user."""


class CloudProvider(ABC):
    """Provider seam. The generic SSH provider below covers any rented Linux box;
    provider-specific plugins (API-driven create/destroy) can implement the same surface."""

    name = "custom"

    @abstractmethod
    def validate_endpoint(self, host: str, user: str, port: int) -> None: ...

    @abstractmethod
    def connect(self, target: "Target") -> None: ...

    @abstractmethod
    def discover(self, target: "Target") -> dict: ...

    @abstractmethod
    def enroll(self, target: "Target", authkey: str) -> dict: ...

    @abstractmethod
    def bootstrap(self, target: "Target", profile: str, docker: bool = False) -> list[str]: ...

    @abstractmethod
    def health(self, target: "Target") -> dict: ...

    @abstractmethod
    def retire(self, target: "Target") -> bool: ...


class Target:
    """How to reach one node before (direct endpoint) or after (alias) it is in the inventory."""

    def __init__(self, host: str, user: str, port: int, label: str, host_key: str):
        self.host, self.user, self.port, self.label, self.host_key = host, user, port, label, host_key
        self._known: str | None = None

    def known_hosts(self) -> str:
        if self._known is None:
            fd, self._known = tempfile.mkstemp(prefix="suw-kh-", dir=paths.ensure(paths.runtime_dir()))
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{sshcfg.cloud_alias(self.label)} {self.host_key}\n")
        return self._known

    def close(self) -> None:
        if self._known:
            Path(self._known).unlink(missing_ok=True)
            self._known = None

    def ssh(self, remote: list[str], *, stdin: str | None = None, timeout: float = 60, batch: bool = True) -> Result:
        cmd = [
            *ssh_cmd(),
            "-o", f"UserKnownHostsFile={self.known_hosts()}",
            "-o", f"HostKeyAlias={sshcfg.cloud_alias(self.label)}",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ControlPath=none",
            "-o", "ConnectTimeout=10",
            "-o", f"BatchMode={'yes' if batch else 'no'}",
            "-p", str(self.port),
            "-l", self.user,
            self.host,
            "--",
            *remote,
        ]
        return run(cmd, stdin=stdin, timeout=timeout)

    def script(self, name: str, args: list[str], *, stdin_prefix: str = "", timeout: float = 600) -> Result:
        """Run one of the fixed scripts. `stdin_prefix` (e.g. a secret) is consumed by the
        script itself, so secrets never appear in argv or in the process list."""
        args = [shlex.quote(a) for a in args]  # ssh re-parses argv through the remote shell
        if stdin_prefix:
            # Two-stage: ship the script to a private temp file, then feed the secret on stdin.
            put = self.ssh(["sh", "-c", "'umask 077; f=$(mktemp); cat > \"$f\"; echo \"$f\"'"], stdin=(SCRIPTS / name).read_text(), timeout=30)
            if not put.ok or not put.out.strip():
                return put
            remote_file = put.out.strip().splitlines()[-1]
            if not remote_file.startswith("/tmp/") and not remote_file.startswith("/var/"):
                return Result(1, "", "unexpected temp path")
            try:
                return self.ssh(["sh", remote_file, *args], stdin=stdin_prefix, timeout=timeout)
            finally:
                self.ssh(["rm", "-f", remote_file], timeout=15)
        return self.ssh(["sh", "-s", "--", *args], stdin=(SCRIPTS / name).read_text(), timeout=timeout)


# ── host identity ───────────────────────────────────────────────────────────


def scan_host_key(host: str, port: int) -> tuple[str, str]:
    """Fetch the host public key. Returns (key, SHA256 fingerprint). No trust is implied —
    the caller must get an explicit decision before using it."""
    res = run(["ssh-keyscan", "-T", "8", "-p", str(port), "-t", "ed25519,ecdsa,rsa", host], timeout=20)
    found: dict[str, str] = {}
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            found.setdefault(parts[1], f"{parts[1]} {parts[2]}")
    for kind in KEY_ORDER:
        if kind in found:
            fp = run(["ssh-keygen", "-lf", "-"], stdin=f"host {found[kind]}\n", timeout=10)
            parts = fp.out.split()
            if fp.ok and len(parts) >= 2:
                return found[kind], parts[1]
    raise CloudError(f"cannot reach SSH on {host}:{port} (no host key offered)")


def fingerprints_match(expected: str, actual: str) -> bool:
    def norm(value: str) -> str:
        return value.strip().removeprefix("SHA256:").rstrip("=")

    return bool(expected.strip()) and norm(expected) == norm(actual)


# ── generic SSH provider ────────────────────────────────────────────────────


class GenericSSH(CloudProvider):
    def validate_endpoint(self, host: str, user: str, port: int) -> None:
        if not inventory.valid_host(host):
            raise CloudError(f"'{host}' is not a valid IP address or hostname")
        if not inventory.valid_user(user):
            raise CloudError(f"'{user}' is not a valid SSH username")
        if not 1 <= port <= 65535:
            raise CloudError(f"port {port} is out of range")

    def connect(self, target: Target) -> None:
        res = target.ssh(["true"], timeout=20)
        if res.ok:
            return
        err = res.err.strip()
        if "Permission denied" in err:
            raise CloudError("authentication failed: your SSH key is not authorised for this user")
        if "Host key verification failed" in err:
            raise CloudError("host identity changed between scan and connect - aborting")
        raise CloudError(f"SSH connection failed: {(err.splitlines() or ['unknown error'])[-1]}")

    def discover(self, target: Target) -> dict:
        res = target.ssh(["sh", "-s"], stdin=health.PROBE.read_text(), timeout=40)
        if not res.ok or "probe=1" not in res.out:
            raise CloudError("hardware discovery failed")
        return health.parse(res.out)

    def enroll(self, target: Target, authkey: str) -> dict:
        res = target.script("tailscale_enroll.sh", [target.label], stdin_prefix=authkey.strip() + "\n", timeout=240)
        if not res.ok:
            raise CloudError("Tailscale enrollment failed: " + events.redact((res.err.strip().splitlines() or ["unknown"])[-1]))
        return dict(line.split("=", 1) for line in res.out.splitlines() if "=" in line)

    def bootstrap(self, target: Target, profile: str, docker: bool = False) -> list[str]:
        if profile not in PROFILES:
            raise CloudError(f"unknown profile '{profile}' (choose: {', '.join(PROFILES)})")
        res = target.script("bootstrap.sh", [profile, *(["docker"] if docker else [])], timeout=1200)
        steps = [line[5:] for line in res.out.splitlines() if line.startswith("step=")]
        if not res.ok:
            raise CloudError("bootstrap failed: " + ((res.err.strip().splitlines() or ["unknown"])[-1]))
        return steps

    def health(self, target: Target) -> dict:
        try:
            return {"online": True, **self.discover(target)}
        except CloudError as exc:
            return {"online": False, "error": str(exc)}

    def retire(self, target: Target) -> bool:
        return target.ssh(["sh", "-c", "'s=; [ \"$(id -u)\" -eq 0 ] || s=\"sudo -n\"; $s tailscale logout'"], timeout=30).ok


PROVIDERS: dict[str, type[CloudProvider]] = {"custom": GenericSSH}


def provider(name: str = "custom") -> CloudProvider:
    return PROVIDERS.get(name, GenericSSH)()


def target_for(label: str, node: dict, prefer_tailscale: bool = True) -> Target:
    host = (node.get("tailscale_name") if prefer_tailscale else "") or node.get("endpoint", "")
    port = 22 if (prefer_tailscale and node.get("tailscale_name")) else int(node.get("port", 22))
    return Target(host, node.get("user", "root"), port, label, node.get("host_key", ""))


# ── lifecycle (pure inventory transitions are in core.inventory) ────────────


def register(cfg: Config, label: str, node: dict) -> str | None:
    """Give `label` the cloud role, re-render SSH config. Returns the retired label, if any."""
    inv = inventory.load()
    retired = inventory.cloud_assign(inv, label, node)
    inventory.save(inv)
    sshcfg.apply(inv, cfg.device)
    events.emit("cloud.enrolled" if not retired else "cloud.replaced", f"cloud role -> {label}" + (f" (retired {retired})" if retired else ""), label=label, status="ok")
    return retired


def release(cfg: Config) -> str | None:
    inv = inventory.load()
    label = inventory.cloud_release(inv)
    inventory.save(inv)
    sshcfg.apply(inv, cfg.device)
    if label:
        events.emit("cloud.retired", f"cloud role released ({label} retired)", label=label, status="ok")
    return label


def alias_answers() -> tuple[bool, str]:
    """Does `ssh cloud` (the alias the user types) reach a machine right now?"""
    report = health.probe_remote("cloud")
    return bool(report.get("online")), str(report.get("error", ""))


def switch_role(cfg: Config, label: str, node: dict, verify=alias_answers) -> str | None:
    """Move the `cloud` role to `label` — or leave everything exactly as it was.

    The inventory and the rendered SSH files are snapshotted first. If the alias does not
    answer after the switch, both are put back and the previous node keeps the role.
    """
    from ..core import tomlw

    inv_file = inventory.file()
    snapshot = inv_file.read_text(encoding="utf-8") if inv_file.exists() else None
    retired = register(cfg, label, dict(node))
    ok, why = verify()
    if ok:
        return retired
    if snapshot is None:
        inv_file.unlink(missing_ok=True)
    else:
        tomlw.atomic_write(inv_file, snapshot, 0o644)
    sshcfg.apply(inventory.load(), cfg.device)
    events.emit("cloud.rollback", f"cloud role kept on the previous node: `ssh cloud` did not reach {label}", "warn", label=label, status="rolled-back")
    raise CloudError(f"the new machine did not answer as `cloud` ({why or 'unreachable'}); the previous cloud role was left untouched")


def enroll(
    cfg: Config,
    prov: CloudProvider,
    target: Target,
    node: dict,
    *,
    authkey: str = "",
    profile: str = "",
    docker: bool = False,
    say=lambda message: None,
    verify=alias_answers,
) -> dict:
    """Everything after the human trust decision, in the order that keeps the role safe:

        connect -> discover -> tailnet -> bootstrap -> health check -> switch role

    The logical `cloud` mapping is not touched until the candidate passed its health
    check; any failure before that leaves the current cloud node in charge.
    Returns {"label", "retired", "node", "steps"}.
    """
    prov.connect(target)
    say("SSH connection verified")
    report = prov.discover(target)
    hardware = health.hardware(report)
    node = {**node, "hardware": hardware}
    say(f"{hardware.get('os', 'Linux')} · {hardware.get('gpu') or node.get('declared_gpu') or 'no GPU'} · {hardware.get('cores', '?')} vCPU · {hardware.get('ram_gb', '?')}GB RAM")

    candidate = target
    if authkey:
        try:
            joined = prov.enroll(target, authkey)
            name = joined.get("tailscale_name") or target.label
            over_tailnet = Target(name, target.user, 22, target.label, target.host_key)
            try:
                reachable = over_tailnet.ssh(["true"], timeout=25).ok
            finally:
                over_tailnet.close()
            if reachable:
                node["tailscale_name"] = name
                candidate = Target(name, target.user, 22, target.label, target.host_key)
                say(f"joined the tailnet as {name}")
            else:
                say("! joined the tailnet but SSH over it is not reachable; using the public endpoint")
        except CloudError as exc:
            say(f"! {exc}; using the public endpoint")
    else:
        say("tailnet skipped; `ssh cloud` will use the public endpoint (reported as degraded)")

    steps: list[str] = []
    try:
        if profile and profile != "none":
            steps = prov.bootstrap(candidate, profile, docker)
            node["bootstrap_profile"] = profile
            node["bootstrap_version"] = BOOTSTRAP_VERSION
            say(f"bootstrap profile '{profile}' applied")
        check = prov.health(candidate)
        if not check.get("online"):
            raise CloudError(f"the new machine failed its health check ({check.get('error', 'no answer')}); the cloud role was not changed")
        say("health check passed")
    finally:
        if candidate is not target:
            candidate.close()
    retired = switch_role(cfg, target.label, node, verify)
    return {"label": target.label, "retired": retired, "node": node, "steps": steps}


def parse_report(text: str) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {k: [] for k in ("dirty", "unpushed", "noremote", "job", "mount", "output")}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in report and value:
            report[key].append(value.replace("|", " - "))
    return report


def teardown_report(target: Target, outputs: list[str]) -> dict[str, list[str]]:
    safe = [o for o in outputs if o and not o.startswith("-") and "\n" not in o]
    res = target.script("teardown_report.sh", safe, timeout=120)
    if not res.ok:
        raise CloudError("could not inspect the node: " + ((res.err.strip().splitlines() or ["unreachable"])[-1]))
    return parse_report(res.out)


def report_counts(report: dict[str, list[str]]) -> dict[str, int]:
    return {
        "uncommitted": len(report["dirty"]) + len(report["unpushed"]) + len(report["noremote"]),
        "unexported": len(report["output"]),
        "jobs": len(report["job"]),
    }


def is_risky(report: dict[str, list[str]]) -> bool:
    """Obvious data-loss conditions that make teardown refuse by default."""
    return any(report[k] for k in ("dirty", "unpushed", "noremote", "job", "output"))
