"""The shared work folder.

The first half tests the policy as pure functions. The second half starts two real Syncthing
instances on 127.0.0.1 ("ubuntu" and "mac", each with its own home directory and folder) and
drives the scenarios of the workspace contract end to end. Nothing here sleeps blindly: every
wait polls for the exact condition it needs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import _free_port, sh

from suw.core import config, state
from suw.core.config import Config, deep_merge
from suw.integrations import worksync
from suw.integrations.worksync import Instance, Peer


def cfg_for(folder: Path, **work) -> Config:
    return Config(deep_merge(config.load().data, {"work": {"path": str(folder), **work}}))


# ── policy ──────────────────────────────────────────────────────────────────


def test_everything_is_shared_except_what_is_unsafe_to_copy(tmp_path):
    work = tmp_path / "work"
    for rel in ("proj/.git/HEAD", "proj/src/app.py", "proj/.env", "proj/.claude/settings.json", "proj/node_modules/x/i.js", "proj/.venv/bin/python", "notes/.hidden", "keys/deploy.pem"):
        file = work / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("x")
    found = worksync.scan(cfg_for(work))
    ignored = {item["path"] for item in found.ignored}
    assert ignored == {"proj/.git", "proj/node_modules", "proj/.venv"}
    assert all(item["reason"] for item in found.ignored)
    assert found.files == 5  # .env, .claude, hidden files and keys are part of the workspace
    assert found.repos == ["proj"]


def test_scan_reports_what_cannot_be_shared(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "Readme.md").write_text("a")
    (work / "README.md").write_text("b")
    os.mkfifo(work / "pipe")
    (work / "big.bin").write_bytes(b"0" * (2 * 1048576))
    (work / "notes.sync-conflict-20260101-120000-ABCDEFG.md").write_text("theirs")
    found = worksync.scan(cfg_for(work, large_file_mb=1))
    reasons = {item["path"]: item["reason"] for item in found.unsupported}
    assert "letter case" in reasons["Readme.md"] and "named pipe" in reasons["pipe"]
    assert found.large == [{"path": "big.bin", "mb": 2}]
    assert found.conflicts[0]["original"] == "notes.md"
    assert worksync.held_large(cfg_for(work, large_file_mb=1), found) == ["big.bin"]
    assert worksync.held_large(cfg_for(work, large_file_mb=1, large_allow=["big.bin"]), found) == []
    assert worksync.held_large(cfg_for(work, large_file_mb=1, large_file_policy="sync"), found) == []


def test_original_of():
    assert worksync.original_of("a/b.sync-conflict-20260101-120000-ABCDEFG.txt") == "a/b.txt"
    assert worksync.original_of("Makefile.sync-conflict-20260101-120000-ABCDEFG") == "Makefile"


def test_ignore_block_is_managed_and_user_lines_survive(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    cfg = cfg_for(work)
    assert worksync.write_ignore(cfg, []) is True
    assert worksync.write_ignore(cfg, []) is False  # idempotent
    file = work / ".stignore"
    file.write_text(file.read_text() + "my-scratch\n")
    assert worksync.write_ignore(cfg, ["ex[eriment/huge*.bin"]) is True
    text = file.read_text()
    assert "my-scratch" in text and text.count(worksync.BEGIN) == 1
    assert "/ex\\[eriment/huge\\*.bin" in text  # literal path, not a pattern
    assert "(?d).git\n" in text and ".env" not in text and ".claude" not in text
    assert "(?d).venv" not in worksync.render_ignore(cfg_for(work, sync_anyway=[".venv"]), [])
    assert "(?d)scratch" in worksync.render_ignore(cfg_for(work, ignore=["scratch"]), [])


def test_first_pairing_comparison(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for base, files in ((a, {"same.txt": "1", "diff.txt": "ubuntu", "only-a.txt": "x"}), (b, {"same.txt": "1", "diff.txt": "mac", "only-b.txt": "y", ".git/HEAD": "ref"})):
        for rel, body in files.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
    result = worksync.compare(worksync.manifest(a, cfg_for(a)), worksync.manifest(b, cfg_for(b)))
    assert result == {"only_here": 1, "only_there": 1, "identical": 1, "differing": ["diff.txt"]}


def test_resolving_a_conflict_never_deletes_a_version(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    cfg = cfg_for(work)
    copy = "notes.sync-conflict-20260101-120000-ABCDEFG.md"
    (work / "notes.md").write_text("mine")
    (work / copy).write_text("theirs")
    saved = Path(worksync.resolve_conflict(cfg, copy, "conflict"))
    assert (work / "notes.md").read_text() == "theirs" and saved.read_text() == "mine" and not (work / copy).exists()
    (work / copy).write_text("again")
    saved = Path(worksync.resolve_conflict(cfg, copy, "current"))
    assert (work / "notes.md").read_text() == "theirs" and saved.read_text() == "again"
    with pytest.raises(ValueError):
        worksync.resolve_conflict(cfg, "notes.md", "current")
    with pytest.raises(ValueError):
        worksync.resolve_conflict(cfg, "../outside.sync-conflict-1-2-3.md", "current")


def test_restore_keeps_an_existing_file(tmp_path):
    work = tmp_path / "work"
    (work / ".stversions" / "docs").mkdir(parents=True)
    (work / ".stversions" / "docs" / "plan.md").write_text("old")
    (work / "docs").mkdir()
    (work / "docs" / "plan.md").write_text("new")
    cfg = cfg_for(work)
    assert [v["path"] for v in worksync.versions(cfg)] == ["docs/plan.md"]
    target = Path(worksync.restore(cfg, "docs/plan.md"))
    assert target.read_text() == "old" and target.name.startswith("plan.restored-")
    assert (work / "docs" / "plan.md").read_text() == "new"
    with pytest.raises(ValueError):
        worksync.restore(cfg, "../../etc/passwd")


def test_a_synced_working_tree_gets_its_own_git_database(remote_pair, tmp_path):
    """.git is never copied. The other workstation receives the files and attaches the history."""
    origin, ubuntu, _mac = remote_pair
    work_u, work_m = ubuntu.parent, tmp_path / "macwork"
    (ubuntu / "wip.txt").write_text("uncommitted work that arrived through file sync\n")
    cfg_u = Config(deep_merge(cfg_for(work_u).data, {"device": {"name": "ubuntu"}}))
    assert worksync.publish_repos(cfg_u, ["proj"]) is True
    assert worksync.publish_repos(cfg_u, ["proj"]) is False
    # What file sync delivers to the Mac: everything but .git.
    shutil.copytree(work_u, work_m, ignore=shutil.ignore_patterns(".git"))
    cfg_m = Config(deep_merge(cfg_for(work_m).data, {"device": {"name": "mac"}}))
    todo = worksync.detached_repos(cfg_m)
    assert [(t["path"], t["url"], t["branch"], t["from"]) for t in todo] == [("proj", str(origin), "main", "ubuntu")]
    ok, note = worksync.attach_repo(cfg_m, "proj", todo[0]["url"], todo[0]["branch"])
    assert ok, note
    mac = work_m / "proj"
    assert sh(mac, "rev-parse", "HEAD") == sh(ubuntu, "rev-parse", "HEAD")
    assert sh(mac, "status", "--porcelain") == "?? wip.txt"  # same view as on Ubuntu, nothing touched
    assert (mac / "wip.txt").read_text().startswith("uncommitted")
    assert sh(mac, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/main"
    assert worksync.detached_repos(cfg_m) == []
    assert worksync.attach_repo(cfg_m, "proj", "--upload-pack=x", "main")[0] is True  # already attached: no-op
    assert worksync.attach_repo(cfg_m, "missing", "--upload-pack=x", "main")[0] is False


def test_pairing_needs_local_approval(sandbox):
    from suw.core import inventory

    inv = inventory.load()
    inventory.ensure_device(inv, "mac", tailscale_name="workstation-mac", syncthing_id="MAC-ID")
    inventory.save(inv)
    cfg = config.load()
    assert worksync.peers(cfg) == [] and worksync.unpaired(cfg) == [("mac", "MAC-ID")]
    worksync.approve("MAC-ID")
    assert worksync.peers(cfg) == [Peer("mac", "MAC-ID", ["tcp://workstation-mac:22000", "dynamic"])]
    worksync.revoke("MAC-ID")
    assert worksync.peers(cfg) == []


# ── two real Syncthing instances ────────────────────────────────────────────


class Station:
    def __init__(self, base: Path, name: str):
        self.name = name
        self.work = base / name / "work"
        self.work.mkdir(parents=True)
        self.port = _free_port()
        # SUW_TEST_SYNCTHING_PEER=/path/to/another/syncthing runs every station except "ubuntu"
        # on that binary: the mixed-version pairing a real Ubuntu + Mac setup ends up with.
        other = os.environ.get("SUW_TEST_SYNCTHING_PEER", "") if name != "ubuntu" else ""
        self.inst = Instance(base / name / "st", self.work, gui=f"127.0.0.1:{_free_port()}", listen=[f"tcp://127.0.0.1:{self.port}"], name=name, local_discovery=False, exe=other)
        # Retry a lost peer every 5 s instead of every 60 s: only the waiting time of the
        # reconnect scenarios changes. A first dial that loses a race with the teardown of the
        # previous connection would otherwise cost a full minute.
        self.inst.reconnect_s = 5
        self.inst.generate()
        self.id = self.inst.device_id()
        self.proc = None
        self.log = base / name / "syncthing.log"
        self.cfg = cfg_for(self.work)

    def peer(self) -> Peer:
        return Peer(self.name, self.id, [f"tcp://127.0.0.1:{self.port}"])

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ST")}
        self.proc = subprocess.Popen(self.inst.serve_command(), stdout=self.log.open("a"), stderr=subprocess.STDOUT, env=env)
        assert self.inst.wait_running(40), self.log.read_text()[-2000:]

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def write(self, rel: str, body: str) -> None:
        file = self.work / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.work / rel).read_text(encoding="utf-8")


def until(condition, seconds: float = 60.0, what: str = "") -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for: {what}")


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    if not worksync.binary():
        pytest.skip("syncthing is not installed")
    base = tmp_path_factory.mktemp("stations")
    ubuntu, mac = Station(base, "ubuntu"), Station(base, "mac")
    for station, other in ((ubuntu, mac), (mac, ubuntu)):
        worksync.write_ignore(station.cfg, [], station.work)
        assert station.inst.apply([other.peer()]) is True
        assert station.inst.apply([other.peer()]) is False  # idempotent
        station.start()
    yield ubuntu, mac
    ubuntu.stop()
    mac.stop()


def settle(*stations: Station, seconds: float = 90.0) -> None:
    for station in stations:
        station.inst.rescan()

    def quiet() -> bool:
        snaps = [s.inst.snapshot() for s in stations]
        return all(s["state"] == "IN_SYNC" and s["peers"][0]["completion"] == 100 for s in snaps)

    until(quiet, seconds, "both workstations in sync")


def test_live_workspace_contract(pair):
    ubuntu, mac = pair
    settle(ubuntu, mac)
    assert ubuntu.inst.snapshot()["peers"][0]["name"] == "mac"

    # 1-4  a file created on Ubuntu reaches the Mac; the Mac's edit comes back
    ubuntu.write("notes.md", "written on ubuntu\n")
    settle(ubuntu, mac)
    assert mac.read("notes.md") == "written on ubuntu\n"
    mac.write("notes.md", "edited on the mac\n")
    settle(ubuntu, mac)
    assert ubuntu.read("notes.md") == "edited on the mac\n"

    # 5-7, 11-13  nested directories, hidden files, dot-directories, Claude state, secrets, Unicode
    text = "строка один\nline two — ñ é ü\n日本語 🚀\n"
    ubuntu.write("a/b/c/deep.txt", "deep")
    ubuntu.write(".hidden", "h")
    ubuntu.write("proj/.claude/settings.json", '{"memory": true}')
    ubuntu.write("proj/.claude/memory/MEMORY.md", "# память проекта\n")
    ubuntu.write("proj/.env", "API_TOKEN=workspace-secret-value-91\n")
    ubuntu.write("proj/pyproject.toml", "[project]\nname='p'\n")
    ubuntu.write("Заметки/план 🚀.md", text)
    # ...and what must stay on the machine that made it
    ubuntu.write("proj/.git/HEAD", "ref: refs/heads/main\n")
    ubuntu.write("proj/node_modules/pkg/index.js", "x")
    ubuntu.write("proj/.venv/pyvenv.cfg", "home = /usr/bin")
    ubuntu.write("proj/__pycache__/m.pyc", "x")
    settle(ubuntu, mac)
    for rel in ("a/b/c/deep.txt", ".hidden", "proj/.claude/settings.json", "proj/.claude/memory/MEMORY.md", "proj/.env", "proj/pyproject.toml"):
        assert mac.read(rel) == ubuntu.read(rel), rel
    assert mac.read("Заметки/план 🚀.md") == text
    for rel in ("proj/.git", "proj/node_modules", "proj/.venv", "proj/__pycache__"):
        assert not (mac.work / rel).exists(), rel

    # 8-9  rename a file, move a directory
    os.rename(ubuntu.work / "notes.md", ubuntu.work / "journal.md")
    os.rename(ubuntu.work / "a", ubuntu.work / "moved")
    settle(ubuntu, mac)
    assert mac.read("journal.md") == "edited on the mac\n" and not (mac.work / "notes.md").exists()
    assert mac.read("moved/b/c/deep.txt") == "deep" and not (mac.work / "a").exists()

    # 10  a deletion propagates, and the deleted file is still recoverable
    (ubuntu.work / "journal.md").unlink()
    settle(ubuntu, mac)
    assert not (mac.work / "journal.md").exists()
    saved = worksync.versions(mac.cfg)
    assert "journal.md" in [v["path"] for v in saved]
    restored = Path(worksync.restore(mac.cfg, "journal.md"))
    assert restored.read_text() == "edited on the mac\n"
    settle(ubuntu, mac)
    assert ubuntu.read("journal.md") == "edited on the mac\n"  # the rescue travels back too

    # 14-17  offline edits on either side are delivered after the reconnect
    mac.stop()
    ubuntu.write("offline-ubuntu.txt", "made while the mac was off\n")
    ubuntu.inst.rescan()
    until(lambda: ubuntu.inst.snapshot()["state"] in ("PEER_OFFLINE", "SYNCING"), 30, "ubuntu notices the peer is gone")
    assert ubuntu.inst.snapshot()["peers"][0]["connected"] is False
    mac.write("offline-mac.txt", "made while switched off from the network\n")
    mac.start()  # 20-22: the service restarts and picks the queue up by itself
    settle(ubuntu, mac)
    assert mac.read("offline-ubuntu.txt").startswith("made while the mac")
    assert ubuntu.read("offline-mac.txt").startswith("made while switched off")

    # 18-19  the same file changed on both sides: both versions survive
    ubuntu.write("shared.txt", "base\n")
    settle(ubuntu, mac)
    mac.stop()
    ubuntu.write("shared.txt", "ubuntu version\n")
    ubuntu.inst.rescan()
    time.sleep(1.1)  # the two edits need distinct modification times
    mac.write("shared.txt", "mac version\n")
    mac.start()
    settle(ubuntu, mac)
    # "in sync" can be reported a moment before the conflict copy has reached the other side
    until(lambda: all(len(worksync.scan(station.cfg).conflicts) == 1 for station in (ubuntu, mac)), 60, "the conflict copy reaches both sides")
    for station in (ubuntu, mac):
        found = worksync.scan(station.cfg)
        assert len(found.conflicts) == 1 and found.conflicts[0]["original"] == "shared.txt"
        versions = {station.read("shared.txt"), station.read(found.conflicts[0]["path"])}
        assert versions == {"ubuntu version\n", "mac version\n"}
    status = worksync.Instance.snapshot(ubuntu.inst)
    assert status["state"] == "IN_SYNC"

    # pause / resume
    assert ubuntu.inst.set_paused(True)
    until(lambda: ubuntu.inst.snapshot()["state"] == "PAUSED", 20, "paused")
    ubuntu.write("while-paused.txt", "p")
    time.sleep(1)
    assert not (mac.work / "while-paused.txt").exists()
    assert ubuntu.inst.set_paused(False)
    settle(ubuntu, mac)
    assert mac.read("while-paused.txt") == "p"

    # 23  the secret's content never appears in the sync engine's log
    for station in (ubuntu, mac):
        assert "workspace-secret-value-91" not in station.log.read_text(errors="replace")
    # 24  machine identity stays put: each instance has its own key, outside the folder
    assert ubuntu.id != mac.id and not list(ubuntu.work.rglob("key.pem"))
    assert oct((ubuntu.inst.home / "config.xml").stat().st_mode & 0o777) == "0o600"


def test_live_instance_is_private(pair):
    ubuntu, _mac = pair
    options = (ubuntu.inst.api("GET", "/rest/config") or {})["options"]
    assert options["globalAnnounceEnabled"] is False and options["relaysEnabled"] is False and options["natEnabled"] is False
    assert options["urAccepted"] == -1 and options["startBrowser"] is False
    gui = (ubuntu.inst.api("GET", "/rest/config") or {})["gui"]
    assert gui["address"].startswith("127.0.0.1:")
    folder = (ubuntu.inst.api("GET", "/rest/config") or {})["folders"][0]
    assert folder["versioning"]["type"] == "trashcan" and folder["maxConflicts"] == -1 and folder["type"] == "sendreceive"
    # Without the API key the local API answers nothing useful.
    import urllib.error
    import urllib.request

    with pytest.raises(urllib.error.HTTPError):
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://{ubuntu.inst.gui}/rest/system/status", timeout=5)


def test_status_words(sandbox, tmp_path):
    cfg = cfg_for(tmp_path / "nowhere")
    report = worksync.status(cfg)
    assert report["state"] in ("NOT_CONFIGURED", "NOT_INSTALLED") and report["exists"] is False
    assert worksync.status(cfg_for(tmp_path, enabled=False))["state"] == "DISABLED"
    assert set(worksync.WORDS) >= {"IN_SYNC", "SYNCING", "PEER_OFFLINE", "PAUSED", "CONFLICTS", "ERROR", "NO_PEER", "STOPPED"}
    assert state.load("work") == {}


# ── Git inside the shared folder ────────────────────────────────────────────


def test_history_catches_up_when_file_sync_delivered_the_files_first(remote_pair):
    """Ubuntu commits and pushes. The Mac already has the new files (file sync) but its own
    Git database is one commit behind: the branch moves, no file is rewritten."""
    from suw.core import gitsync

    origin, ubuntu, mac = remote_pair
    (ubuntu / "a.txt").write_text("two\n")
    (ubuntu / "new.txt").write_text("fresh\n")
    sh(ubuntu, "add", "-A"), sh(ubuntu, "commit", "-qm", "work"), sh(ubuntu, "push", "-q")
    (mac / "a.txt").write_text("two\n")  # what file sync put there
    (mac / "new.txt").write_text("fresh\n")
    (mac / "mine.txt").write_text("local scratch, not committed anywhere\n")
    before = {p.name: p.stat().st_mtime_ns for p in mac.iterdir() if p.is_file()}
    policy = dict(gitsync.project_policy(mac, config.load().data), enabled=False, adopt=True)
    outcome = gitsync.reconcile(mac, policy)
    assert outcome.changed and sh(mac, "rev-parse", "HEAD") == sh(origin, "rev-parse", "main")
    assert sh(mac, "status", "--porcelain") == "?? mine.txt"
    assert {p.name: p.stat().st_mtime_ns for p in mac.iterdir() if p.is_file()} == before  # nothing was written


def test_history_waits_while_file_sync_is_still_delivering(remote_pair):
    from suw.core import gitsync

    _origin, ubuntu, mac = remote_pair
    (ubuntu / "a.txt").write_text("two\n")
    (ubuntu / "b.txt").write_text("b\n")
    sh(ubuntu, "add", "-A"), sh(ubuntu, "commit", "-qm", "work"), sh(ubuntu, "push", "-q")
    (mac / "a.txt").write_text("a different local edit\n")  # not what the commit brings
    head = sh(mac, "rev-parse", "HEAD")
    policy = dict(gitsync.project_policy(mac, config.load().data), enabled=False, adopt=True)
    outcome = gitsync.reconcile(mac, policy)
    assert outcome.status.value == "REMOTE_AHEAD" and sh(mac, "rev-parse", "HEAD") == head
    assert (mac / "a.txt").read_text() == "a different local edit\n"
    assert gitsync.adopt_delivered(mac) is False


def test_repositories_in_the_work_folder_are_not_auto_committed(remote_pair, sandbox):
    from suw.core import syncer

    _origin, ubuntu, _mac = remote_pair
    base = Config(deep_merge(config.load().data, {"projects": {"roots": [], "extra": [str(ubuntu)]}}))
    inside = Config(deep_merge(base.data, {"work": {"path": str(ubuntu.parent)}}))
    outside = Config(deep_merge(base.data, {"work": {"path": str(ubuntu.parent / "elsewhere")}}))
    assert [(p.name, pol["enabled"], pol.get("adopt")) for p, pol in syncer.managed(inside)] == [("proj", False, True)]
    assert [(p.name, pol["enabled"], pol.get("adopt")) for p, pol in syncer.managed(outside)] == [("proj", True, None)]
    opted = Config(deep_merge(inside.data, {"work": {"git_autosync": True}}))
    assert syncer.managed(opted)[0][1]["enabled"] is True
