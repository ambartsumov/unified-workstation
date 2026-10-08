"""End-to-end scenarios driven through the daemon's own decision loop.

Two workstations ("ubuntu" and "mac") share one bare repository that stands in for
GitHub. Time is injected (FakeClock): nothing here sleeps.
"""

from __future__ import annotations

import pytest
from conftest import MANUAL, FakeClock, sh

from suw.core import config, events, gitsync, state
from suw.core.config import Config, deep_merge
from suw.daemon.suwd import Daemon, backoff

AUTOSYNC = dict(MANUAL, idle_seconds=20, min_interval_seconds=0, max_wait_seconds=900)


class Workstation:
    """One machine: its clone, its daemon, its clock."""

    def __init__(self, name: str, repo, clock: FakeClock):
        self.name, self.repo, self.clock = name, repo, clock
        self.daemon = Daemon(clock=clock)
        self.cfg = Config(deep_merge(config.load().data, {"device": {"name": name}}))

    def tick(self, seconds: float = 0, policy: dict = AUTOSYNC, force: bool = False) -> dict:
        self.clock.advance(seconds)
        return self.daemon._sync_one(self.cfg, self.repo, policy, force)

    def boot(self) -> "Workstation":
        """Power-cycle: all in-memory daemon state is lost, on-disk state remains."""
        self.daemon.watcher.stop()
        self.daemon = Daemon(clock=self.clock)
        return self

    def settle(self, policy: dict = AUTOSYNC) -> dict:
        """Quiet period passes, checkpoint happens, push follows."""
        self.tick(1, policy)
        return self.tick(25, policy)


@pytest.fixture
def pair(remote_pair):
    origin, ubuntu, mac = remote_pair
    clock = FakeClock()
    machines = Workstation("ubuntu", ubuntu, clock), Workstation("mac", mac, clock)
    yield origin, *machines
    for machine in machines:
        machine.daemon.watcher.stop()


def head(repo) -> str:
    return sh(repo, "rev-parse", "HEAD")


def test_ubuntu_edits_while_mac_is_off_then_mac_catches_up(pair):
    origin, ubuntu, mac = pair
    (ubuntu.repo / "model.py").write_text("layers = 12\n")
    row = ubuntu.tick()
    assert row["status"] == "CHECKPOINT_PENDING"  # seen, waiting for the quiet period
    assert ubuntu.tick(5)["status"] == "CHECKPOINT_PENDING" and head(ubuntu.repo) == sh(origin, "rev-parse", "main")
    row = ubuntu.tick(20)  # quiet for 20 s -> checkpoint -> push
    assert row["status"] == "SYNCED"
    assert sh(ubuntu.repo, "log", "-1", "--format=%s").startswith("chore(suw-autosync): checkpoint ubuntu ")
    assert sh(origin, "rev-parse", "main") == head(ubuntu.repo)

    # The MacBook was off the whole time. It boots: first cycle reconciles.
    assert mac.boot().tick()["status"] == "SYNCED"
    assert head(mac.repo) == head(ubuntu.repo)
    assert (mac.repo / "model.py").read_text() == "layers = 12\n"
    assert events.read(50, kinds=("notify",)) == []  # a normal catch-up is silent


def test_mac_edits_while_ubuntu_is_off_then_ubuntu_catches_up(pair):
    origin, ubuntu, mac = pair
    (mac.repo / "notes.md").write_text("# written on the mac\n")
    assert mac.settle()["status"] == "SYNCED"
    assert sh(mac.repo, "log", "-1", "--format=%s").startswith("chore(suw-autosync): checkpoint mac ")
    assert ubuntu.boot().tick()["status"] == "SYNCED"
    assert head(ubuntu.repo) == head(mac.repo) == sh(origin, "rev-parse", "main")
    assert (ubuntu.repo / "notes.md").exists()


def test_both_edit_independently_ends_diverged_with_nothing_overwritten(pair):
    origin, ubuntu, mac = pair
    (ubuntu.repo / "A.txt").write_text("ubuntu wrote A\n")
    assert ubuntu.settle()["status"] == "SYNCED"
    remote_before = sh(origin, "rev-parse", "main")

    # The Mac was offline when it made its own change, so its checkpoint forks history.
    (mac.repo / "B.txt").write_text("mac wrote B\n")
    sh(mac.repo, "add", "-A"), sh(mac.repo, "commit", "-qm", "chore(suw-autosync): checkpoint mac offline")
    mac_head = head(mac.repo)
    row = mac.tick()
    assert row["status"] == "DIVERGED"
    assert head(mac.repo) == mac_head and sh(origin, "rev-parse", "main") == remote_before
    assert (mac.repo / "B.txt").read_text() == "mac wrote B\n" and not (mac.repo / "A.txt").exists()
    assert row["recovery_branch"].startswith("suw/recovery/mac/")
    assert len(events.read(50, kinds=("notify",))) == 1
    for _ in range(5):  # stays put: no retries of a merge, no notification storm, no branch pile-up
        assert mac.tick(400)["status"] == "DIVERGED"
    assert len(events.read(50, kinds=("notify",))) == 1 and len(gitsync.recovery_branches(mac.repo)) == 1
    info = gitsync.recovery_info(mac.repo)
    assert len(info["unpushed"]) == 1 and len(info["incoming"]) == 1 and info["last_synced_head"]


