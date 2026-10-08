"""Unit tests: config, inventory, state machines, parsing, rendering."""

from __future__ import annotations

import tomllib

from suw import cloud
from suw.core import config, events, gitsync, health, inventory, journal, modes, paths, syncer, tomlw
from suw.core.gitsync import Action, RepoState, Status
from suw.integrations import browser, gnome, ssh


# ── config ──────────────────────────────────────────────────────────────────


def test_toml_roundtrip():
    data = {"a": 1, "s": 'q"uo\\te', "l": ["x", "y"], "t": {"b": True, "n": {"f": 1.5}}, "arr": [{"k": "v"}, {"k": "w"}], "sp ace": "ok"}
    assert tomllib.loads(tomlw.dumps(data)) == data


def test_config_layers_and_named_urls():
    cfg = config.load()
    assert cfg.get("sync.policy") == "manual"
    assert cfg.url("docs") is None  # not configured until shared/urls.toml exists
    config.set_value("urls.docs", "https://huggingface.co", "urls")
    config.set_value("workstation.browser.open", ["docs", "github"], "shared")
    config.set_value("sync.policy", "rebase", "local")
    cfg = config.load()
    assert cfg.get("sync.policy") == "rebase"
    assert browser.session_urls(cfg) == (["https://huggingface.co", "https://github.com"], [])


def test_placeholder_url_is_reported_not_opened():
    config.set_value("urls.docs", "https://REPLACE_WITH_URL", "urls")
    config.set_value("workstation.browser.open", ["docs"], "shared")
    assert browser.session_urls(config.load()) == ([], ["docs"])


def test_broken_config_falls_back_to_last_known_good():
    config.set_value("sync.poll_seconds", 11, "local")
    assert config.load().get("sync.poll_seconds") == 11
    config.local_file().write_text("this is = = not toml")
    cfg = config.load()
    assert cfg.get("sync.poll_seconds") == 11
    assert cfg.warnings


def test_cli_value_parsing():
    assert config.parse_cli_value("5") == 5
    assert config.parse_cli_value("true") is True
    assert config.parse_cli_value('["a","b"]') == ["a", "b"]
    assert config.parse_cli_value("https://x.y") == "https://x.y"


# ── inventory / cloud role ──────────────────────────────────────────────────


def test_cloud_role_moves_and_history_remains():
    inv = inventory.load()
    first = inventory.new_cloud_label(inv)
    assert inventory.cloud_assign(inv, first, {"endpoint": "203.0.113.5", "user": "root"}) is None
    second = inventory.new_cloud_label(inv)
    assert second != first
    assert inventory.cloud_assign(inv, second, {"endpoint": "198.51.100.9", "user": "ubuntu"}) == first
    inventory.save(inv)
    inv = inventory.load()
    assert inventory.cloud_current(inv)[0] == second
    assert inv["cloud"]["nodes"][first]["state"] == "retired"
    assert inventory.cloud_release(inv) == second
    assert inventory.cloud_current(inv) == (None, None)
    assert len(inv["cloud"]["nodes"]) == 2


def test_endpoint_validation_rejects_option_injection():
    assert inventory.valid_host("203.0.113.5") and inventory.valid_host("gpu-1.example.com") and inventory.valid_host("2001:db8::1")
    for bad in ("-oProxyCommand=evil", "a b", "host;rm", "", "x" * 300, "$(id)"):
        assert not inventory.valid_host(bad)
    assert inventory.valid_user("root") and not inventory.valid_user("-l") and not inventory.valid_user("a;b")
    provider = cloud.provider()
    for host, user, port in (("bad host", "root", 22), ("1.2.3.4", "ro ot", 22), ("1.2.3.4", "root", 0)):
        try:
            provider.validate_endpoint(host, user, port)
            raise AssertionError("accepted invalid endpoint")
        except cloud.CloudError:
            pass


def test_capabilities_from_hardware_not_brand():
    big = {"gpu": "A100", "gpu_vram_gb": 80, "cuda": "12.4", "cores": 16, "ram_gb": 64}
    assert inventory.capabilities(big)[:2] == ["training", "inference"]
    assert inventory.recommend_profile(big) == "training"
    assert inventory.recommend_profile({"gpu": "T4", "gpu_vram_gb": 15}) == "inference"
    assert inventory.recommend_profile({"cores": 8}) == "compute"
    assert inventory.capabilities({"cores": 1, "ram_gb": 1}) == ["lightweight services"]


