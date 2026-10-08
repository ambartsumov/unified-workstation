"""Cloud role lifecycle: atomic replacement, host-key safety, real SSH against local servers.

The "cloud machines" here are unprivileged sshd processes on 127.0.0.1, each with its own
host key. That exercises the real ssh / ssh-keyscan / known-hosts code paths. It is not a
rented server: Tailscale enrollment and package bootstrap over SSH are not covered here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from suw import cloud
from suw.cli import main as cli
from suw.cloud import CloudError
from suw.core import config, health, inventory, paths
from suw.integrations import ssh as sshcfg

KEY_A, KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB"


class FakeProvider(cloud.CloudProvider):
    """Scriptable provider for failure injection."""

    def __init__(self, healthy: bool = True, connect_error: str = "", bootstrap_error: str = ""):
        self.healthy, self.connect_error, self.bootstrap_error = healthy, connect_error, bootstrap_error
        self.calls: list[str] = []

    def validate_endpoint(self, host, user, port):
        self.calls.append("validate")

    def connect(self, target):
        self.calls.append("connect")
        if self.connect_error:
            raise CloudError(self.connect_error)

    def discover(self, target):
        self.calls.append("discover")
        return {"os": "Ubuntu 24.04", "cores": 8, "ram_mb": 32768, "gpus": [{"name": "NVIDIA A100", "vram_mb": 81920, "used_mb": 0, "util_pct": 0}], "cuda": "12.4"}

    def enroll(self, target, authkey):
        self.calls.append("tailscale")
        return {}

    def bootstrap(self, target, profile, docker=False):
        self.calls.append(f"bootstrap:{profile}")
        if self.bootstrap_error:
            raise CloudError(self.bootstrap_error)
        return ["packages already present"]

    def health(self, target):
        self.calls.append("health")
        return {"online": True} if self.healthy else {"online": False, "error": "kernel panic"}

    def retire(self, target):
        return True


def node(endpoint: str, key: str) -> dict:
    return {"provider": "testlab", "role": "compute", "endpoint": endpoint, "port": 22, "user": "root", "host_key": key, "fingerprint": "SHA256:x"}


def enroll(label: str, endpoint: str, key: str, prov: FakeProvider, verify=lambda: (True, ""), profile: str = "") -> dict:
    target = cloud.Target(endpoint, "root", 22, label, key)
    try:
        return cloud.enroll(config.load(), prov, target, node(endpoint, key), profile=profile, verify=verify)
    finally:
        target.close()


def snapshot() -> tuple[str, str, str]:
    ssh = paths.ssh_dir()
    return inventory.file().read_text(), (ssh / "suw.conf").read_text(), (ssh / "suw_known_hosts").read_text()


# ── atomic replacement (fake provider) ──────────────────────────────────────


def test_failed_replacement_leaves_the_previous_cloud_in_charge():
    enroll("cloud-a", "203.0.113.5", KEY_A, FakeProvider())
    before = snapshot()
    assert inventory.cloud_current(inventory.load())[0] == "cloud-a"

    sick = FakeProvider(healthy=False)
    with pytest.raises(CloudError, match="failed its health check"):
        enroll("cloud-b", "198.51.100.9", KEY_B, sick)
    assert sick.calls[-1] == "health"
    assert snapshot() == before  # inventory, ssh config and known hosts untouched
    assert "cloud-b" not in inventory.load()["cloud"]["nodes"]

    for broken in (FakeProvider(connect_error="SSH connection failed: refused"), FakeProvider(bootstrap_error="bootstrap failed: apt")):
        with pytest.raises(CloudError):
            enroll("cloud-b", "198.51.100.9", KEY_B, broken, profile="compute")
        assert snapshot() == before

    result = enroll("cloud-b", "198.51.100.9", KEY_B, FakeProvider(), profile="compute")
    inv = inventory.load()
    assert result["retired"] == "cloud-a" and inventory.cloud_current(inv)[0] == "cloud-b"
    assert inv["cloud"]["nodes"]["cloud-a"]["state"] == "retired" and inv["cloud"]["nodes"]["cloud-a"]["retired_at"]
    assert inv["cloud"]["nodes"]["cloud-b"]["bootstrap_version"] == cloud.BOOTSTRAP_VERSION
    conf = (paths.ssh_dir() / "suw.conf").read_text()
    assert "HostName 198.51.100.9" in conf and "203.0.113.5" not in conf and "HostKeyAlias suw-cloud-b" in conf
    assert KEY_A not in (paths.ssh_dir() / "suw_known_hosts").read_text()  # the retired key is no longer trusted


def test_switch_is_rolled_back_when_the_alias_does_not_answer():
    enroll("cloud-a", "203.0.113.5", KEY_A, FakeProvider())
    before = snapshot()
    with pytest.raises(CloudError, match="previous cloud role was left untouched"):
        enroll("cloud-b", "198.51.100.9", KEY_B, FakeProvider(), verify=lambda: (False, "connection timed out"))
    assert snapshot() == before
    assert inventory.cloud_current(inventory.load())[0] == "cloud-a"


def test_first_enrollment_failure_leaves_no_cloud_at_all():
    with pytest.raises(CloudError):
        enroll("cloud-a", "203.0.113.5", KEY_A, FakeProvider(), verify=lambda: (False, "nope"))
    assert inventory.cloud_current(inventory.load()) == (None, None)
    assert "Host cloud" not in (paths.ssh_dir() / "suw.conf").read_text()


def test_order_is_bootstrap_then_health_then_switch():
    prov = FakeProvider()
    seen = []
    enroll("cloud-a", "203.0.113.5", KEY_A, prov, verify=lambda: (seen.append(list(prov.calls)) or True, ""), profile="minimal")
    assert seen[0] == ["connect", "discover", "bootstrap:minimal", "health"]  # the role moved only after all of it


def test_user_ssh_config_is_never_rewritten(sandbox):
    user_config = sandbox / ".ssh" / "config"
    user_config.parent.mkdir(mode=0o700, exist_ok=True)
    user_config.write_text("Host work\n    HostName work.example.org\n    User me\n")
    enroll("cloud-a", "203.0.113.5", KEY_A, FakeProvider())
    enroll("cloud-b", "198.51.100.9", KEY_B, FakeProvider())
    text = user_config.read_text()
    assert "Host work\n    HostName work.example.org\n    User me\n" in text
    assert text.count("Include") == 1 and "203.0.113.5" not in text and "198.51.100.9" not in text


# ── real SSH against local servers ──────────────────────────────────────────


def real_enroll(server, label: str, user: str, prov: cloud.CloudProvider | None = None) -> dict:
    key, fingerprint = cloud.scan_host_key("127.0.0.1", server.port)
    target = cloud.Target("127.0.0.1", user, server.port, label, key)
    spec = {"provider": "testlab", "role": "compute", "endpoint": "127.0.0.1", "port": server.port, "user": user, "host_key": key, "fingerprint": fingerprint}
    try:
        return cloud.enroll(config.load(), prov or cloud.GenericSSH(), target, spec)
    finally:
        target.close()


def whoami_via_alias() -> str:
    res = health.ssh("cloud", ["echo", "$SSH_CONNECTION"])
    return res.out.strip().split()[-1] if res.ok and res.out.strip() else ""


def test_real_enrollment_makes_ssh_cloud_work(ssh_lab):
    server = ssh_lab("a")
    result = real_enroll(server, "cloud-a", ssh_lab.user)
    assert result["node"]["hardware"]["cores"] >= 1  # discovered by the real probe over SSH
    report = health.probe_remote("cloud")
    assert report["online"] and report["status"] == health.ONLINE
    assert whoami_via_alias() == str(server.port)
    # Not on the tailnet: honest about the degraded path.
    assert health.server_status(True, report, direct=True) == health.DEGRADED


def test_real_replacement_moves_the_alias_to_the_new_machine(ssh_lab):
    """New IP/port, new host key, same logical name: `ssh cloud` just keeps working."""
    a, b = ssh_lab("a"), ssh_lab("b")
    real_enroll(a, "cloud-a", ssh_lab.user)
    assert whoami_via_alias() == str(a.port)
    result = real_enroll(b, "cloud-b", ssh_lab.user)
    assert result["retired"] == "cloud-a"
    assert whoami_via_alias() == str(b.port)  # no host-key prompt, no manual config edit
    a.stop()
    assert health.probe_remote("cloud")["online"]  # the old machine can be destroyed


def test_real_replacement_with_a_dead_candidate_keeps_the_old_cloud(ssh_lab):
    a, b = ssh_lab("a"), ssh_lab("b")
    real_enroll(a, "cloud-a", ssh_lab.user)
    before = snapshot()
    key_b = b.host_key
    b.stop()  # the new machine dies right after the user confirmed its fingerprint
    target = cloud.Target("127.0.0.1", ssh_lab.user, b.port, "cloud-b", key_b)
    with pytest.raises(CloudError):
        cloud.enroll(config.load(), cloud.GenericSSH(), target, {"endpoint": "127.0.0.1", "port": b.port, "user": ssh_lab.user, "host_key": key_b})
    target.close()
    assert snapshot() == before and whoami_via_alias() == str(a.port)


def test_changed_host_key_blocks_the_connection(ssh_lab):
    server = ssh_lab("a")
    real_enroll(server, "cloud-a", ssh_lab.user)
    server.stop()
    server.rekey()  # same address, different machine identity: MITM or a silently re-installed box
    server.start()
    report = health.probe_remote("cloud")
    assert not report["online"] and report["status"] == health.UNTRUSTED
    assert health.identity_changed(report)
    assert whoami_via_alias() == ""  # nothing is executed on the impostor
    assert server.host_key not in (paths.ssh_dir() / "suw_known_hosts").read_text()  # never auto-trusted


def test_unknown_host_is_never_trusted_silently(ssh_lab):
    server = ssh_lab("a")
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user=ssh_lab.user, ssh_port=server.port)
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    report = health.probe_remote("home")  # nobody ran `suw home trust`
    assert not report["online"] and report["status"] == health.UNTRUSTED
    known = paths.ssh_dir() / "known_hosts"
    assert not known.exists() or server.host_key.split()[1] not in known.read_text()


def test_pinned_key_mismatch_at_enrollment_aborts(ssh_lab):
    server = ssh_lab("a")
    target = cloud.Target("127.0.0.1", ssh_lab.user, server.port, "cloud-x", KEY_A + "AAAA")  # not the server's key
    with pytest.raises(CloudError, match="host identity changed|SSH connection failed"):
        cloud.GenericSSH().connect(target)
    target.close()


def test_unauthorised_key_reports_auth_required(ssh_lab):
    stranger = ssh_lab.lab / "stranger"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(stranger)], check=True)
    server = ssh_lab("locked", client_pub=stranger.with_suffix(".pub"))
    inv = inventory.load()
    inventory.cloud_assign(inv, "cloud-a", {"endpoint": "127.0.0.1", "port": server.port, "user": ssh_lab.user, "host_key": server.host_key})
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    report = health.probe_remote("cloud")
    assert report["status"] == health.AUTH_REQUIRED  # not collapsed into OFFLINE


def test_cli_enrollment_requires_the_right_fingerprint(ssh_lab, capsys):
    server = ssh_lab("a")
    base = ["cloud", "add", "--host", "127.0.0.1", "--port", str(server.port), "--user", ssh_lab.user, "--provider", "testlab", "--no-tailscale", "--profile", "none", "-y"]
    assert cli.main(base) == 1  # non-interactive and no fingerprint: refuses
    assert "host identity not confirmed" in capsys.readouterr().err
    assert cli.main([*base, "--fingerprint", "SHA256:" + "A" * 43]) == 1
    assert "does NOT match" in capsys.readouterr().err
    assert inventory.cloud_current(inventory.load()) == (None, None)

    assert cli.main([*base, "--dry-run"]) == 0
    assert "Nothing was changed" in capsys.readouterr().out and inventory.cloud_current(inventory.load()) == (None, None)

    assert cli.main([*base, "--fingerprint", server.fingerprint, "--gpu", "A100"]) == 0
    out = capsys.readouterr().out
    assert "[1/8] Validate input" in out and "[8/8] Health check" in out and "CLOUD READY" in out and server.fingerprint in out
    label, enrolled = inventory.cloud_current(inventory.load())
    assert enrolled["provider"] == "testlab" and enrolled["declared_gpu"] == "A100" and enrolled["fingerprint"] == server.fingerprint

    assert cli.main(["cloud", "status", "--json"]) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "DEGRADED" and payload["label"] == label and payload["logical_name"] == "cloud"
    assert cli.main(["cloud", "verify"]) == 0 and "identity unchanged" in capsys.readouterr().out

    server.stop()
    assert cli.main(["cloud", "status"]) == 1
    out = capsys.readouterr().out
    assert "CLOUD OFFLINE" in out and "Your local environment is unaffected." in out and "suw cloud replace" in out


# ── fixed remote scripts, executed for real in a sandbox HOME ────────────────


def run_script(name: str, home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(cloud.SCRIPTS / name), *args], env={"HOME": str(home), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, capture_output=True, text=True, timeout=120)


def test_bootstrap_script_is_idempotent(tmp_path):
    if not shutil.which("dpkg") or any(subprocess.run(["dpkg", "-s", p], capture_output=True).returncode for p in ("git", "tmux", "curl", "ca-certificates")):
        pytest.skip("needs an apt-based system with the minimal packages already present")
    home = tmp_path / "node"
    home.mkdir()
    (home / ".tmux.conf").write_text("# user line\n")
    for _ in range(5):
        done = run_script("bootstrap.sh", home, "minimal")
        assert done.returncode == 0, done.stderr
    conf = (home / ".tmux.conf").read_text()
    assert conf.count("# >>> suw managed block >>>") == 1 and conf.startswith("# user line\n")  # appended once
    log = (home / ".local/state/suw/bootstrap.log").read_text().splitlines()
    assert len(log) == 5 and all("profile=minimal version=2" in line for line in log)
    assert (home / ".local/state/suw/bootstrap.version").read_text().strip() == "2"
    assert run_script("bootstrap.sh", home, "no-such-profile").returncode != 0


def test_teardown_report_finds_work_that_would_be_lost(tmp_path):
    home = tmp_path / "node"
    repo = home / "proj"
    repo.mkdir(parents=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    (home / ".gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.com\n")
    for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "only here"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env)
    (repo / "unsaved.txt").write_text("x")
    (home / "outputs").mkdir()
    (home / "outputs" / "model.ckpt").write_text("weights")
    done = run_script("teardown_report.sh", home, "~/outputs")
    report = cloud.parse_report(done.stdout)
    mine = {k: [v for v in vals if str(home) in v] for k, vals in report.items()}
    assert mine["dirty"] and mine["noremote"] and mine["output"]
    assert cloud.is_risky(report)
    assert cloud.report_counts({**report, "dirty": mine["dirty"], "noremote": mine["noremote"], "unpushed": [], "output": mine["output"], "job": []}) == {"uncommitted": 2, "unexported": 1, "jobs": 0}
