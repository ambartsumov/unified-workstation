"""Truthfulness and safety details: what doctor may claim, what the leave dialog offers,
what the password helper refuses to answer."""

from __future__ import annotations

import time

import pytest

from suw.cli import main as cli
from suw.core import askpass, config, doctor, health, inventory, proc, state, status
from suw.integrations import ssh as sshcfg
from suw.integrations import tailscale
from suw.ui import exit_dialog

TAILNET = {"installed": True, "running": True, "online": True, "health": [], "magic_dns": "tail1234.ts.net", "self": {"dns": "ubuntu.tail1234.ts.net"}, "peers": []}
CUT_OFF = {**TAILNET, "online": False, "health": ["Unable to connect to the Tailscale coordination server"]}


def by_title(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.title: c for c in checks}


# ── Tailscale: running is not the same as connected ─────────────────────────


def test_cut_off_and_tailnet_addresses_are_pure():
    assert tailscale.cut_off(CUT_OFF) and not tailscale.cut_off(TAILNET)
    assert not tailscale.cut_off({"installed": True, "running": False})
    assert not tailscale.cut_off({"installed": True, "running": True})  # an older cached snapshot claims nothing
    for host, expected in (("sam.tail1234.ts.net", True), ("100.66.16.100", True), ("sam", True), ("203.0.113.7", False), ("example.com", False), ("", False)):
        assert tailscale.on_tailnet(host, TAILNET) is expected, host


def test_doctor_does_not_blame_the_server_for_this_machines_network(monkeypatch):
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="sam.tail1234.ts.net", ssh_user="me")
    inventory.save(inv)
    offline = {"online": False, "status": health.OFFLINE, "error": "ssh: connect to host sam.tail1234.ts.net port 22: Connection timed out"}
    monkeypatch.setattr(health, "probe_remote", lambda *a, **k: dict(offline))

    monkeypatch.setattr(tailscale, "status", lambda: dict(CUT_OFF))
    found = by_title(doctor.check_network(config.load()))
    assert found["Tailscale is running but cut off from the tailnet"].level == doctor.WARN
    assert found["Home cannot be tested from here"].level == doctor.NA
    assert not any(title.startswith("Home offline") for title in found)

    monkeypatch.setattr(tailscale, "status", lambda: dict(TAILNET))
    found = by_title(doctor.check_network(config.load()))
    assert found["Tailscale"].level == doctor.PASS and found["Home offline"].level == doctor.WARN  # now it IS the server


def test_a_server_on_a_public_address_is_still_judged_when_the_tailnet_is_down(monkeypatch):
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="203.0.113.7", ssh_user="me")
    inventory.save(inv)
    monkeypatch.setattr(health, "probe_remote", lambda *a, **k: {"online": False, "status": health.OFFLINE, "error": "timed out"})
    monkeypatch.setattr(tailscale, "status", lambda: dict(CUT_OFF))
    assert by_title(doctor.check_network(config.load()))["Home offline"].level == doctor.WARN


def test_status_names_the_real_cause_once(monkeypatch):
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="sam.tail1234.ts.net", ssh_user="me")
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    monkeypatch.setattr(tailscale, "status", lambda: dict(CUT_OFF))
    monkeypatch.setattr(health, "probe_remote", lambda *a, **k: {"online": False, "status": health.OFFLINE, "error": "timed out", "checked": time.time()})
    snap = status.build(config.load(), live=True)
    assert snap["components"]["tailscale"] == health.DEGRADED and snap["tailscale"]["cut_off"] is True
    assert [item for item in snap["attention"] if "tailscale" in item] and not any(item.startswith("home:") for item in snap["attention"])
    assert snap["overall"] == "NEEDS ATTENTION"


# ── doctor claims only what was checked ─────────────────────────────────────


def test_an_old_clipboard_proof_is_not_presented_as_current():
    cfg = config.load()
    state.save("clipboard_test", {"ok": True, "via": "home", "terminal": "wezterm", "when": time.time() - 3600})
    assert any(c.title == "Clipboard round-trip proven" and "today" in c.detail for c in doctor.check_clipboard(cfg))
    state.save("clipboard_test", {"ok": True, "via": "home", "terminal": "wezterm", "when": time.time() - 90 * 86400})
    found = by_title(doctor.check_clipboard(cfg))
    assert found["Clipboard round-trip not proven recently"].level == doctor.NONE and "90 day(s) ago" in found["Clipboard round-trip not proven recently"].detail
    state.save("clipboard_test", {"ok": True, "via": "home"})
    assert "Clipboard round-trip not proven recently" in by_title(doctor.check_clipboard(cfg))


def test_startup_check_reports_a_service_that_would_not_return_after_a_reboot():
    found = doctor.check_startup(config.load())  # the sandbox session manager knows no unit
    assert [c.level for c in found] == [doctor.WARN] and found[0].title == "Background daemon does not start at login" and found[0].fix == "suw bootstrap"


