"""The CLI contract: machine-readable output, dry runs, install/rollback invariants."""

from __future__ import annotations

import json
import shutil
import tarfile

import pytest
from conftest import sh

from suw.cli import main as cli
from suw.core import bootstrap, config, doctor, events, handoff, journal, paths, status
from suw.ui import text

COMPONENT = {"ONLINE", "OFFLINE", "DEGRADED", "SYNCING", "ERROR", "UNKNOWN", "NOT_CONFIGURED", "AUTH_REQUIRED", "UNTRUSTED"}
SAFE_STEPS = ["dirs", "config", "launcher", "shell", "git", "ssh", "terminal", "prompt"]  # no real services / desktop


def run_json(capsys, *argv: str):
    capsys.readouterr()
    code = cli.main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def project(sandbox, remote_pair):
    _origin, ubuntu, _mac = remote_pair
    target = sandbox / "Projects" / "active" / "demo"
    shutil.copytree(ubuntu, target)
    return target


def test_every_major_command_speaks_json(capsys, project, monkeypatch):
    monkeypatch.chdir(project)
    code, snap = run_json(capsys, "status")
    assert code == 0 and snap["overall"] in ("READY", "NEEDS ATTENTION") and snap["mode"] == "DEFAULT"
    assert set(snap["components"].values()) <= COMPONENT
    assert {snap["servers"]["home"]["status"], snap["servers"]["cloud"]["status"]} == {"NOT_CONFIGURED"}
    assert snap["sync"]["managed"] == 1 and snap["sync"]["conflicts"] == 0

    _, sync = run_json(capsys, "sync", "status")
    assert sync["summary"]["managed"] == 1 and sync["projects"][0]["name"] == "demo"
    _, row = run_json(capsys, "project", "status")
    assert row["name"] == "demo" and row["managed"] is True and row["autosync"] is True
    _, listed = run_json(capsys, "project", "list")
    assert listed == [{"name": "demo", "path": str(project), "autosync": True, "policy": "manual"}]
    code, cloud = run_json(capsys, "cloud", "status")
    assert code == 0 and cloud == {"status": "NOT_CONFIGURED", "label": "", "configured": False}
    _, home = run_json(capsys, "home", "status")
    assert home == {"status": "NOT_CONFIGURED"}
    _, valid = run_json(capsys, "config", "validate")
    assert valid == {"valid": True, "errors": [], "warnings": []}
    _, recovery = run_json(capsys, "project", "recovery", "demo")
    assert recovery["local_head"] and recovery["unpushed"] == [] and recovery["uncommitted"] == []
    _, arts = run_json(capsys, "artifact", "list")
    assert arts["artifacts"] == []
    _, clip = run_json(capsys, "clipboard", "status")
    assert {"local", "ssh", "terminal", "tmux"} <= set(clip)

    events.emit("sync.push", "demo: pushed 1 commit(s)", project="demo")
    events.emit("cloud.enrolled", "cloud role -> cloud-1")
    _, history = run_json(capsys, "history", "--sync")
    assert [r["event"] for r in history] == ["sync.push"]
    _, history = run_json(capsys, "history", "--project", "demo")
    assert len(history) == 1 and history[0]["project"] == "demo"
    assert cli.main(["history", "--cloud"]) == 0 and "cloud role -> cloud-1" in capsys.readouterr().out


def test_doctor_sections_verdict_and_optional_components(capsys, project):
    code, report = run_json(capsys, "doctor")
    sections = {c["section"] for c in report["checks"]}
    assert sections == {"Workstation", "Sync", "Work folder", "Peripherals", "Network", "Clipboard", "Security", "Startup", "Recovery"}
    assert {c["level"] for c in report["checks"]} <= {"PASS", "WARN", "FAIL", "NONE"}
    titles = {c["title"]: c for c in report["checks"]}
    # Optional components that are simply absent never count against the verdict.
    assert titles["Cloud is not configured"]["level"] == "NONE" and titles["Home server is not configured"]["level"] == "NONE"
    assert all(c["fix"] for c in report["checks"] if c["level"] in ("WARN", "FAIL"))
    assert report["overall"] == doctor.verdict([doctor.Check(**c) for c in report["checks"]])
    assert doctor.verdict([doctor.Check(doctor.PASS, "a"), doctor.Check(doctor.NONE, "MacBook has not been enrolled")]) == "READY"
    assert doctor.verdict([doctor.Check(doctor.WARN, "w")]) == "READY WITH WARNINGS"
    assert doctor.verdict([doctor.Check(doctor.FAIL, "f")]) == "NOT READY"
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "Overall: " in out and "Notes:" in out and "✓ PASS   ⚠ WARNING   ✗ FAIL   ○ NOT CONFIGURED" in out


def test_status_text_reassures_when_a_server_is_down(project):
    snap = status.build(config.load())
    snap["servers"]["cloud"].update(configured=True, status="OFFLINE")
    snap["attention"], snap["still_works"] = status.attention(snap), status.still_works(snap)
    summary = text.render_summary(snap)
    assert "cloud       ○ OFFLINE" in summary and "NEEDS ATTENTION" in summary and "Unaffected: local work" in summary
    panel = text.render_status(snap)
    for block in ("MODE", "MACHINES", "THIS MACHINE", "SYNC", "NETWORK", "CLIPBOARD"):
        assert block in panel
    assert len(panel.splitlines()) < 40  # compact


