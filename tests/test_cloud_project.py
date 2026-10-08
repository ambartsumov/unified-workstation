"""Cloud project sessions: one selected project travels, never the work folder.

The "cloud machine" is an unprivileged sshd on 127.0.0.1 whose sessions get a throw-away
directory as $HOME; rsync and the remote scripts run for real against it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import sh

from suw.cli import main as cli
from suw.cloud import project
from suw.core import config, inventory
from suw.core.config import Config, deep_merge
from suw.integrations import ssh as sshcfg

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="rsync is not installed")


def cfg_for(work: Path, **cloud) -> Config:
    return Config(deep_merge(config.load().data, {"work": {"path": str(work)}, "cloud": cloud}))


@pytest.fixture
def work(tmp_path) -> Path:
    base = tmp_path / "work"
    for rel, body in {
        "alpha/main.py": "print('alpha')\n",
        "alpha/data/Заметки 🚀.md": "строка\n",
        "alpha/.claude/settings.json": "{}",
        "alpha/.hidden": "h",
        "alpha/node_modules/pkg/index.js": "x",
        "alpha/__pycache__/m.pyc": "x",
        "beta/secret-plan.txt": "belongs to another project\n",
        "loose-note.txt": "top level of the work folder\n",
    }.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(body, encoding="utf-8")
    return base


@pytest.fixture
def cloud(ssh_lab, tmp_path, monkeypatch):
    """An enrolled cloud machine whose home directory is a sandbox folder."""
    remote_home = tmp_path / "cloud-home"
    remote_home.mkdir()
    server = ssh_lab("cloud", home=remote_home)
    inv = inventory.load()
    inventory.cloud_assign(inv, "cloud-a", {"endpoint": "127.0.0.1", "port": server.port, "user": ssh_lab.user, "host_key": server.host_key})
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    probe = project.remote_sh('printf %s "$HOME"')
    if not probe.ok or probe.out != str(remote_home):
        pytest.skip(f"this sshd does not let a session's HOME be redirected: {probe.err.strip()[-120:]}")
    # rsync's relative destination is resolved against the account's real home directory,
    # so the tests address the sandbox home explicitly.
    monkeypatch.setattr(project, "destination", lambda name: f"cloud:{remote_home}/{project.REMOTE_ROOT}/{name}/")
    return remote_home / project.REMOTE_ROOT


# ── what may be sent (pure) ─────────────────────────────────────────────────


def test_only_a_project_inside_the_work_folder_qualifies(work, tmp_path):
    cfg = cfg_for(work)
    assert project.candidates(cfg) == ["alpha", "beta"]
    assert project.locate(cfg, "alpha") == (work / "alpha").resolve()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (work / "sneaky").symlink_to(outside)
    for bad in (".", "", "..", "../elsewhere", str(work), str(outside), "/etc", "sneaky", "loose-note.txt", "alpha/../.."):
        with pytest.raises(project.ProjectError):
            project.locate(cfg, bad)
    (work / "bad name;rm").mkdir()
    with pytest.raises(project.ProjectError, match="cannot be used as a remote folder name"):
        project.locate(cfg, "bad name;rm")


def test_plan_counts_exactly_what_would_travel(work):
    cfg = cfg_for(work)
    plan = project.plan(cfg, "alpha")
    assert plan.files == 3 and plan.name == "alpha"  # main.py, the note, .hidden
    assert {".claude", "node_modules", "__pycache__"} <= set(plan.excluded)
    assert project.plan(cfg, "alpha", with_claude=True).files == 4
    (work / "alpha" / ".env").write_text("TOKEN=project-secret-77\n")
    (work / "alpha" / "deploy.pem").write_text("k")
    again = project.plan(cfg, "alpha")
    assert sorted(again.sensitive) == [".env", "deploy.pem"] and again.digest != plan.digest
    lines = "\n".join(project.summary(again, "cloud-a"))
    assert "WILL be sent" in lines and "project-secret-77" not in lines  # names, never contents
    for flag in project.excludes(cfg, with_git=True):
        assert not flag.startswith("-") and "/" not in flag


def test_confirmation_rules(work):
    cfg = cfg_for(work, project_confirm_mb=1)
    plan = project.plan(cfg, "alpha")
    assert project.needs_confirmation(cfg, plan, None) == ["first transfer of this project"]
    assert project.needs_confirmation(cfg, plan, {"state": "ok"}) == []
    (work / "alpha" / "big.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    (work / "alpha" / "id_ed25519").write_text("k")
    reasons = project.needs_confirmation(cfg, project.plan(cfg, "alpha"), {"state": "ok"})
    assert reasons == ["larger than 1 MB", "1 file(s) that look like secrets"]


def test_nothing_is_sent_without_a_cloud_machine(work):
    with pytest.raises(project.ProjectError, match="no cloud machine is enrolled"):
        project.push(cfg_for(work), "alpha")
    assert project.records() == [] or all(r["state"] != "ok" for r in project.records())


# ── real transfer ───────────────────────────────────────────────────────────


def test_push_sends_the_project_and_nothing_else(work, cloud):
    cfg = cfg_for(work)
    sh(work / "alpha", "init", "-q")
    sh(work / "alpha", "add", "main.py")
    sh(work / "alpha", "commit", "-q", "-m", "init")
    record = project.push(cfg, "alpha")
    there = cloud / "alpha"
    assert (there / "main.py").read_text() == "print('alpha')\n"
    assert (there / "data" / "Заметки 🚀.md").read_text(encoding="utf-8") == "строка\n"
    assert (there / ".hidden").exists()
    for absent in (".git", ".claude", "node_modules", "__pycache__"):
        assert not (there / absent).exists(), absent
    assert sorted(p.name for p in cloud.iterdir()) == ["alpha"]  # beta and the loose note stayed home
    manifest = json.loads((there / project.MANIFEST).read_text())
    assert manifest["revision"] == record["revision"] and len(record["revision"]) == 40
    assert manifest["uncommitted_changes"] is True and manifest["cloud"] == "cloud-a"
    assert "source" not in manifest  # the local path is nobody else's business
    assert project.status(cfg)[0]["note"] == "up to date"

    (work / "alpha" / "main.py").write_text("print('changed')\n")
    assert project.status(cfg)[0]["note"] == "changed here since the last push"
    assert project.needs_confirmation(cfg, project.plan(cfg, "alpha"), project.load("alpha")) == []

    # a plain re-push never deletes remote results; --mirror removes what is gone here
    (there / "results.txt").write_text("made on the cloud\n")
    (work / "alpha" / ".hidden").unlink()
    project.push(cfg, "alpha")
    assert (there / "results.txt").exists() and (there / ".hidden").exists()
    project.push(cfg, "alpha", mirror=True)
    assert not (there / ".hidden").exists() and not (there / "results.txt").exists()
    assert (there / project.MANIFEST).exists()  # the manifest survives a mirror


def test_pull_brings_results_back_and_keeps_replaced_files(work, cloud):
    cfg = cfg_for(work)
    project.push(cfg, "alpha")
    there = cloud / "alpha"
    (there / "out").mkdir()
    (there / "out" / "model.txt").write_text("trained\n")
    (there / "main.py").write_text("print('edited on the cloud')\n")
    (work / "alpha" / "local-only.txt").write_text("never sent back and forth\n")
    changes, target, backup = project.pull_preview(cfg, "alpha")
    assert len(changes) == 2 and target == str((work / "alpha").resolve())
    assert (work / "alpha" / "main.py").read_text() == "print('alpha')\n"  # a preview changes nothing
    done = project.pull(cfg, "alpha")
    assert (work / "alpha" / "out" / "model.txt").read_text() == "trained\n"
    assert (work / "alpha" / "main.py").read_text() == "print('edited on the cloud')\n"
    assert (work / "alpha" / "local-only.txt").exists()  # nothing local is deleted
    assert (Path(done["backup"]) / "main.py").read_text() == "print('alpha')\n"  # the replaced version is kept
    assert not (work / "alpha" / project.MANIFEST).exists()


def test_remove_deletes_only_what_suw_put_there(work, cloud):
    cfg = cfg_for(work)
    project.push(cfg, "alpha")
    foreign = cloud / "beta"
    foreign.mkdir()
    (foreign / "theirs.txt").write_text("not ours\n")
    with pytest.raises(project.ProjectError, match="was not created by"):
        project.remove(cfg, "beta")
    assert (foreign / "theirs.txt").exists()
    for bad in ("../alpha", "..", "a;b", "$(id)"):
        with pytest.raises(project.ProjectError):
            project.remove(cfg, bad)
    assert project.remove(cfg, "alpha") is True
    assert not (cloud / "alpha").exists() and (work / "alpha" / "main.py").exists()
    assert project.remove(cfg, "alpha") is False  # idempotent
    assert project.status(cfg)[0]["note"] == "removed from the cloud"
    with pytest.raises(project.ProjectError, match="has not been sent"):
        project.pull_preview(cfg, "alpha")


def test_a_replaced_cloud_machine_never_receives_an_old_projects_pull(work, cloud):
    cfg = cfg_for(work)
    project.push(cfg, "alpha")
    inv = inventory.load()
    node = dict(inventory.cloud_current(inv)[1])
    inventory.cloud_assign(inv, "cloud-b", node)
    inventory.save(inv)
    assert "no longer the cloud" in project.status(cfg)[0]["note"]
    with pytest.raises(project.ProjectError, match="no longer the cloud"):
        project.pull_preview(cfg, "alpha")


def test_failed_transfer_is_recorded_and_changes_nothing_locally(work, cloud):
    cfg = cfg_for(work)
    project.push(cfg, "alpha")
    inv = inventory.load()
    label, node = inventory.cloud_current(inv)
    inventory.cloud_assign(inv, label, {**node, "port": 1})  # nothing listens there
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    with pytest.raises(project.ProjectError):
        project.push(cfg, "alpha")
    record = project.load("alpha")
    assert record["state"] == "failed" and record["error"] and record["last_ok"] > 0
    assert (work / "alpha" / "main.py").read_text() == "print('alpha')\n"


# ── the command line ────────────────────────────────────────────────────────


def test_cli_previews_and_refuses_to_send_unasked(work, cloud, capsys, monkeypatch):
    config.set_value("work.path", str(work))
    assert cli.main(["cloud", "project", "push", "alpha", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "FILES     3" in out and "Dry run: nothing was sent." in out and not (cloud / "alpha").exists()
    assert cli.main(["cloud", "project", "push", "alpha"]) == 1  # not a terminal: the first transfer is not confirmed
    assert "Nothing was sent." in capsys.readouterr().out and not (cloud / "alpha").exists()
    assert cli.main(["cloud", "project", "push", "alpha", "--yes"]) == 0
    assert (cloud / "alpha" / "main.py").exists()
    capsys.readouterr()
    assert cli.main(["cloud", "project", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["note"] == "up to date"
    assert cli.main(["cloud", "project", "push", str(work)]) == 1
    assert "whole work folder is never sent" in capsys.readouterr().err
    assert cli.main(["cloud", "project", "remove", "alpha"]) == 1  # needs --yes without a terminal
    assert (cloud / "alpha").exists()
