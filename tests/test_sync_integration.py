"""Integration tests for the sync engine against a real local 'GitHub' (a bare repository)."""

from __future__ import annotations

import shutil
import subprocess

from conftest import MANUAL, POLICY, sh

from suw.core import config, events, gitsync, state, syncer
from suw.core.gitsync import Status


def test_checkpoint_then_push_then_other_machine_catches_up(remote_pair):
    """Edit on Ubuntu, MacBook boots later and becomes current — no manual git."""
    origin, ubuntu, mac = remote_pair
    (ubuntu / "a.txt").write_text("one\ntwo\n")
    (ubuntu / "new.py").write_text("print(1)\n")
    outcome = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    assert outcome.changed and outcome.status is Status.PUSH_PENDING
    assert sh(ubuntu, "log", "-1", "--format=%s").startswith("chore(suw-autosync): checkpoint ubuntu ")
    assert gitsync.reconcile(ubuntu, POLICY).action == "push"
    assert sh(origin, "rev-parse", "main") == sh(ubuntu, "rev-parse", "HEAD")

    result = gitsync.reconcile(mac, POLICY)
    assert (result.status, result.action) == (Status.SYNCED, "fast-forward")
    assert (mac / "new.py").read_text() == "print(1)\n"
    assert gitsync.reconcile(mac, POLICY).action == "none"  # idempotent


def test_human_commits_are_never_rewritten(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "a.txt").write_text("human\n")
    sh(ubuntu, "commit", "-qam", "feat: human work")
    before = sh(ubuntu, "rev-parse", "HEAD")
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).detail == "nothing to commit"
    gitsync.reconcile(ubuntu, POLICY)
    assert sh(ubuntu, "rev-parse", "HEAD") == before
    assert sh(ubuntu, "log", "-1", "--format=%s") == "feat: human work"


def test_uncommitted_work_is_never_overwritten(remote_pair):
    _origin, ubuntu, mac = remote_pair
    (ubuntu / "a.txt").write_text("from ubuntu\n")
    sh(ubuntu, "commit", "-qam", "ubuntu change")
    gitsync.reconcile(ubuntu, POLICY)
    (mac / "a.txt").write_text("precious unsaved mac work\n")
    result = gitsync.reconcile(mac, dict(POLICY, enabled=False))
    assert (result.status, result.action) == (Status.REMOTE_AHEAD, "hold-dirty")
    assert (mac / "a.txt").read_text() == "precious unsaved mac work\n"
    assert gitsync.collect(mac).behind == 1


def test_incoming_commits_are_taken_when_they_do_not_touch_unsaved_files(remote_pair):
    """The common MacBook-wakes-up case: stale unsaved edit in one file, news in another."""
    _origin, ubuntu, mac = remote_pair
    (ubuntu / "other.txt").write_text("from ubuntu\n")
    sh(ubuntu, "add", "-A"), sh(ubuntu, "commit", "-qm", "ubuntu change")
    gitsync.reconcile(ubuntu, POLICY)
    (mac / "a.txt").write_text("unsaved mac work\n")
    row = syncer.sync_project(config.load(), mac, MANUAL, force_checkpoint=True)
    assert (mac / "other.txt").exists() and (mac / "a.txt").read_text() == "unsaved mac work\n"
    assert row["status"] == "SYNCED"  # fast-forward, then checkpoint on top, then push: linear
    assert sh(mac, "log", "--format=%p", "-1").count(" ") == 0  # not a merge commit


def test_diverged_is_not_merged_by_default(remote_pair):
    """Ubuntu edits A, Mac edits B, both commit independently -> DIVERGED, no overwrite."""
    origin, ubuntu, mac = remote_pair
    (ubuntu / "u.txt").write_text("u\n")
    sh(ubuntu, "add", "-A"), sh(ubuntu, "commit", "-qm", "ubuntu")
    gitsync.reconcile(ubuntu, MANUAL)
    (mac / "m.txt").write_text("m\n")
    sh(mac, "add", "-A"), sh(mac, "commit", "-qm", "mac")
    mac_head, origin_head = sh(mac, "rev-parse", "HEAD"), sh(origin, "rev-parse", "main")

    cfg = config.load()
    row = syncer.sync_project(cfg, mac, MANUAL)
    assert row["status"] == "DIVERGED" and "nothing was changed" in row["detail"]
    assert sh(mac, "rev-parse", "HEAD") == mac_head and sh(origin, "rev-parse", "main") == origin_head
    assert not (mac / "u.txt").exists()  # nothing merged behind the user's back
    branch = row["recovery_branch"]
    assert branch.startswith("suw/recovery/") and sh(mac, "rev-parse", branch) == mac_head

    for _ in range(3):  # remembered: no endless branches, no repeated notifications
        assert syncer.sync_project(cfg, mac, MANUAL)["status"] == "DIVERGED"
    assert gitsync.recovery_branches(mac) == [branch]
    assert len(events.read(50, kinds=("notify",))) == 1

    # The user decides: one explicit command merges (it would abort on conflict).
    assert gitsync.manual_reconcile(mac, "merge").changed
    assert syncer.sync_project(cfg, mac, MANUAL)["status"] == "SYNCED"
    assert sh(origin, "rev-parse", "main") == sh(mac, "rev-parse", "HEAD")
    assert sh(mac, "merge-base", "--is-ancestor", mac_head, "HEAD") == ""
    assert state.load("sync_memo") == {}