def test_ssh_config_rendering_and_cloud_replacement():
    inv = inventory.load()
    inventory.ensure_device(inv, "ubuntu", role="workstation", tailscale_name="ubuntu.tail.ts.net")
    inventory.ensure_device(inv, "mac", role="workstation")  # not enrolled: no host block
    inventory.ensure_device(inv, "home", role="home", tailscale_name="home.tail.ts.net", ssh_user="sam")
    inventory.cloud_assign(inv, "cloud-20260101", {"endpoint": "203.0.113.5", "port": 2222, "user": "root", "host_key": "ssh-ed25519 AAAAone"})
    text = ssh.render(inv, "ubuntu")
    assert "Host home\n    HostName home.tail.ts.net\n    User sam" in text
    assert "Host ubuntu\n" not in text and "Host mac\n" not in text
    assert "HostName 203.0.113.5" in text and "Port 2222" in text and "HostKeyAlias suw-cloud-20260101" in text
    assert "StrictHostKeyChecking yes" in text and "StrictHostKeyChecking no" not in text
    inventory.cloud_assign(inv, "cloud-20260202", {"endpoint": "198.51.100.9", "user": "ubuntu", "tailscale_name": "cloud-20260202", "host_key": "ssh-ed25519 AAAAtwo"})
    text = ssh.render(inv, "ubuntu")
    assert "HostName cloud-20260202" in text and "203.0.113.5" not in text and "Port" not in text
    known = ssh.render_known_hosts(inv)
    assert "suw-cloud-20260202 ssh-ed25519 AAAAtwo" in known and "AAAAone" not in known


def test_ssh_apply_keeps_user_config(sandbox):
    user_config = sandbox / ".ssh" / "config"
    user_config.parent.mkdir()
    user_config.write_text("Host mine\n    HostName 10.0.0.1\n")
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", tailscale_name="home.ts.net", ssh_user="u")
    assert ssh.apply(inv, "ubuntu")
    assert ssh.apply(inv, "ubuntu") == []  # idempotent
    text = user_config.read_text()
    assert text.startswith(journal.BEGIN) and "Include ~/.ssh/suw.conf" in text and "Host mine\n    HostName 10.0.0.1" in text
    assert text.count("Include") == 1


# ── git policy (pure) ───────────────────────────────────────────────────────

V2 = """# branch.oid abc
# branch.head main
# branch.upstream origin/main
# branch.ab +2 -1
1 .M N... 100644 100644 100644 aaa bbb src/app.py
2 R. N... 100644 100644 100644 aaa bbb R100 new name.txt\told.txt
u UU N... 100644 100644 100644 100644 a b c conflict.txt
? notes with space.md
"""


def test_status_parser():
    st = gitsync.parse_status_v2(V2)
    assert (st.branch, st.upstream, st.ahead, st.behind) == ("main", "origin/main", 2, 1)
    assert (st.dirty, st.unmerged, st.untracked) == (2, 1, 1)
    assert st.files == ["src/app.py", "new name.txt", "conflict.txt", "notes with space.md"]
    assert gitsync.parse_status_v2("# branch.head (detached)\n").detached


def state(**kw) -> RepoState:
    return RepoState(path="/x", branch="main", upstream="origin/main", **kw)


def test_sync_decisions():
    assert gitsync.decide(state()) is Action.NONE
    assert gitsync.decide(state(behind=3)) is Action.FAST_FORWARD
    # Attempted even on a dirty tree: git itself refuses when an incoming file overlaps.
    assert gitsync.decide(state(behind=3, dirty=1)) is Action.FAST_FORWARD
    assert gitsync.decide(state(ahead=1)) is Action.PUSH
    assert gitsync.decide(state(ahead=1, dirty=4)) is Action.PUSH  # pushing never touches the tree
    assert gitsync.decide(state(ahead=1), push=False) is Action.NONE
    assert gitsync.decide(state(ahead=1, behind=1)) is Action.HOLD_MANUAL  # default: stop and ask
    assert gitsync.decide(state(ahead=1, behind=1), policy="merge") is Action.RECONCILE
    assert gitsync.decide(state(ahead=1, behind=1), policy="rebase") is Action.RECONCILE
    assert gitsync.decide(state(ahead=1, behind=1, dirty=1)) is Action.HOLD_DIRTY
    assert gitsync.decide(state(unmerged=1, behind=1)) is Action.HOLD_CONFLICT
    assert gitsync.decide(state(in_progress="rebase", ahead=1)) is Action.HOLD_CONFLICT
    assert gitsync.decide(RepoState(path="/x", branch="main", ahead=2)) is Action.NONE  # no upstream
    assert gitsync.decide(RepoState(path="/x", detached=True, upstream="o/m", behind=1)) is Action.NONE