def test_config_set_validates_and_supports_dry_run(capsys):
    assert cli.main(["config", "set", "sync.policy", "yolo"]) == 1
    assert "nothing was changed" in capsys.readouterr().err and not config.local_file().exists()
    assert cli.main(["config", "set", "cloud.api_token", "abcdef123456"]) == 1  # secrets never land in config
    assert cli.main(["config", "set", "sync.fetch_seconds", "600", "--dry-run"]) == 0
    assert config.load().get("sync.fetch_seconds") == 300
    assert cli.main(["config", "set", "urls.docs", "https://docs.example"]) == 0
    assert config.load().url("docs") == "https://docs.example"
    capsys.readouterr()
    assert cli.main(["config", "diff"]) == 0 and "urls.docs" in capsys.readouterr().out


def test_install_twice_equals_once_and_rollback_restores_user_files(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "have", lambda tool: False)  # do not reach the real tmux server
    originals = {".zshrc": "export EDITOR=vim\nalias ll='ls -l'\n", ".bashrc": "# my bash\n", ".ssh/config": "Host work\n    User me\n"}
    for name, body in originals.items():
        (sandbox / name).parent.mkdir(parents=True, exist_ok=True)
        (sandbox / name).write_text(body)
    gitconfig_before = (sandbox / ".gitconfig").read_text()

    assert cli.main(["bootstrap", "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert "~/.zshrc" in preview and "~/.ssh/config" in preview and "Nothing was changed" in preview
    assert (sandbox / ".zshrc").read_text() == originals[".zshrc"]  # the preview really changed nothing

    first = dict(bootstrap.run_all(SAFE_STEPS))
    after_first = {name: (sandbox / name).read_text() for name in originals}
    device_id = config.load().get("device.id")
    second = dict(bootstrap.run_all(SAFE_STEPS))
    assert not any(v.startswith("FAILED") for v in [*first.values(), *second.values()])
    assert {name: (sandbox / name).read_text() for name in originals} == after_first  # idempotent
    assert all(text_.count(journal.BEGIN) == 1 for text_ in after_first.values())
    assert config.load().get("device.id") == device_id and device_id.startswith("ubuntu-") and len(device_id) > len("ubuntu-") + 6
    for name, body in originals.items():
        assert body.strip() in after_first[name]  # user content preserved inside the modified files

    assert cli.main(["rollback", "--dry-run"]) == 0 and "Dry run" in capsys.readouterr().out
    assert {name: (sandbox / name).read_text() for name in originals} == after_first
    assert cli.main(["uninstall", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "The following SUW-owned resources will be removed:" in out and "The following user resources will remain:" in out
    assert cli.main(["rollback", "--yes"]) == 0
    for name, body in originals.items():
        assert (sandbox / name).read_text().strip() == body.strip(), name  # back to the user's own content
    assert (sandbox / ".gitconfig").read_text() == gitconfig_before
    assert not (sandbox / ".local/bin/suw").exists() and not (sandbox / ".ssh/suw.conf").exists()
    assert (sandbox / "Projects" / "active").is_dir()  # user directories are never removed


def test_handoff_bundle_contains_no_secrets(sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "have", lambda tool: False)
    bootstrap.run_all(["dirs", "config"])
    (sandbox / ".ssh").mkdir(exist_ok=True)
    (sandbox / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
    sh(paths.shared_dir(), "remote", "add", "origin", "https://me:hunter2token@github.com/me/shared-config.git")
    bundle = handoff.build(config.load(), tmp_path / "handoff.tar.gz")
    with tarfile.open(bundle) as archive:
        names = sorted(archive.getnames())
        installer = archive.extractfile("suw-handoff/Install Workstation.command").read().decode()
    assert names == ["suw-handoff", "suw-handoff/Install Workstation.command", "suw-handoff/READ ME FIRST.md", "suw-handoff/repo.bundle", "suw-handoff/shared.bundle"]
    assert "hunter2token" not in installer and "PRIVATE KEY" not in installer
    assert oct(bundle.stat().st_mode & 0o777) == "0o600"


def test_no_command_execution_path_uses_a_shell_or_disables_verification():
    """Static guard over the whole package (documentation strings included)."""
    import re

    root = paths.REPO_ROOT / "suw"
    offenders = []
    for path in root.rglob("*.py"):
        body = path.read_text()
        if re.search(r"shell\s*=\s*True", body) or re.search(r"os\.system\(|os\.popen\(", body):
            offenders.append(f"{path.name}: shell execution")
        if re.search(r"StrictHostKeyChecking[= ](no|off|accept-new)", body):
            offenders.append(f"{path.name}: host key checking weakened")
        for line in body.splitlines():
            if re.search(r'"push"', line) and re.search(r"--force|\"-f\"|\+refs", line):
                offenders.append(f"{path.name}: forced push")
            if re.search(r'"reset",\s*"--hard"|"clean",\s*"-f|"branch",\s*"-D"|"checkout",\s*"--",', line):
                offenders.append(f"{path.name}: destructive git command")
    assert offenders == []