def test_opt_in_merge_policy_reconciles_non_conflicting_history(remote_pair):
    origin, ubuntu, mac = remote_pair
    (ubuntu / "u.txt").write_text("u\n")
    sh(ubuntu, "add", "-A"), sh(ubuntu, "commit", "-qm", "ubuntu")
    gitsync.reconcile(ubuntu, POLICY)
    (mac / "m.txt").write_text("m\n")
    sh(mac, "add", "-A"), sh(mac, "commit", "-qm", "mac")
    local_commit = sh(mac, "rev-parse", "HEAD")
    result = gitsync.reconcile(mac, POLICY)
    assert result.status is Status.SYNCED and result.changed
    assert (mac / "u.txt").exists() and (mac / "m.txt").exists()
    assert sh(mac, "merge-base", "--is-ancestor", local_commit, "HEAD") == ""  # local commit kept, not rewritten
    assert sh(origin, "rev-parse", "main") == sh(mac, "rev-parse", "HEAD")


def test_true_conflict_preserves_both_sides_and_notifies_once(remote_pair):
    _origin, ubuntu, mac = remote_pair
    (ubuntu / "a.txt").write_text("ubuntu version\n")
    sh(ubuntu, "commit", "-qam", "ubuntu edit")
    gitsync.reconcile(ubuntu, POLICY)
    (mac / "a.txt").write_text("mac version\n")
    sh(mac, "commit", "-qam", "mac edit")
    mac_head = sh(mac, "rev-parse", "HEAD")

    cfg = config.load()
    row = syncer.sync_project(cfg, mac, POLICY)  # merge policy opted in: the merge is tried
    assert row["status"] == "CONFLICT"
    assert sh(mac, "rev-parse", "HEAD") == mac_head  # nothing reset
    assert (mac / "a.txt").read_text() == "mac version\n"  # worktree restored exactly
    assert gitsync.collect(mac).clean and not gitsync.collect(mac).in_progress
    assert "refs/suw/backup/main/" in sh(mac, "for-each-ref", "refs/suw/backup")
    assert len(gitsync.recovery_branches(mac)) == 1

    # Second and third cycles: remembered, not re-attempted, not re-announced.
    for _ in range(2):
        assert syncer.sync_project(cfg, mac, POLICY)["status"] == "CONFLICT"
    assert len(sh(mac, "for-each-ref", "refs/suw/backup").splitlines()) == 1
    assert len(gitsync.recovery_branches(mac)) == 1
    assert len(events.read(50, kinds=("notify",))) == 1

    # The user resolves it; the next cycle heals and clears the memo.
    sh(mac, "merge", "-q", "-X", "ours", "--no-edit", "origin/main")
    assert syncer.sync_project(cfg, mac, POLICY)["status"] == "SYNCED"
    assert state.load("sync_memo") == {}


def test_offline_queues_push_and_recovers(remote_pair, tmp_path):
    """GitHub outage: local work continues, push is queued, reconnect needs no repair."""
    origin, ubuntu, _mac = remote_pair
    hidden = tmp_path / "origin.hidden"
    origin.rename(hidden)
    sh(ubuntu, "remote", "set-url", "origin", "https://127.0.0.1:1/unreachable.git")
    (ubuntu / "a.txt").write_text("offline edit\n")
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).changed  # committing works offline
    row = syncer.sync_project(config.load(), ubuntu, POLICY)
    assert row["status"] == "OFFLINE" and row["ahead"] == 1
    assert events.read(20, level="error") == []  # an outage is not an error and is not announced
    assert events.read(20, kinds=("notify",)) == []

    hidden.rename(origin)
    sh(ubuntu, "remote", "set-url", "origin", str(origin))
    assert syncer.sync_project(config.load(), ubuntu, POLICY)["status"] == "SYNCED"
    assert sh(origin, "rev-parse", "main") == sh(ubuntu, "rev-parse", "HEAD")