def test_network_outage_queues_work_and_drains_on_return(pair, tmp_path):
    origin, ubuntu, _mac = pair
    hidden = tmp_path / "github.down"
    origin.rename(hidden)
    sh(ubuntu.repo, "remote", "set-url", "origin", "https://127.0.0.1:1/unreachable.git")

    (ubuntu.repo / "one.txt").write_text("1\n")
    assert ubuntu.settle()["status"] == "OFFLINE"  # committed locally, push queued
    (ubuntu.repo / "two.txt").write_text("2\n")  # the user keeps working
    row = ubuntu.settle()
    assert row["status"] == "OFFLINE" and row["ahead"] == 2 and gitsync.collect(ubuntu.repo).clean
    key = str(ubuntu.repo)
    first_retry = ubuntu.daemon.next_fetch[key] - ubuntu.clock()
    assert ubuntu.daemon.failures[key] >= 1 and first_retry > 0
    before = len(events.read(200))
    for _ in range(4):  # polls inside the backoff window do not touch the network
        assert ubuntu.tick(1)["status"] == "OFFLINE"
    assert len(events.read(200)) == before and events.read(50, kinds=("notify",)) == []

    hidden.rename(origin)
    sh(ubuntu.repo, "remote", "set-url", "origin", str(origin))
    assert ubuntu.tick(first_retry + 1)["status"] == "SYNCED"  # the retry timer fires on its own
    assert sh(origin, "rev-parse", "main") == head(ubuntu.repo) and ubuntu.daemon.failures[key] == 0


def test_network_return_retries_at_once_instead_of_waiting_out_the_backoff(pair, tmp_path, monkeypatch):
    from suw.core import health

    origin, ubuntu, _mac = pair
    hidden = tmp_path / "github.down"
    origin.rename(hidden)
    route = {"ip": ""}
    monkeypatch.setattr(health, "route", lambda: route["ip"])
    ubuntu.daemon.watch_network(30)
    (ubuntu.repo / "one.txt").write_text("1\n")
    assert ubuntu.settle()["status"] == "OFFLINE"
    key = str(ubuntu.repo)
    for _ in range(6):  # a long outage: the pause between attempts has grown
        ubuntu.tick(ubuntu.daemon.next_fetch[key] - ubuntu.clock() + 1)
    assert ubuntu.daemon.failures[key] >= 5 and ubuntu.daemon.next_fetch[key] - ubuntu.clock() > 200
    assert ubuntu.daemon.next_fetch[key] - ubuntu.clock() <= 300 * 1.2  # never longer than the offline cap

    ubuntu.daemon.health_seen = ubuntu.clock()  # the health loop kept running: no sleep in between
    hidden.rename(origin)
    ubuntu.daemon.watch_network(30)  # same route: nothing to do
    assert ubuntu.tick(1)["status"] == "OFFLINE"
    route["ip"] = "192.168.1.20"  # Wi-Fi came back
    ubuntu.daemon.watch_network(30)
    assert ubuntu.tick(1)["status"] == "SYNCED" and ubuntu.daemon.failures[key] == 0
    assert any("network changed" in e["message"] for e in events.read(50))
    assert ubuntu.daemon.network_back("again") is False  # nothing is waiting: no event, no wake


def test_waking_up_and_a_server_answering_again_also_retry(pair, tmp_path, monkeypatch):
    from suw.core import health

    origin, ubuntu, _mac = pair
    monkeypatch.setattr(health, "route", lambda: "10.0.0.2")
    key = str(ubuntu.repo)
    hidden = tmp_path / "github.down"
    for wake in ("sleep", "server"):
        origin.rename(hidden)
        (ubuntu.repo / f"{wake}.txt").write_text("x\n")
        assert ubuntu.settle()["status"] == "OFFLINE"
        hidden.rename(origin)
        if wake == "sleep":
            ubuntu.daemon.watch_network(30)
            ubuntu.daemon.health_seen = ubuntu.clock() - 4000  # the lid was closed for an hour
            ubuntu.daemon.watch_network(30)
        else:
            cfg = config.load()
            ubuntu.daemon._track(cfg, "home", {"online": False})
            ubuntu.daemon._track(cfg, "home", {"online": True})
        assert ubuntu.tick(1)["status"] == "SYNCED", wake
        assert ubuntu.daemon.failures[key] == 0


