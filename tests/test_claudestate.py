"""Claude Code's own notes follow the work folder only when they live inside it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from suw.cli import main as cli
from suw.core import config
from suw.core.config import Config
from suw.integrations import claudestate


@pytest.fixture
def work(sandbox) -> Path:
    base = sandbox / "Desktop" / "Work"
    (base / "demo-app" / ".git").mkdir(parents=True)
    (base / "notes").mkdir()
    (base / ".claude").mkdir()
    (base / ".claude" / "settings.json").write_text('{"env": {"PYTHONPATH": ""}}')
    config.set_value("work.path", str(base))
    return base


def cfg() -> Config:
    return config.load()


def test_projects_are_the_work_folder_and_its_repositories(work):
    assert [p.name for p in claudestate.projects(cfg())] == ["Work", "demo-app"]
    assert claudestate.portable(work / ".claude" / "memory") == "~/Desktop/Work/.claude/memory"  # same text on every machine
    for bad in ("..", "../..", "/etc", "nope"):
        with pytest.raises(claudestate.ClaudeStateError):
            claudestate.locate(cfg(), bad)


def test_share_moves_nothing_and_overwrites_nothing(work, sandbox):
    local = claudestate.local_memory(work)
    assert local.parent.parent == sandbox / ".claude" / "projects" and local.parent.name.endswith("-home-Desktop-Work") and "/" not in local.parent.name
    local.mkdir(parents=True)
    (local / "MEMORY.md").write_text("# index from this machine\n")
    (local / "fact.md").write_text("a fact\n")
    (work / ".claude" / "memory").mkdir()
    (work / ".claude" / "memory" / "MEMORY.md").write_text("# index that arrived from the other workstation\n")

    preview = claudestate.share(work, dry=True)
    assert preview["changed"] and preview["copied"] == ["fact.md"]
    assert "autoMemoryDirectory" not in (work / ".claude" / "settings.json").read_text()

    done = claudestate.share(work)
    settings = json.loads((work / ".claude" / "settings.json").read_text())
    assert settings == {"env": {"PYTHONPATH": ""}, "autoMemoryDirectory": "~/Desktop/Work/.claude/memory"}  # existing keys kept
    assert json.loads(Path(done["backup"]).read_text()) == {"env": {"PYTHONPATH": ""}}
    assert (work / ".claude" / "memory" / "fact.md").read_text() == "a fact\n"
    assert (work / ".claude" / "memory" / "MEMORY.md").read_text().startswith("# index that arrived")  # never overwritten
    assert (local / "fact.md").exists() and (local / "MEMORY.md").exists()  # the originals stay
    again = claudestate.share(work)
    assert not again["changed"] and again["copied"] == [] and again["backup"] == ""
    assert claudestate.row(work)["shared"] is True and claudestate.row(work / "demo-app")["shared"] is False


def test_foreign_or_broken_settings_are_left_alone(work):
    (work / "demo-app" / ".claude").mkdir()
    file = work / "demo-app" / ".claude" / "settings.json"
    file.write_text('{"autoMemoryDirectory": "~/somewhere/else"}')
    with pytest.raises(claudestate.ClaudeStateError, match="already sets"):
        claudestate.share(work / "demo-app")
    file.write_text("{ not json")
    with pytest.raises(claudestate.ClaudeStateError, match="not valid JSON"):
        claudestate.share(work / "demo-app")
    assert file.read_text() == "{ not json"
    assert claudestate.row(work / "demo-app")["error"]


def test_cli(work, capsys):
    assert cli.main(["work", "claude"]) == 0
    out = capsys.readouterr().out
    assert "this machine only" in out and "suw work claude share" in out
    assert cli.main(["work", "claude", "share", "--dry-run"]) == 0
    assert "autoMemoryDirectory" not in (work / ".claude" / "settings.json").read_text()
    assert cli.main(["work", "claude", "share:demo-app"]) == 0
    assert json.loads((work / "demo-app" / ".claude" / "settings.json").read_text()) == {"autoMemoryDirectory": "~/Desktop/Work/demo-app/.claude/memory"}
    assert cli.main(["work", "claude", "share"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "claude", "--json"]) == 0
    assert [row["shared"] for row in json.loads(capsys.readouterr().out)] == [True, True]
    assert cli.main(["work", "claude", "share:../.."]) == 1