def test_status_classification():
    assert gitsync.classify(state()) is Status.SYNCED
    assert gitsync.classify(state(), online=None) is Status.CLEAN  # remote not checked
    assert gitsync.classify(state(dirty=1)) is Status.LOCAL_CHANGES
    assert gitsync.classify(state(untracked=1), autosync=True) is Status.CHECKPOINT_PENDING
    assert gitsync.classify(state(ahead=1)) is Status.PUSH_PENDING
    assert gitsync.classify(state(ahead=1), push=False) is Status.LOCAL_COMMITTED
    assert gitsync.classify(state(behind=1)) is Status.REMOTE_AHEAD
    assert gitsync.classify(state(ahead=1, behind=1)) is Status.DIVERGED
    assert gitsync.classify(state(unmerged=1)) is Status.CONFLICT
    assert gitsync.classify(state(ahead=1), online=False) is Status.OFFLINE
    assert gitsync.classify(RepoState(path="/x", branch="main")) is Status.LOCAL_COMMITTED  # no remote
    assert gitsync.classify(RepoState(path="/x", error="boom")) is Status.RECOVERY_REQUIRED
    assert set(gitsync.LABELS) == set(Status)
    required = "CLEAN LOCAL_CHANGES CHECKPOINT_PENDING LOCAL_COMMITTED PUSH_PENDING PUSH_FAILED REMOTE_AHEAD SYNCED DIVERGED CONFLICT OFFLINE AUTH_REQUIRED POLICY_BLOCKED RECOVERY_REQUIRED"
    assert {s.value for s in Status} == set(required.split())


def test_sensitive_file_preflight():
    deny, allow = gitsync.DEFAULT_DENY, gitsync.DEFAULT_ALLOW
    files = [".env", "app/.env.production", "certs/server.pem", "deploy/id_ed25519", "x/credentials.json", "service-account-prod.json", ".env.example", "src/main.py", "keyboard.py"]
    assert gitsync.sensitive_files(files, deny, allow) == files[:6]


def test_checkpoint_message_format():
    import re

    assert re.fullmatch(r"chore\(suw-autosync\): checkpoint ubuntu \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", gitsync.checkpoint_message("ubuntu"))


def test_debounce_batches_edits():
    deb = syncer.Debounce()
    assert not deb.due("a", 0, idle=60, min_interval=300)  # first sighting starts the clock
    assert not deb.due("a", 30, 60, 300)  # still within idle window
    assert not deb.due("b", 50, 60, 300)  # user kept typing: clock restarts
    assert not deb.due("b", 100, 60, 300)
    assert deb.due("b", 111, 60, 300)
    deb.committed(111)
    assert not deb.due("c", 120, 60, 300)
    assert not deb.due("c", 200, 60, 300)  # idle, but min interval not reached
    assert deb.due("c", 411, 60, 300)
    assert not deb.due(None, 500, 60, 300)  # clean tree


def test_debounce_max_wait_when_user_never_stops_typing():
    deb = syncer.Debounce()
    for second in range(0, 100, 5):  # a save every 5 s: the quiet period never elapses
        assert not deb.due(f"v{second}", second, idle=20, min_interval=0, max_wait=120)
    assert deb.due("v-final", 121, 20, 0, 120)  # ...but the checkpoint still happens
    deb.committed(121)
    assert not deb.due("again", 122, 20, 0, 120)


# ── health / cloud parsing ──────────────────────────────────────────────────


def test_probe_parsing_and_hardware():
    report = health.parse("probe=1\nos=Ubuntu 24.04\ncores=16\nram_mb=64312\ncpu_pct=7\ngpu=NVIDIA A100-SXM4-80GB, 81920, 1024, 3\ngpu=NVIDIA A100-SXM4-80GB, 81920, 0, 0\ncuda=12.4\nservice.nginx=active\nservice.bot=failed\n")
    assert report["services"] == {"nginx": "active", "bot": "failed"}
    hw = health.hardware(report)
    assert (hw["cores"], hw["ram_gb"], hw["gpu_count"], hw["gpu_vram_gb"], hw["cuda"]) == (16, 63, 2, 80, "12.4")
    assert hw["gpu"] == "2x A100-SXM4-80GB"


def test_local_probe_runs():
    report = health.probe_local()
    assert report["probe"] == 1 and report["cores"] >= 1 and 0 <= report["disk_used_pct"] <= 100


def test_teardown_report_guard():
    report = cloud.parse_report("dirty=/root/proj|3\nunpushed=/root/proj|2\njob=tmux session train (1 windows)\nmount=/data (ext4, 40% used)\n")
    assert cloud.is_risky(report)
    assert not cloud.is_risky(cloud.parse_report("mount=/data (ext4, 1% used)\n"))
    assert cloud.is_risky(cloud.parse_report("noremote=/root/only-copy\n"))


def test_fingerprint_comparison_is_strict():
    assert cloud.fingerprints_match("SHA256:abcDEF", "SHA256:abcDEF")
    assert cloud.fingerprints_match("abcDEF=", "SHA256:abcDEF")
    assert not cloud.fingerprints_match("", "SHA256:abcDEF")
    assert not cloud.fingerprints_match("SHA256:abcdef", "SHA256:abcDEF")


