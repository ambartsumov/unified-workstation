"""Secret scanning, checkpoint policy, the state store and other safety primitives."""

from __future__ import annotations

import errno
import json
import os

import pytest
from conftest import POLICY, sh

from suw.core import config, events, gitsync, journal, policy, state, tomlw
from suw.core.gitsync import Status

FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_TG = "123456789:" + "AAH" + "x" * 32


# ── secret scanning ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line, rule",
    [
        (f'token = "{FAKE_GH}"', "GitHub token"),
        (f"aws_access_key_id={FAKE_AWS}", "AWS access key"),
        (f"BOT={FAKE_TG}", "Telegram bot token"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
        ('password = "correct-horse-battery"', "password assignment"),
        ("url = https://deploy:s3cretpass@example.org/repo.git", "credentials in URL"),
        ("HF=" + "hf_" + "Z" * 34, "Hugging Face token"),
    ],
)
def test_secret_patterns_are_detected(line, rule):
    assert policy.scan_text("x = 1\n" + line + "\n") == [(rule, 2)]


def test_scan_avoids_obvious_false_positives():
    harmless = [
        'password = "changeme"',
        'api_key = "${API_KEY}"',
        'token = "<your-token-here>"',
        "password = os.environ['DB_PASSWORD']",
        'secret = "example-secret-value"',
        "id: 12345",
        f'token = "{FAKE_GH}"  # suw:allow-secret (test fixture)',
    ]
    assert policy.scan_text("\n".join(harmless)) == []


def test_secret_in_file_content_blocks_only_that_file(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "settings.py").write_text(f'GITHUB = "{FAKE_GH}"\n')
    (ubuntu / "app.py").write_text("print('hello')\n")
    outcome = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    assert outcome.status is Status.POLICY_BLOCKED and outcome.blocked == ["settings.py"]
    assert outcome.held[0]["reason"] == "secret-content" and "GitHub token at line 1" in outcome.held[0]["detail"]
    assert sh(ubuntu, "show", "--name-only", "--format=", "HEAD") == "app.py"
    assert FAKE_GH in (ubuntu / "settings.py").read_text()  # the secret is not silently removed
    assert FAKE_GH not in events.log_path().read_text()  # ...and never reaches the log


def test_secret_added_to_a_tracked_file_is_held_back(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "a.txt").write_text(f"one\nkey={FAKE_AWS}\n")
    outcome = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    assert outcome.status is Status.POLICY_BLOCKED and not outcome.changed
    assert sh(ubuntu, "diff", "--cached", "--name-only") == ""


def test_allow_once_and_ignore_pattern_manage_false_positives(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "fixture.txt").write_text(f"sample {FAKE_GH}\n")
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).status is Status.POLICY_BLOCKED
    assert policy.allow_once(ubuntu, "fixture.txt")
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).changed  # accepted exactly once...
    (ubuntu / "fixture.txt").write_text(f"sample {FAKE_GH}\nchanged\n")
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).status is Status.POLICY_BLOCKED  # ...for that content only
    ignoring = dict(POLICY, secret_ignore=["fixture.txt"])
    assert gitsync.checkpoint(ubuntu, "ubuntu", ignoring).changed


def test_declared_artifact_paths_are_never_staged(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "datasets").mkdir()
    (ubuntu / "datasets" / "train.csv").write_text("a,b\n")
    (ubuntu / "src.py").write_text("x = 1\n")
    outcome = gitsync.checkpoint(ubuntu, "ubuntu", dict(POLICY, artifact_paths=["datasets/"]))
    assert outcome.blocked == ["datasets/train.csv"] and outcome.held[0]["reason"] == "artifact"
    assert sh(ubuntu, "ls-files") == "a.txt\nsrc.py"


def test_large_tracked_files_stay_the_users_decision(remote_pair):
    """Size rules apply to new files only: what a human already committed is theirs."""
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "big.dat").write_bytes(b"1" * (2 * 1024 * 1024))
    sh(ubuntu, "add", "big.dat"), sh(ubuntu, "commit", "-qm", "chore: add data by hand")
    (ubuntu / "big.dat").write_bytes(b"2" * (2 * 1024 * 1024))
    assert gitsync.checkpoint(ubuntu, "ubuntu", POLICY).status is Status.PUSH_PENDING


def test_token_in_remote_url_is_recognised():
    assert gitsync.url_has_credentials("https://user:pass@github.com/a/b.git")
    assert gitsync.url_has_credentials("https://" + FAKE_GH + "@github.com/a/b.git")
    assert not gitsync.url_has_credentials("https://github.com/a/b.git")
    assert not gitsync.url_has_credentials("git@github.com:a/b.git")


