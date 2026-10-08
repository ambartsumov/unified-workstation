"""Failure-injection matrix: every failure has one deterministic, non-destructive outcome.

Covered elsewhere: GitHub unavailable / network returns / daemon restart / Mac offline /
Ubuntu offline (test_e2e), wrong host key / cloud replacement (test_cloud), corrupt or
interrupted state / disk full / secrets / oversized files (test_policy_state).
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import time

import pytest
from conftest import MANUAL, POLICY, FakeClock, sh

from suw.cli import main as cli
from suw.core import artifacts, config, events, gitsync, health, inventory, modes, paths, projects, state, status, syncer, watch
from suw.core.artifacts import ArtifactError, DirectoryStore, SSHStore
from suw.core.gitsync import Status
from suw.daemon.suwd import Daemon
from suw.integrations import apps, browser, clipboard, ssh as sshcfg, tailscale


def commit(repo, name="f.txt", text="x\n", message="work"):
    (repo / name).write_text(text)
    sh(repo, "add", "-A"), sh(repo, "commit", "-qm", message)


# ── remote failures ─────────────────────────────────────────────────────────


def test_dns_unavailable_is_offline_not_an_error(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    sh(ubuntu, "remote", "set-url", "origin", "https://no-such-host.invalid/repo.git")
    commit(ubuntu)
    row = syncer.sync_project(config.load(), ubuntu, POLICY)
    assert row["status"] == "OFFLINE" and row["ahead"] == 1
    assert events.read(20, level="error") == []


def test_push_rejected_keeps_commits_and_notifies_once_after_three(remote_pair):
    origin, ubuntu, _mac = remote_pair
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n")
    hook.chmod(0o755)
    commit(ubuntu)
    head = sh(ubuntu, "rev-parse", "HEAD")
    cfg = config.load()
    for _ in range(5):
        row = syncer.sync_project(cfg, ubuntu, POLICY)
        assert row["status"] == "PUSH_FAILED" and row["ahead"] == 1
    assert sh(ubuntu, "rev-parse", "HEAD") == head  # nothing rewritten, nothing dropped
    assert len(events.read(50, kinds=("notify",))) == 1  # persistent failure: exactly one notification
    hook.unlink()
    assert syncer.sync_project(cfg, ubuntu, POLICY)["status"] == "SYNCED"


def test_auth_failure_is_reported_as_auth_required():
    class Res:
        rc, out = 128, ""
        err = "remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/a/b.git/'"

    assert gitsync.net_failure(Res).state == "auth"
    Res.err = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository."
    assert gitsync.net_failure(Res).state == "auth"  # not mistaken for "offline"
    Res.err = "fatal: unable to access 'https://github.com/a/b.git/': Could not resolve host: github.com"
    assert gitsync.net_failure(Res).state == "offline"
    Res.rc, Res.err = 124, "git: timed out after 90s"
    assert gitsync.net_failure(Res).state == "offline"


def test_remote_history_replaced_never_destroys_local_commits(remote_pair, tmp_path):
    """The remote was rewritten by someone else: we stop, we do not follow, we do not overwrite."""
    origin, ubuntu, mac = remote_pair
    commit(ubuntu, "mine.txt")
    gitsync.reconcile(ubuntu, MANUAL)
    sh(mac, "pull", "-q")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
    sh(other, "commit", "-q", "--amend", "-m", "rewritten elsewhere"), sh(other, "push", "-q", "--force-with-lease", "origin", "main")
    mac_head = sh(mac, "rev-parse", "HEAD")
    row = syncer.sync_project(config.load(), mac, MANUAL)
    assert row["status"] == "DIVERGED" and sh(mac, "rev-parse", "HEAD") == mac_head
    assert (mac / "mine.txt").exists()


def test_unwritable_repository_fails_closed(remote_pair):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "new.txt").write_text("work in progress\n")
    objects = ubuntu / ".git" / "objects"
    mode = stat.S_IMODE(objects.stat().st_mode)
    objects.chmod(0o500)
    try:
        outcome = gitsync.checkpoint(ubuntu, "ubuntu", POLICY)
    finally:
        objects.chmod(mode)
    assert not outcome.changed and outcome.status in (Status.RECOVERY_REQUIRED, Status.POLICY_BLOCKED)
    assert (ubuntu / "new.txt").read_text() == "work in progress\n"


# ── servers ─────────────────────────────────────────────────────────────────


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def point(role: str, port: int, key: str = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA") -> None:
    inv = inventory.load()
    if role == "home":
        inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user="nobody", ssh_port=port)
    else:
        inventory.cloud_assign(inv, "cloud-x", {"endpoint": "127.0.0.1", "port": port, "user": "nobody", "host_key": key})
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")


def test_ssh_refused_and_ssh_timeout_are_offline(ssh_lab):
    point("cloud", closed_port())
    assert health.probe_remote("cloud")["status"] == health.OFFLINE  # connection refused
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)  # accepts the TCP connection, never speaks SSH
    try:
        point("cloud", silent.getsockname()[1])
        started = time.monotonic()
        report = health.probe_remote("cloud", timeout=2)
        assert report["status"] == health.OFFLINE and time.monotonic() - started < 6  # bounded, never hangs
    finally:
        silent.close()


def test_cloud_and_home_disappearing_do_not_break_the_workstation(ssh_lab, remote_pair, sandbox):
    _origin, ubuntu, _mac = remote_pair
    point("cloud", closed_port())
    point("home", closed_port())
    snap = status.build(config.load(), live=True)
    assert snap["servers"]["cloud"]["status"] == "OFFLINE" and snap["servers"]["home"]["status"] == "OFFLINE"
    assert any(item.startswith("cloud:") for item in snap["attention"]) and "local work (editing, commits, terminal)" in snap["still_works"]
    commit(ubuntu)
    assert syncer.sync_project(config.load(), ubuntu, POLICY)["status"] == "SYNCED"  # Git does not care
    assert modes.on(config.load()) is not None and modes.current() == modes.WORKSTATION  # nor does the mode switch


def test_unreachable_server_notifies_once_and_stays_quiet(monkeypatch):
    daemon = Daemon(clock=FakeClock())
    cfg = config.load()
    try:
        for _ in range(10):
            daemon._track(cfg, "home", {"online": False, "status": "OFFLINE", "error": "timed out"})
        assert len(events.read(50, kinds=("notify",))) == 1  # after the third miss, then silence
        daemon._track(cfg, "home", {"online": True})
        assert state.load("notified") == {}
        daemon._track(cfg, "cloud", {"online": False, "status": "UNTRUSTED", "identity_changed": True, "error": "Host key verification failed."})
        daemon._track(cfg, "cloud", {"online": False, "status": "UNTRUSTED", "identity_changed": True, "error": "Host key verification failed."})
        assert [e["key"] for e in events.read(50, kinds=("notify",))].count("identity:cloud") == 1
    finally:
        daemon.watcher.stop()


def test_tailscale_missing_or_stopped_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(tailscale, "have", lambda tool: False)
    snap = status.build(config.load())
    assert snap["components"]["tailscale"] == "NOT_CONFIGURED" and snap["overall"] == "READY"
    monkeypatch.setattr(tailscale, "status", lambda: {"installed": True, "running": False, "peers": []})
    snap = status.build(config.load())
    assert snap["components"]["tailscale"] == "OFFLINE" and "tailscale is not connected" in snap["attention"]


def test_powered_off_workstation_is_not_an_error():
    inv = inventory.load()
    inventory.ensure_device(inv, "ubuntu", role="workstation", tailscale_name="ubuntu.tail.ts.net")
    inventory.ensure_device(inv, "mac", role="workstation", platform="macos", tailscale_name="mac.tail.ts.net")
    seen = time.time() - 2 * 3600
    tailnet = {"installed": True, "running": True, "self": {"name": "ubuntu"}, "peers": [{"name": "mac", "online": False, "last_seen": seen}]}
    rows = {r["name"]: r for r in status.workstations(config.load(), inv, tailnet)}
    assert rows["mac"]["status"] == "OFFLINE" and rows["mac"]["last_seen"] == seen
    from suw.ui import text

    assert "offline" in text.workstation_line(rows["mac"]) and "last seen 2h ago" in text.workstation_line(rows["mac"])


# ── mode / browser / terminal duplicates ────────────────────────────────────


def test_repeated_mode_activation_opens_everything_once(monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr(apps, "spawn", lambda cmd, **kw: launched.append(list(cmd)) or True)
    monkeypatch.setattr(browser, "spawn", lambda cmd, **kw: launched.append(list(cmd)) or True)
    monkeypatch.setattr(apps, "editor", lambda cfg: "code")
    monkeypatch.setattr(apps, "terminal", lambda cfg: "wezterm")
    monkeypatch.setattr(apps, "have", lambda tool: False)  # no tmux: plain terminal windows
    monkeypatch.setattr(browser, "detect", lambda cfg: ("chromium", "chromium"))
    monkeypatch.setattr(browser, "running", lambda kind, profile="": True)
    config.set_value("urls.docs", "https://docs.example", "urls")
    config.set_value("workstation.browser.open", ["docs", "github"], "shared")
    cfg = config.load()
    for _ in range(3):
        modes.on(cfg)
    first = len(launched)
    assert sum(1 for cmd in launched if cmd[0] == "chromium") == 1 and sum(1 for cmd in launched if cmd[0] == "code") == 1
    assert [c for c in launched if c[0] == "chromium"][0][-2:] == ["https://docs.example", "https://github.com"]

    modes.off(cfg)
    assert len(launched) == first  # leaving the mode closes and kills nothing
    modes.on(cfg)  # a new session: the browser workspace is still there, so no second one
    assert sum(1 for cmd in launched if cmd[0] == "chromium") == 1


def test_browser_already_open_and_docs_unset(monkeypatch):
    opened: list[list[str]] = []
    monkeypatch.setattr(browser, "spawn", lambda cmd, **kw: opened.append(list(cmd)) or True)
    monkeypatch.setattr(browser, "detect", lambda cfg: ("firefox", "firefox"))
    monkeypatch.setattr(browser, "running", lambda kind, profile="": True)
    config.set_value("workstation.browser.open", ["docs", "github"], "shared")
    cfg = config.load()  # "docs" has no URL
    ok, note = browser.open_session(cfg)
    assert ok and "no address is set for: docs" in note
    assert opened == [["firefox", "--new-window", "https://github.com"]]  # no guessed site in its place
    assert browser.open_session(cfg)[1].startswith("already open") and len(opened) == 1
    monkeypatch.setattr(browser, "running", lambda kind, profile="": False)  # the user quit the browser
    browser.open_session(cfg)
    assert len(opened) == 2
    config.set_value("workstation.browser.profile", "dedicated", "local")
    monkeypatch.setattr(browser, "detect", lambda cfg: ("chrome", "google-chrome"))
    browser.open_session(config.load(), force=True)
    assert any(arg.startswith("--user-data-dir=") for arg in opened[-1])


def test_terminal_already_attached_is_not_opened_again(monkeypatch):
    spawned = []
    monkeypatch.setattr(apps, "spawn", lambda cmd, **kw: spawned.append(cmd) or True)
    monkeypatch.setattr(apps, "have", lambda tool: True)
    monkeypatch.setattr(apps, "terminal", lambda cfg: "wezterm")
    monkeypatch.setattr(apps, "tmux_attached", lambda session: True)
    assert apps.open_terminal(config.load(), "main", "t", 1) is None and spawned == []
    monkeypatch.setattr(apps, "tmux_attached", lambda session: False)
    assert apps.open_terminal(config.load(), "main", "t", 1) and len(spawned) == 1


# ── artifacts ───────────────────────────────────────────────────────────────


def exercise(store: artifacts.ArtifactStore, tmp_path) -> None:
    blob = tmp_path / "model.ckpt"
    blob.write_bytes(os.urandom(4096))
    item = store.put(blob, "speech-model", retention="30d", metadata={"epoch": "3"})
    assert (item.size, item.kind, item.retention, item.metadata) == (4096, "file", "30d", {"epoch": "3"}) and len(item.sha256) == 64
    data = tmp_path / "dataset"
    (data / "sub").mkdir(parents=True)
    (data / "sub" / "a.wav").write_bytes(b"RIFF" * 10)
    assert store.put(data, "speech-model").kind == "directory"
    assert [a.name for a in store.list()] == ["dataset", "model.ckpt"] and store.projects() == ["speech-model"]
    assert store.stat("speech-model", "model.ckpt").sha256 == item.sha256
    assert all(ok for _a, ok in store.verify())
    out = tmp_path / "restore"
    assert store.get("speech-model", "model.ckpt", out).read_bytes() == blob.read_bytes()
    assert (store.get("speech-model", "dataset", out) / "sub" / "a.wav").read_bytes() == b"RIFF" * 10
    with pytest.raises(ArtifactError, match="already exists"):
        store.get("speech-model", "model.ckpt", out)
    for bad in ("../escape", "a/b", "-rf", ""):
        with pytest.raises(ArtifactError):
            store.put(blob, "speech-model", bad or "..")
    assert store.delete("speech-model", "dataset") and not store.delete("speech-model", "dataset")
    assert [a.name for a in store.list("speech-model")] == ["model.ckpt"]


def test_directory_artifact_store(tmp_path):
    store = DirectoryStore(tmp_path / "store")
    exercise(store, tmp_path)
    (tmp_path / "store" / "speech-model" / "model.ckpt").write_bytes(b"bit rot")
    ((artifact, intact),) = store.verify()
    assert artifact.name == "model.ckpt" and not intact  # corruption is detected, not hidden
    with pytest.raises(ArtifactError, match="failed verification"):
        store.get("speech-model", "model.ckpt", tmp_path / "again")
    assert not (tmp_path / "again" / "model.ckpt").exists()


def test_ssh_artifact_store_over_real_ssh(ssh_lab, tmp_path, sandbox):
    server = ssh_lab("home")
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user=ssh_lab.user, ssh_port=server.port)
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    (sandbox / ".ssh" / "known_hosts").write_text(f"[127.0.0.1]:{server.port} {server.host_key}\n")  # what `suw home trust` records
    remote = tmp_path / "remote-store"
    store = SSHStore("home", str(remote), ssh_opts=["-o", "ControlPath=none"])
    exercise(store, tmp_path)
    assert (remote / "speech-model" / "model.ckpt").exists()
    server.stop()
    with pytest.raises(ArtifactError, match="cannot reach the artifact store"):
        store.list()
    with pytest.raises(ArtifactError):
        SSHStore("home", "../../etc")


# ── file watching ───────────────────────────────────────────────────────────


@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="inotify is Linux-only; macOS falls back to the periodic scan")
def test_file_watcher_reports_edits_and_ignores_noise(remote_pair):
    _origin, ubuntu, _mac = remote_pair
    (ubuntu / "node_modules" / "pkg").mkdir(parents=True)
    (ubuntu / "datasets").mkdir()
    seen: list[str] = []
    watcher = watch.Watcher(seen.append)
    assert watcher.available and watcher.watch(ubuntu, ["datasets/"])
    watcher.start()

    def wait(expect: bool) -> bool:
        deadline = time.time() + (2 if expect else 0.6)
        while time.time() < deadline:
            if seen:
                return True
            time.sleep(0.05)
        return False

    try:
        gitsync.collect(ubuntu)  # our own `git status` must not look like a change
        (ubuntu / "node_modules" / "pkg" / "index.js").write_text("x")
        (ubuntu / "datasets" / "big.bin").write_text("x")
        (ubuntu / ".git" / "FETCH_HEAD").write_text("x")
        assert not wait(False), seen
        (ubuntu / "a.txt").write_text("edited\n")
        assert wait(True) and seen[0] == str(ubuntu)
        seen.clear()
        (ubuntu / "src").mkdir()
        time.sleep(0.3)
        seen.clear()
        (ubuntu / "src" / "new.py").write_text("print()\n")  # a directory created after the watch began
        assert wait(True)
        seen.clear()
        sh(ubuntu, "commit", "-qam", "human commit")  # commits made by hand are noticed too
        assert wait(True)
    finally:
        watcher.stop()
    assert watcher.stats()["available"] is False  # stopped cleanly


def test_idle_daemon_makes_no_git_calls_for_a_watched_repository(remote_pair, monkeypatch):
    _origin, ubuntu, _mac = remote_pair
    clock = FakeClock()
    daemon = Daemon(clock=clock)
    try:
        if not daemon.watcher.watch(ubuntu):
            pytest.skip("no native file watching here")
        cfg = config.load()
        daemon._sync_one(cfg, ubuntu, MANUAL, False)
        calls = []
        real = gitsync.git
        monkeypatch.setattr(gitsync, "git", lambda *a, **k: calls.append(a) or real(*a, **k))
        for _ in range(20):
            clock.advance(10)
            daemon._sync_one(cfg, ubuntu, MANUAL, False)
        assert calls == []  # 200 idle seconds: zero subprocesses, zero network
        daemon.dirty.add(str(ubuntu))  # what the watcher does on a file event
        daemon._sync_one(cfg, ubuntu, MANUAL, False)
        assert calls
    finally:
        daemon.watcher.stop()


# ── project registration ────────────────────────────────────────────────────


def test_scan_is_bounded_previews_and_never_takes_control(sandbox, remote_pair, capsys):
    import shutil

    _origin, ubuntu, _mac = remote_pair
    for where in ("Projects/active/managed", "Projects/experiments/found", "Desktop/elsewhere", "Projects/experiments/deep/nested"):
        shutil.copytree(ubuntu, sandbox / where)
    cfg = config.load()
    found = projects.scan(cfg)
    assert [p.rsplit("/", 1)[-1] for p in found["managed"]] == ["managed"]
    assert [p.rsplit("/", 1)[-1] for p in found["unmanaged"]] == ["found"]  # not ~/Desktop, not nested: no crawling
    assert cli.main(["project", "scan"]) == 0
    out = capsys.readouterr().out
    assert "1 Git repositories found outside SUW management." in out and "suw project add <path>" in out
    assert [p.name for p in projects.discover(config.load())] == ["managed"]  # preview only
    assert cli.main(["project", "scan", "--add"]) == 1  # non-interactive without --yes: refuses to mass-enable
    assert cli.main(["project", "add", str(sandbox / "Projects/experiments/found")]) == 0
    assert {p.name for p in projects.discover(config.load())} == {"managed", "found"}
    assert cli.main(["project", "remove", "managed"]) == 0
    assert {p.name for p in projects.discover(config.load())} == {"found"}
    assert (sandbox / "Projects/active/managed/.git").exists()  # un-managing never touches the repository
    assert cli.main(["project", "autosync", "off", "found"]) == 0
    assert gitsync.project_policy(sandbox / "Projects/experiments/found", config.load().data)["enabled"] is False


def test_shared_project_list_never_contains_credentials(sandbox, remote_pair):
    import shutil

    _origin, ubuntu, _mac = remote_pair
    shared = paths.shared_dir()
    shared.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(shared)], check=True)
    for name, url in (("clean", "https://github.com/me/clean.git"), ("leaky", "https://me:ghp_secrettokenvalue1234567890abcdefgh@github.com/me/leaky.git")):
        shutil.copytree(ubuntu, sandbox / "Projects" / "active" / name)
        sh(sandbox / "Projects" / "active" / name, "remote", "set-url", "origin", url)
    assert projects.publish(config.load()) and not projects.publish(config.load())
    text = projects.catalog_file().read_text()
    assert "clean" in text and "leaky" not in text and "ghp_" not in text
    shutil.rmtree(sandbox / "Projects" / "active" / "clean")
    assert [(n, r) for n, r, _p in projects.missing(config.load())] == [("clean", "https://github.com/me/clean.git")]


# ── clipboard ───────────────────────────────────────────────────────────────


def test_osc52_sequence_and_capability_model(monkeypatch):
    assert clipboard.osc52("hi") == "\033]52;c;aGk=\a"
    monkeypatch.setattr(clipboard, "vte_version", lambda: (0, 76))
    assert clipboard.terminal_supports_osc52("gnome-terminal") is False
    monkeypatch.setattr(clipboard, "vte_version", lambda: (0, 78))
    assert clipboard.terminal_supports_osc52("gnome-terminal") is True
    assert clipboard.terminal_supports_osc52("wezterm") is True and clipboard.terminal_supports_osc52("mystery") is None
    result = clipboard.self_test(via="bad host;rm")
    assert result["ok"] is False  # never passes without proof, never executes a malformed alias


def test_tmux_forwards_osc52_to_the_outer_terminal(tmp_path):
    """Real tmux with the shipped config, inside a pty we own: an application's copy must
    come out the other side as an OSC 52 sequence addressed to the outer terminal."""
    import base64
    import pty
    import select
    import shutil

    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    token = base64.b64encode(b"suw-clip-proof").decode()
    inner = f"sleep 1; printf '\\033]52;c;{token}\\a'; sleep 1"
    conf = paths.resources() / "dotfiles" / "tmux" / "tmux.conf"
    pid, fd = pty.fork()
    if pid == 0:
        env = {"TERM": "xterm-256color", "PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMUX_TMPDIR": str(tmp_path), "SSH_CONNECTION": "1 2 3 4"}
        os.execvpe("tmux", ["tmux", "-L", "suw-test", "-f", str(conf), "new-session", inner], env)
    data = b""
    deadline = time.time() + 8
    try:
        while time.time() < deadline and token.encode() not in data:
            ready, _, _ = select.select([fd], [], [], 0.3)
            if ready:
                try:
                    data += os.read(fd, 65536)
                except OSError:
                    break
    finally:
        subprocess.run(["tmux", "-L", "suw-test", "kill-server"], env={"PATH": os.environ["PATH"], "TMUX_TMPDIR": str(tmp_path)}, capture_output=True)
        try:
            os.close(fd)
            os.waitpid(pid, 0)
        except OSError:
            pass
    assert b"\x1b]52;" in data and token.encode() in data


# ── leaving Workstation Mode: save / close ──────────────────────────────────


def test_mode_off_can_save_everything_and_close_only_workstation_windows(sandbox, remote_pair, monkeypatch):
    import shutil

    _origin, ubuntu, _mac = remote_pair
    project = sandbox / "Projects" / "active" / "demo"
    shutil.copytree(ubuntu, project)
    (project / "unsaved.py").write_text("x = 1\n")
    detached = []
    monkeypatch.setattr(apps, "have", lambda tool: True)
    monkeypatch.setattr(apps, "tmux_attached", lambda session: True)
    monkeypatch.setattr(apps, "editor", lambda cfg: "code")
    monkeypatch.setattr(apps, "run", lambda cmd, **kw: detached.append(cmd) or type("R", (), {"ok": True, "out": ""})())
    config.set_value("workstation.launch", {"editor": False, "terminal": False, "servers": False, "browser": False, "files": False}, "local")
    cfg = config.load()
    modes.on(cfg)

    steps = modes.off(cfg, save=True, close=True)
    assert steps[0] == "saved: 1 project(s) committed and pushed"
    assert gitsync.collect(project).clean and gitsync.collect(project).ahead == 0  # really saved
    assert [cmd[:2] for cmd in detached if cmd[0] == "tmux"] == [["tmux", "detach-client"], ["tmux", "detach-client"]]
    assert not any("kill" in part for cmd in detached for part in cmd)  # sessions survive
    shell = state.load("shell")
    assert shell["mode"] == "DEFAULT" and shell["close_id"] > 0
    classes = [rule.get("class", "") for rule in shell["close"]]
    assert "^suw-main$" in classes and "^suw-servers$" in classes
    # Classes the user's own windows share travel under a separate key: a desktop helper that
    # cannot tell whose window it is never sees them, so it can only close our terminals.
    assert "^[Cc]ode$" not in classes and [rule["class"] for rule in shell["close_owned"]] == ["^[Cc]ode$"]
    everything = classes + [rule["class"] for rule in shell["close_owned"]]
    assert not any("chrom" in c or "firefox" in c for c in everything)  # the browser is left alone by default

    modes.on(cfg)
    assert state.load("shell")["close"] == [] and state.load("shell")["close_owned"] == [] and state.load("shell")["close_id"] == 0
    assert modes.off(cfg)[-1].startswith("apps, browser tabs")  # plain off closes nothing
    assert state.load("shell")["close"] == [] and state.load("shell")["close_owned"] == []


def test_an_editor_the_user_already_had_open_is_never_closed(sandbox, monkeypatch):
    monkeypatch.setattr(apps, "have", lambda tool: tool != "tmux")
    monkeypatch.setattr(apps, "editor", lambda cfg: "code")
    monkeypatch.setattr(apps, "running", lambda name: 1 if name == "code" else 0)  # open before the mode started
    config.set_value("workstation.launch", {"editor": True, "terminal": False, "servers": False, "browser": False, "files": False}, "local")
    cfg = config.load()
    modes.on(cfg)
    modes.off(cfg, close=True)
    shell = state.load("shell")
    classes = [rule.get("class", "") for rule in shell["close"]]
    assert "^suw-main$" in classes and shell["close_owned"] == []


def test_leave_summary_counts_work_in_flight_without_calling_it_an_error(sandbox, remote_pair, monkeypatch):
    import shutil

    from suw.ui import exit_dialog

    _origin, ubuntu, _mac = remote_pair
    project = sandbox / "Projects" / "active" / "demo"
    shutil.copytree(ubuntu, project)
    (project / "draft.py").write_text("x = 1\n")
    monkeypatch.setattr(apps, "running", lambda name: {"claude": 1, "ssh": 2}.get(name, 0))
    summary = modes.leave_summary(config.load())
    assert (summary["uncommitted"], summary["claude"], summary["ssh"], summary["pending_sync"]) == (1, 1, 2, 0)
    rows = dict(exit_dialog.summary_lines(summary))
    assert rows["Projects with uncommitted changes"] == "1" and rows["SSH sessions open"] == "2"
    assert (project / "draft.py").exists()  # looking never touches anything


def test_mode_on_opens_the_shared_work_folder(sandbox, monkeypatch):
    from suw.core import proc

    work = sandbox / "Desktop" / "Work"
    work.mkdir(parents=True)
    opened = []
    monkeypatch.setattr(apps, "have", lambda tool: tool in ("code", "wezterm"))
    monkeypatch.setattr(apps, "running", lambda name: 0)
    monkeypatch.setattr(apps, "spawn", lambda cmd, **kw: opened.append(list(cmd)) or True)
    config.set_value("workstation.launch", {"editor": True, "terminal": True, "servers": False, "browser": False, "files": False}, "local")
    cfg = config.load()
    steps = modes.on(cfg)
    assert ["code", str(work)] in opened
    terminal = next(cmd for cmd in opened if cmd[0] == "wezterm")
    assert terminal[terminal.index("--cwd") + 1] == str(work) and "suw-main" in terminal
    assert any(step.startswith("work folder:") for step in steps)
    before = len(opened)
    modes.on(cfg), modes.on(cfg)  # idempotent: nothing is launched twice
    assert len(opened) == before