def test_work_folder_without_a_peer_is_not_called_synced():
    from suw.ui import text

    rows = "\n".join(text.extra_rows({"work": {"state": "NO_PEER"}, "peripherals": {"state": "UNPAIRED"}}))
    assert "SYNCED" not in rows and rows.count("NOT CONFIGURED") == 2


def test_bootstrap_rejects_an_unknown_step(capsys):
    assert cli.main(["bootstrap", "--only", "sssh"]) == 1
    assert "unknown bootstrap step: sssh" in capsys.readouterr().err


# ── leaving Workstation Mode ────────────────────────────────────────────────


def test_leave_dialog_promises_only_what_happens():
    note = exit_dialog.NOTE.lower()
    assert "automatic checkpoints" in note and "stays exactly as it is" in note and "only windows workstation mode opened" in note
    rows = dict(exit_dialog.summary_lines({"uncommitted": 2, "claude": 1, "ssh": 3, "pending_sync": 4, "conflicts": 1}))
    assert rows == {"Projects with uncommitted changes": "2", "Claude Code sessions running": "1", "SSH sessions open": "3", "Work folder items still syncing": "4", "Conflict copies to review": "1"}


@pytest.mark.parametrize("rc,out,expected", [(0, "Save & Close\n", "close"), (1, "", "cancel"), (0, "something else", "cancel"), (127, "", "keep")])
def test_macos_dialog_has_two_answers_and_cancel_is_the_default(monkeypatch, rc, out, expected):
    seen = []
    monkeypatch.setattr(proc, "run", lambda cmd, **kw: seen.append(cmd) or proc.Result(rc, out, ""))
    assert exit_dialog.ask_macos(config.load(), {"uncommitted": 1}) == expected
    script = seen[0][2]
    assert 'buttons {"Cancel", "Save & Close"}' in script and "keep" not in script.lower()
    assert "Projects with uncommitted changes: 1" in seen[0][3]  # facts travel as an argument, never spliced into the script


# ── password sign-in helper ─────────────────────────────────────────────────


def test_helper_answers_password_prompts_only(monkeypatch, capsys):
    monkeypatch.setattr(askpass.secrets, "get", lambda ref: "hunter2-not-real" if ref == "ssh-password/home" else None)
    assert askpass.answer("sam@home's password: ", "home") == "hunter2-not-real"
    for prompt in (
        "The authenticity of host 'home' can't be established.\nED25519 key fingerprint is SHA256:abc.\nAre you sure you want to continue connecting (yes/no/[fingerprint])? ",
        "Enter passphrase for key '/home/me/.ssh/id_ed25519': ",
        "Please type 'yes', 'no' or the fingerprint: ",
        "",
    ):
        assert askpass.answer(prompt, "home") is None, prompt
    assert askpass.answer("sam@cloud's password: ", "cloud") is None  # nothing stored for that role
    assert askpass.answer("sam@home's password: ", "") is None
    assert askpass.main(["Are you sure you want to continue connecting (yes/no/[fingerprint])? "]) == 1
    assert capsys.readouterr().out == ""


def test_only_a_public_key_line_is_ever_installed():
    for bad in ("", "rm -rf ~", "ssh-ed25519 AAAA\nssh-ed25519 BBBB", "-----BEGIN OPENSSH PRIVATE KEY-----"):
        res = askpass.install_key("home", "home", bad)
        assert not res.ok and res.err == "not a public key"