def test_secret_files_are_held_back_and_the_rest_is_committed(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / ".env").write_text("API_KEY=sk-live-123\n")
    (ubuntu / "ok.py").write_text("x = 1\n")
    outcome = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    assert outcome.status is Status.POLICY_BLOCKED and outcome.blocked == [".env"] and outcome.changed
    assert sh(ubuntu, "show", "--name-only", "--format=", "HEAD") == "ok.py"  # only the safe file
    assert (ubuntu / ".env").read_text() == "API_KEY=sk-live-123\n"  # never deleted, never staged
    assert sh(ubuntu, "status", "--porcelain") == "?? .env"

    again = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    assert again.status is Status.POLICY_BLOCKED and not again.changed  # nothing else to commit
    (ubuntu / ".env").unlink()
    (ubuntu / ".env.example").write_text("API_KEY=\n")
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).status is Status.PUSH_PENDING


def test_oversized_and_artifact_files_are_held_back(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "model.bin").write_bytes(b"\0" * (2 * 1024 * 1024))  # POLICY: max_file_mb = 1
    (ubuntu / "weights.safetensors").write_bytes(b"\1" * 10)
    (ubuntu / "train.py").write_text("print('ok')\n")
    outcome = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    assert sorted(outcome.blocked) == ["model.bin", "weights.safetensors"]
    assert {h["reason"] for h in outcome.held} == {"oversized", "artifact"}
    assert sh(ubuntu, "show", "--name-only", "--format=", "HEAD") == "train.py"
    assert (ubuntu / "model.bin").stat().st_size == 2 * 1024 * 1024  # never silently deleted


def test_merge_in_progress_is_left_alone(remote_pair):
    _origin, ubuntu, mac = remote_pair
    (ubuntu / "a.txt").write_text("U\n")
    sh(ubuntu, "commit", "-qam", "u"), gitsync.reconcile(ubuntu, POLICY)
    (mac / "a.txt").write_text("M\n")
    sh(mac, "commit", "-qam", "m"), sh(mac, "fetch", "-q")
    subprocess.run(["git", "-C", str(mac), "merge", "origin/main"], capture_output=True)
    assert gitsync.collect(mac).in_progress == "merge"
    assert gitsync.checkpoint(mac, "mac", POLICY).status is Status.CONFLICT
    assert gitsync.reconcile(mac, POLICY).action == "hold-conflict"
    assert "<<<<<<<" in (mac / "a.txt").read_text()  # the user's in-flight merge is untouched


def test_project_policy_file(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    defaults = config.load().data
    policy = gitsync.project_policy(ubuntu, defaults)
    assert policy["enabled"] is True and policy["policy"] == "manual"  # safe by default
    assert 15 <= policy["idle_seconds"] <= 30
    (ubuntu / ".suw.toml").write_text(
        '[autosync]\nenabled = false\ndebounce_seconds = 5\nallow = ["fixtures/test.pem"]\nsecret_ignore = ["fixtures/*"]\n'
        '[artifacts]\npaths = ["datasets/", "checkpoints/"]\n[sync]\npolicy = "rebase"\n'
    )
    policy = gitsync.project_policy(ubuntu, defaults)
    assert (policy["enabled"], policy["idle_seconds"], policy["policy"]) == (False, 5, "rebase")  # explicit opt-out
    assert policy["artifact_paths"] == ["datasets/", "checkpoints/"] and policy["secret_ignore"] == ["fixtures/*"]
    assert gitsync.sensitive_files(["fixtures/test.pem", "prod.pem"], policy["deny"], policy["allow"]) == ["prod.pem"]
    (ubuntu / ".suw.toml").write_text("not [valid toml")
    assert gitsync.project_policy(ubuntu, defaults)["policy"] == "manual"  # broken file = global defaults


def test_repo_without_remote_and_missing_repo(tmp_path):
    lonely = tmp_path / "lonely"
    subprocess.run(["git", "init", "-q", "-b", "main", str(lonely)], check=True)
    (lonely / "f").write_text("x")
    assert gitsync.checkpoint(lonely, "ubuntu", POLICY).changed
    result = gitsync.reconcile(lonely, POLICY)
    assert (result.status, result.action) == (Status.LOCAL_COMMITTED, "none")
    shutil.rmtree(lonely)
    assert gitsync.reconcile(lonely, POLICY).status is Status.RECOVERY_REQUIRED


def test_discovery_and_sync_all_isolate_failures(sandbox, remote_pair):
    _origin, ubuntu, _mac = remote_pair
    active = sandbox / "Projects" / "active"
    active.mkdir(parents=True)
    shutil.copytree(ubuntu, active / "good")
    (active / "broken").mkdir()
    (active / "broken" / ".git").mkdir()  # corrupt repository
    (active / "not-a-repo").mkdir()
    rows = {r["name"]: r for r in syncer.sync_all(config.load())}
    assert set(rows) == {"good", "broken"}
    assert rows["good"]["status"] == "SYNCED" and rows["broken"]["status"] == "RECOVERY_REQUIRED"