def test_daemon_restart_resumes_the_queue(pair, tmp_path):
    origin, ubuntu, _mac = pair
    hidden = tmp_path / "github.down"
    origin.rename(hidden)
    (ubuntu.repo / "queued.txt").write_text("q\n")
    assert ubuntu.settle()["status"] == "OFFLINE"
    hidden.rename(origin)
    assert ubuntu.boot().tick()["status"] == "SYNCED"  # nothing in memory was needed
    assert sh(origin, "rev-parse", "main") == head(ubuntu.repo)


def test_backoff_grows_and_is_capped():
    import random

    rng = random.Random(7)
    delays = [backoff(n, 1800, rng) for n in range(1, 12)]
    assert 24 <= delays[0] <= 36 and delays[3] > delays[0] * 4
    assert max(delays) <= 1800 * 1.2
    assert len({round(backoff(3, 1800, rng), 3) for _ in range(5)}) > 1  # jitter


def test_continuous_typing_still_checkpoints_at_max_wait(pair):
    _origin, ubuntu, _mac = pair
    policy = dict(AUTOSYNC, max_wait_seconds=60)
    for second in range(12):  # a save every 10 s for two minutes: never quiet for 20 s
        (ubuntu.repo / "draft.txt").write_text(f"revision {second}\n")
        row = ubuntu.tick(10, policy)
    assert sh(ubuntu.repo, "log", "--format=%s", "origin/main").count("chore(suw-autosync)") >= 1
    assert row["status"] in ("SYNCED", "CHECKPOINT_PENDING")


def test_held_back_file_does_not_cause_a_commit_loop(pair):
    _origin, ubuntu, _mac = pair
    (ubuntu.repo / ".env").write_text("TOKEN=x\n")
    (ubuntu.repo / "ok.txt").write_text("fine\n")
    row = ubuntu.settle()
    assert row["status"] == "POLICY_BLOCKED" and row["held"][0]["path"] == ".env"
    commits = sh(ubuntu.repo, "rev-list", "--count", "HEAD")
    notices = len(events.read(100, kinds=("notify",)))
    for _ in range(6):
        assert ubuntu.tick(120)["status"] == "POLICY_BLOCKED"
    assert sh(ubuntu.repo, "rev-list", "--count", "HEAD") == commits
    assert len(events.read(100, kinds=("notify",))) == notices == 1
    (ubuntu.repo / ".env").unlink()
    assert ubuntu.tick(1)["status"] == "SYNCED"


def test_final_acceptance_scenario_sync_part(pair, tmp_path):
    """Steps 1-16 of the V1 acceptance scenario, with the Mac played by a second clone."""
    origin, ubuntu, mac = pair
    (ubuntu.repo / "feature.py").write_text("def f(): return 1\n")  # 1-2 user works on Ubuntu
    assert ubuntu.settle()["status"] == "SYNCED"  # 3 checkpoint
    hidden = tmp_path / "net.down"
    origin.rename(hidden)  # 4 internet disappears
    sh(ubuntu.repo, "remote", "set-url", "origin", "https://127.0.0.1:1/x.git")
    (ubuntu.repo / "feature.py").write_text("def f(): return 2\n")  # 5 user keeps working
    assert ubuntu.settle()["status"] == "OFFLINE"
    hidden.rename(origin)  # 6 internet returns
    sh(ubuntu.repo, "remote", "set-url", "origin", str(origin))
    assert ubuntu.tick(4000)["status"] == "SYNCED"  # 7 GitHub receives the queue
    assert mac.boot().tick()["status"] == "SYNCED"  # 8-10 Ubuntu off, Mac boots and reconciles
    assert (mac.repo / "feature.py").read_text() == "def f(): return 2\n"  # 11-12 changes are present
    (mac.repo / "README.md").write_text("docs from the mac\n")  # 13 another file on the Mac
    assert mac.settle()["status"] == "SYNCED"  # 14 Mac checkpoints and pushes
    assert ubuntu.boot().tick()["status"] == "SYNCED"  # 15-16 Ubuntu reconciles
    assert head(ubuntu.repo) == head(mac.repo) == sh(origin, "rev-parse", "main")
    assert (ubuntu.repo / "README.md").read_text() == "docs from the mac\n"
    subjects = sh(origin, "log", "--format=%s", "main").splitlines()
    assert [s.split()[2] for s in subjects[:3]] == ["mac", "ubuntu", "ubuntu"]  # 34 nothing lost, linear
    assert state.load("sync_memo") == {}