def test_password_sign_in_never_accepts_an_unknown_server(ssh_lab, sandbox, monkeypatch):
    """Real ssh, real helper: an unverified host key stops the connection before any password."""
    server = ssh_lab("home")
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user=ssh_lab.user, ssh_port=server.port)
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    known = sandbox / ".ssh" / "known_hosts"
    res = askpass.install_key("home", "home", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA test@suw")
    assert not res.ok and health.failure_status(res.err) == health.UNTRUSTED
    assert not known.exists() or str(server.port) not in known.read_text()  # nothing was trusted on the way
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIA test@suw" not in (server.root / "authorized_keys").read_text()


# ── the desktop helper ──────────────────────────────────────────────────────


def test_a_helper_older_than_the_file_on_disk_is_reported_stale(sandbox, monkeypatch):
    from suw.integrations import gnome

    target = sandbox / ".local/share/gnome-shell/extensions" / gnome.EXTENSION_UUID
    target.mkdir(parents=True)
    (target / "extension.js").write_text("// helper")
    monkeypatch.setattr(gnome, "run", lambda cmd, **kw: proc.Result(0, "  State: ACTIVE\n", ""))
    monkeypatch.setattr(gnome, "shell_started", lambda: time.time() - 3600)
    assert gnome.extension_state() == "stale"  # written after the session started
    monkeypatch.setattr(gnome, "shell_started", lambda: time.time() + 3600)
    assert gnome.extension_state() == "active"
    monkeypatch.setattr(gnome, "shell_started", lambda: 0.0)
    assert gnome.extension_state() == "active"  # unknown start time claims nothing
    monkeypatch.setattr(gnome, "run", lambda cmd, **kw: proc.Result(1, "", ""))
    assert gnome.extension_state() == "installed"


# ── one mode switch at a time ───────────────────────────────────────────────


def test_a_second_switch_while_one_is_running_does_nothing(capsys, monkeypatch):
    from suw.core import modes

    monkeypatch.setattr(modes, "BOUNCE", 0)
    with modes.switching() as mine:
        assert mine is True
        with modes.switching() as theirs:
            assert theirs is False
        assert cli.main(["mode", "on"]) == 0
        assert "already in progress" in capsys.readouterr().out and modes.current() == modes.DEFAULT
    assert cli.main(["mode", "on"]) == 0 and modes.current() == modes.WORKSTATION  # the lock is released


def test_a_key_repeat_does_not_flip_the_mode_back(capsys):
    from suw.core import modes

    assert cli.main(["mode", "toggle"]) == 0 and modes.current() == modes.WORKSTATION
    assert cli.main(["mode", "toggle"]) == 0 and modes.current() == modes.WORKSTATION  # within the bounce window
    assert "nothing was done twice" in capsys.readouterr().out
    data = modes.load()
    data["switched"] = time.time() - 5
    state.save("mode", data)
    assert cli.main(["mode", "toggle"]) == 0 and modes.current() == modes.DEFAULT
    assert cli.main(["mode", "off"]) == 0 and cli.main(["mode", "on"]) == 0  # explicit on/off are never debounced
    assert modes.current() == modes.WORKSTATION


def test_parallel_activation_launches_everything_once(sandbox):
    """Ten `suw mode on` at the same instant (a held key): one set of apps, not ten."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    log = sandbox / "spawned.log"
    fake = sandbox / "fakebin"
    fake.mkdir()
    for tool in ("wezterm", "code", "xdg-open", "chromium", "firefox", "google-chrome", "notify-send", "nautilus"):
        (fake / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        (fake / tool).chmod(0o755)
    (fake / "tmux").write_text('#!/bin/sh\ncase "$1" in has-session|list-clients) exit 1 ;; esac\nexit 0\n')
    (fake / "tmux").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": str(repo)}
    procs = [subprocess.Popen([sys.executable, "-m", "suw.cli.main", "mode", "on", "--json"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(10)]
    results = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
    assert sum(1 for r in results if not r.get("ignored")) >= 1
    lines = log.read_text().splitlines() if log.exists() else []
    assert len([ln for ln in lines if ln.startswith("code")]) == 1, lines
    assert len([ln for ln in lines if ln.startswith("wezterm") and "suw-main" in ln]) == 1, lines
    assert len([ln for ln in lines if ln.startswith("wezterm") and "suw-servers" in ln]) == 1, lines


# ── a shared inventory is input, not trusted configuration ──────────────────


def test_a_tampered_inventory_cannot_inject_ssh_configuration():
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="home.example.net", ssh_user="me")
    cases = {
        "evil-host": {"host": "x.example.net\n    ProxyCommand touch /tmp/pwned"},
        "evil-user": {"host": "a.example.net", "ssh_user": "me\nProxyCommand id"},
        "evil-port": {"host": "b.example.net", "ssh_port": "22\nProxyCommand id"},
        "evil-key": {"host": "c.example.net", "ssh_identity": "~/.ssh/id\n    LocalCommand id"},
        "evil-opt": {"host": "-oProxyCommand=id"},
        "evil name\nHost *": {"host": "d.example.net"},
    }
    for name, fields in cases.items():
        inv["devices"][name] = {"role": "server", **fields}
    inv["cloud"]["nodes"]["cloud-x"] = {"state": "active", "endpoint": "203.0.113.9\nProxyCommand id", "user": "root", "port": 22, "host_key": "ssh-ed25519 AAAA\nevil ssh-ed25519 BBBB"}
    inv["cloud"]["current"] = "cloud-x"
    conf = sshcfg.render(inv, "ubuntu")
    assert "ProxyCommand" not in conf and "LocalCommand" not in conf and "Host *" not in conf
    assert "Host home\n    HostName home.example.net\n    User me" in conf  # the honest entry is still there
    assert "Host cloud" not in conf and conf.count("# skipped") == 7
    assert sshcfg.render_known_hosts(inv) == ""
    for line in conf.splitlines():
        assert line.startswith(("#", "Host ", "    ")) or line == "", line
    assert health._SERVICE.match("nginx.service") and not health._SERVICE.match("-n") and not health._SERVICE.match("a;b")