def test_remote_scripts_never_disable_host_checking():
    import pathlib

    root = pathlib.Path(cloud.__file__).parents[1]
    for path in root.rglob("*"):
        if path.suffix in (".py", ".sh") and path.is_file():
            text = path.read_text()
            assert "StrictHostKeyChecking=no" not in text and "StrictHostKeyChecking no" not in text, path
            assert not [ln for ln in text.splitlines() if "push" in ln and ("--force" in ln or '"-f"' in ln)], path
            assert "reset --hard" not in text and "clean -fd" not in text, path


# ── events / journal / modes ────────────────────────────────────────────────


def test_event_log_redacts_secrets():
    events.emit("t", "push failed for https://user:hunter2@github.com/x with ghp_abcdefghijklmnop1234 and tskey-auth-kABCDEF123", token="hf_abcdefghijklmnop", note="password=swordfish")
    raw = events.log_path().read_text()
    for secret in ("hunter2", "ghp_abcdefghijklmnop1234", "tskey-auth-kABCDEF123", "hf_abcdefghijklmnop", "swordfish"):
        assert secret not in raw
    last = events.read(5)[-1]
    assert last["event"] == "t" and set(last) >= {"timestamp", "event", "level", "device", "message"}


def test_event_log_rotates(monkeypatch):
    monkeypatch.setattr(events, "MAX_BYTES", 400)
    for index in range(40):
        events.emit("spam", f"line {index} " + "x" * 40)
    assert events.log_path().with_suffix(".jsonl.1").exists()
    assert events.log_path().stat().st_size < 1200


def test_managed_block_is_idempotent_and_reversible(sandbox):
    rc = sandbox / ".zshrc"
    rc.write_text("export A=1\nalias x=y\n")
    assert journal.managed_block(rc, "source /suw/init.sh")
    assert not journal.managed_block(rc, "source /suw/init.sh")
    assert journal.managed_block(rc, "source /suw/v2.sh")  # refresh replaces, never duplicates
    text = rc.read_text()
    assert text.count(journal.BEGIN) == 1 and "v2.sh" in text and "init.sh" not in text
    created = sandbox / ".config" / "new.conf"
    journal.write_file(created, "x")
    lines = journal.rollback()
    assert rc.read_text() == "export A=1\nalias x=y\n"
    assert not created.exists() and lines
    assert journal.rollback() == []


def test_mode_machine_is_idempotent():
    config.set_value("workstation.launch", {"editor": False, "terminal": False, "servers": False, "browser": False, "files": False}, "local")
    cfg = config.load()
    assert modes.current() == modes.DEFAULT
    assert modes.off(cfg) == ["already in Default Mode"]
    modes.on(cfg)
    assert modes.current() == modes.WORKSTATION
    second = modes.on(cfg)
    assert any("already launched" in step for step in second)
    assert modes.toggle(cfg)[0] == modes.DEFAULT
    modes.off(cfg)
    assert modes.current() == modes.DEFAULT
    assert [e["message"] for e in events.read(10, "Workstation Mode")] == ["Workstation Mode on", "Workstation Mode off"]


def test_interrupted_mode_transition_resumes():
    from suw.core import state as st

    config.set_value("workstation.launch", {"editor": False, "terminal": False, "servers": False, "browser": False}, "local")
    st.save("mode", {"mode": modes.STARTING, "saved": {"schema key": "'v'"}, "launched": True})
    modes.on(config.load())
    data = modes.load()
    assert data["mode"] == modes.WORKSTATION and data["saved"] == {"schema key": "'v'"}  # original values kept


def test_keybinding_normalisation():
    assert gnome.norm("<Super>d") == gnome.norm("<super>D")
    assert gnome.norm("<Primary><Super>d") == gnome.norm("<Super><Control>d")
    assert gnome.norm("<Super>d") != gnome.norm("<Super><Shift>d")
    assert gnome._list("@as []") == [] and gnome._list("['<Super>d', 'x']") == ["<Super>d", "x"]


def test_paths_are_isolated(sandbox):
    assert str(paths.config_dir()).startswith(str(sandbox))
    assert str(paths.state_dir()).startswith(str(sandbox))


def test_rollback_restores_only_wholesale_replacements(sandbox):
    owned = sandbox / "owned.conf"
    owned.write_text("original\n")
    journal.write_file(owned, "replaced by suw\n")
    edited = sandbox / ".gitconfig"
    journal.backup_file(edited)  # surgical change: backed up for safety, not restored
    edited.write_text(edited.read_text() + "[alias]\n\tco = checkout\n")
    journal.rollback()
    assert owned.read_text() == "original\n"
    assert "co = checkout" in edited.read_text()  # the user's later edit survives
