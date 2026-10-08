"""The servers terminal: who am I connected to, and the windows that stay connected.

The tmux part runs against a real tmux server that lives in the test's own socket directory
(see conftest): nothing here can reach the developer's sessions.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from suw.core import config, inventory, status
from suw.integrations import apps
from suw.ui import text

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


def enroll(**devices):
    inv = inventory.load()
    for name, fields in devices.items():
        inventory.ensure_device(inv, name, **fields)
    inventory.save(inv)
    return inv


def test_only_terminal_ssh_sessions_count_and_options_are_skipped():
    listing = "\n".join(
        [
            "pts/3    ssh nas",
            "pts/4    /usr/bin/ssh -p 2222 -o ServerAliveInterval=15 -i /k/id sam@home",
            "pts/5    ssh -A -J jump mac uptime",
            "?        ssh -o BatchMode=yes home sh -s",  # the daemon's probe: no terminal
            "??       ssh cloud",  # macOS spelling of "no terminal"
            "pts/6    sshd: sam@pts/6",
            "pts/7    vim ssh home",
            "pts/8    ssh",
        ]
    )
    assert apps.ssh_destinations(listing) == ["nas", "sam@home", "mac"]


def test_sessions_are_matched_through_the_users_own_aliases(monkeypatch):
    hosts = {"home": "sam.tailnet.ts.net", "nas": "sam.tailnet.ts.net", "mac": "mac.tailnet.ts.net", "cloud": "cloud", "other": "example.org"}
    asked = []
    monkeypatch.setattr(apps, "_resolved", {})
    monkeypatch.setattr(apps, "ssh_host", lambda dest: asked.append(dest) or hosts.get(dest.split("@")[-1], ""))
    monkeypatch.setattr(apps, "run", lambda *a, **k: type("R", (), {"out": "pts/1 ssh nas\npts/2 ssh root@home\npts/3 ssh other\npts/4 ssh cloud\n? ssh mac true\n"})())
    assert apps.ssh_sessions(["home", "mac", "cloud"]) == {"home": 2, "mac": 0, "cloud": 1}
    assert apps.ssh_host.__name__ == "<lambda>" and "; rm" not in asked
    monkeypatch.undo()
    assert apps.ssh_host("-oProxyCommand=evil") == "" and apps.ssh_host("a b") == ""  # never handed to ssh


def test_only_home_and_other_workstations_are_connected_automatically():
    cfg = config.load()
    assert apps.linked(cfg, inventory.load()) == []
    inv = enroll(home={"role": "home", "host": "home.ts.net"}, mac={"role": "workstation", "tailscale_name": "mac.ts.net"}, spare={"role": "workstation"})
    inventory.ensure_device(inv, cfg.device, role="workstation", tailscale_name="me.ts.net")
    inv["cloud"] = {"current": "gpu-1", "nodes": {"gpu-1": {"state": "active", "endpoint": "203.0.113.7", "user": "root"}}}
    assert apps.linked(cfg, inv) == ["home", "mac"]  # not this machine, not a machine without an address, never cloud
    names = [name for name, _ in apps.server_windows(cfg, inv)]
    assert names == ["status", "home", "mac"]
    assert all(" link " in command for name, command in apps.server_windows(cfg, inv) if name != "status")


def test_panel_uses_the_names_you_type_and_says_whether_you_are_connected():
    enroll(home={"role": "home", "host": "home.ts.net"}, mac={"role": "workstation", "tailscale_name": "mac.ts.net"})
    snap = status.build(config.load())
    snap["servers"]["home"].update(status="ONLINE", cpu_pct=4, ram_used_pct=48, disk_used_pct=13, uptime_s=97631)
    for ws in snap["workstations"]:
        if ws["name"] == "mac":
            ws.update(online=True, enrolled=True, status="ONLINE")
    snap["sessions"] = {"home": 2, "mac": 0}
    panel = text.render_status(snap)
    home = next(line for line in panel.splitlines() if " home " in line)
    assert "connected ×2" in home and "CPU 4%" in home and "Disk 13%" in home and "up 1d 3h" in home
    assert "not connected" in next(line for line in panel.splitlines() if " mac " in line)
    assert "to rent one, type: ssh cloud" in next(line for line in panel.splitlines() if " cloud " in line)
    assert "updated " in panel.splitlines()[0]
    assert not any(word in panel for word in ("Home ", "Mac ", "Cloud "))
    summary = text.render_summary(snap)
    assert "home " in summary and "cloud " in summary and "Home " not in summary and "Cloud " not in summary


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout


@needs_tmux
def test_servers_session_gets_its_windows_and_repairs_the_idle_ones(monkeypatch):
    cfg = config.load()
    enroll(home={"role": "home", "host": "home.ts.net"}, mac={"role": "workstation", "tailscale_name": "mac.ts.net"})
    # stand-ins for `suw status --watch` and `suw link <name>`: something that keeps running
    monkeypatch.setattr(apps, "server_windows", lambda cfg, inv: [("status", "sleep 300"), *[(n, "sleep 300") for n in apps.linked(cfg, inv)]])
    monkeypatch.setattr(apps, "have", lambda tool: tool == "tmux")
    assert apps.ensure_servers_session(cfg, "servers")
    listing = lambda: sorted(tuple(line.split("\t")) for line in tmux("list-panes", "-s", "-t", "=servers", "-F", "#{window_name}\t#{pane_current_command}").splitlines())  # noqa: E731
    assert listing() == [("home", "sleep"), ("mac", "sleep"), ("status", "sleep")]

    assert apps.ensure_servers_session(cfg, "servers") and len(listing()) == 3  # a second press adds nothing
    # the home window fell back to a prompt, the mac window is busy with the user's own command
    tmux("respawn-pane", "-k", "-t", "=servers:home", "sh")
    tmux("respawn-pane", "-k", "-t", "=servers:mac", "cat")
    tmux("kill-window", "-t", "=servers:status")
    deadline = time.time() + 5
    while time.time() < deadline and ("home", "sh") not in listing():
        time.sleep(0.1)
    assert apps.ensure_servers_session(cfg, "servers")
    deadline = time.time() + 5
    while time.time() < deadline and ("home", "sleep") not in listing():
        time.sleep(0.1)
    assert listing() == [("home", "sleep"), ("mac", "cat"), ("status", "sleep")]  # repaired, restored, left alone


@needs_tmux
def test_tests_cannot_reach_the_real_tmux_server(sandbox):
    import os

    assert os.environ["TMUX_TMPDIR"].startswith("/tmp/suwt-") or "suwt-" in os.environ["TMUX_TMPDIR"]
    assert "TMUX" not in os.environ
    subprocess.run(["tmux", "new-session", "-d", "-s", "probe", "sleep 30"], check=True)
    assert os.listdir(os.environ["TMUX_TMPDIR"])  # the server's socket lives inside the sandbox directory