def test_log_redaction_covers_every_forbidden_class():
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    events.emit(
        "t",
        f"Authorization: Bearer abcdef0123456789 and Cookie: session=topsecretcookie; {FAKE_TG}",
        key=private_key,
        header="Set-Cookie: sid=anothersecret",
        password="password=hunter2hunter2",
    )
    raw = events.log_path().read_text()
    for leak in ("abcdef0123456789", "topsecretcookie", FAKE_TG, "b3BlbnNzaC1rZXktdjEAAAAA", "anothersecret", "hunter2hunter2"):
        assert leak not in raw


# ── state store ─────────────────────────────────────────────────────────────


def test_state_is_versioned_and_keeps_a_backup():
    state.save("mode", {"mode": "DEFAULT"})
    state.save("mode", {"mode": "WORKSTATION"})
    assert json.loads(state.path("mode").read_text())["_schema"] == state.SCHEMA
    assert state.load("mode") == {"mode": "WORKSTATION"}  # bookkeeping keys are not exposed
    assert json.loads((state.path("mode").parent / "mode.json.bak").read_text())["mode"] == "DEFAULT"


def test_corrupt_state_falls_back_to_previous_version():
    state.save("sync_memo", {"/repo": {"failures": 1}})
    state.save("sync_memo", {"/repo": {"failures": 2}})
    state.path("sync_memo").write_text('{"/repo": {"fail')  # torn write
    assert state.load("sync_memo") == {"/repo": {"failures": 1}}
    assert list(state.path("sync_memo").parent.glob("sync_memo.json.corrupt-*"))  # kept for inspection
    assert state.health()["unreadable"] == []


def test_power_loss_recovery_on_startup():
    state.save("mode", {"mode": "DEFAULT", "saved": {}})
    state.save("mode", {"mode": "WORKSTATION", "saved": {"k": "v"}})
    root = state.path("mode").parent
    (root / ".mode.json.abc123").write_text('{"mode": "WORK')  # temp file of an interrupted write
    state.path("notified").write_text("")  # zero-length file after a crash, no backup
    state.path("mode").write_text("\0\0\0")
    actions = state.recover()
    assert any("interrupted write" in a for a in actions)
    assert not list(root.glob(".mode.json.*"))
    assert state.load("mode")["mode"] == "DEFAULT"  # previous good version
    assert state.load("notified") == {}
    assert state.recover() == []  # second start: nothing left to repair


def test_atomic_write_survives_disk_full(tmp_path, monkeypatch):
    (tmp_path / "store").mkdir()
    target = tmp_path / "store" / "state.json"
    tomlw.atomic_write(target, '{"good": true}')

    def full(_fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError):
        tomlw.atomic_write(target, '{"good": false, "half')
    assert target.read_text() == '{"good": true}'  # old content intact
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]  # no temp file left behind


def test_event_log_never_raises_when_the_disk_is_unwritable(monkeypatch):
    monkeypatch.setattr(events, "log_path", lambda: __import__("pathlib").Path("/proc/suw-cannot-write/events.jsonl"))
    assert events.emit("t", "still fine")["message"] == "still fine"


# ── configuration validation ────────────────────────────────────────────────


def test_config_validation_catches_types_ranges_and_secrets():
    errors, warnings = config.validate(
        {
            "sync": {"policy": "yolo", "fetch_seconds": 1},
            "autosync": {"enabled": "yes"},
            "urls": {"docs": "not a url"},
            "cloud": {"api_token": "abc123456"},
            "secrets": {"openai": "keychain://openai", "raw": "sk-live"},
            "mystery": {"knob": 1},
        }
    )
    joined = "\n".join(errors)
    for needle in ("sync.policy: must be one of", "sync.fetch_seconds: must be between", "autosync.enabled: expected bool", "urls.docs: expected a URL", "cloud.api_token: looks like a secret", "secrets.raw: secrets must be references"):
        assert needle in joined
    assert "secrets.openai" not in joined
    assert warnings == ["mystery.knob: unknown key (ignored)"]
    assert config.validate(config.load().data) == ([], [])  # shipped defaults are valid


def test_config_set_is_validated_before_it_is_applied():
    assert config.check_set("sync.policy", "yolo", "local")
    assert config.check_set("workstation.browser.profile", "dedicated", "local") == []
    assert not config.local_file().exists()  # the dry run wrote nothing


def test_config_diff_names_the_source():
    config.set_value("sync.fetch_seconds", 600, "local")
    assert ("sync.fetch_seconds", 300, 600, "local") in config.diff()


# ── journal: backup manifest ────────────────────────────────────────────────


def test_backup_manifest_records_checksums(sandbox):
    original = sandbox / ".zshrc"
    original.write_text("export A=1\n")
    journal.managed_block(original, "source /suw/init.sh")
    (entry,) = journal.manifest()
    assert entry["original"] == str(original) and entry["owner"] == "suw" and len(entry["sha256"]) == 64 and entry["timestamp"] > 0
    assert journal.verify_backups() == []
    with open(entry["backup"], "a") as handle:
        handle.write("tampered\n")
    assert journal.verify_backups() == [str(original)]
