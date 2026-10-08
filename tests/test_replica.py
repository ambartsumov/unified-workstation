"""Home as the permanent third copy of the work folder.

Two layers, both real:
  * three Syncthing instances on 127.0.0.1 ("ubuntu", "mac" and a receive-only "home")
    prove the semantics the replica is built on;
  * `suw home replica setup|status|remove` and the version listing / restore run over a real
    SSH connection to an unprivileged sshd whose sessions live in a sandbox home directory.
    Only the session manager on that "server" is a stub.
"""

from __future__ import annotations

import os
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_worksync import Station, cfg_for, until

from suw.core import inventory
from suw.integrations import replica, worksync
from suw.integrations import ssh as sshcfg

needs_syncthing = pytest.mark.skipif(not worksync.binary(), reason="syncthing is not installed")


# ── pure ────────────────────────────────────────────────────────────────────


def test_version_names_and_listing_are_parsed_defensively():
    assert replica.original_of("notes~20261008-150102.md") == "notes.md"
    assert replica.original_of("a/b/Заметки~20261008-150102.txt") == "a/b/Заметки.txt"
    listing = "1759900000.5\t12\tnotes~20261008-150102.md\n1759900001\t7\t../../etc/passwd\n1759900002\t7\t/abs~20261008-150102\nbroken line\nx\ty\tz\n"
    rows = replica.parse_versions(listing)
    assert [r["path"] for r in rows] == ["notes~20261008-150102.md"] and rows[0]["original"] == "notes.md" and rows[0]["bytes"] == 12
    assert replica.parse_versions(listing, prefix="other/") == []


def test_replica_folder_must_be_a_plain_path():
    for bad in ("work", "~/../etc", "/srv/$(id)", "~/a b", "/x;y", "~/w/../../z"):
        with pytest.raises(replica.ReplicaError):
            replica.folder(cfg_for(Path("/tmp/w"), replica_path=bad))
    assert replica.folder(cfg_for(Path("/tmp/w"), replica_path="~/work")) == "~/work"


@needs_syncthing
def test_replica_shape_receives_only_and_keeps_dated_versions(tmp_path):
    inst = worksync.Instance(tmp_path / "st", tmp_path / "w", gui="127.0.0.1:1", listen=["tcp://127.0.0.1:1"], name="x")
    inst.generate()
    cfg = cfg_for(tmp_path / "w", replica_days=90, replica_port=22123)
    peer = worksync.Peer("ubuntu", "A" * 7 + "-" + "B" * 7, ["dynamic"])
    for bind, expected in (("100.64.0.9", "tcp://100.64.0.9:22123"), ("", "tcp://0.0.0.0:22123"), ("203.0.113.5; rm", "tcp://0.0.0.0:22123")):
        top = ET.fromstring(replica.shaped(cfg, inst.config_file.read_text(), inst.device_id(), "/srv/work", [peer], bind))
        folder = top.find("folder")
        assert folder.get("type") == "receiveonly" and folder.get("path") == "/srv/work"
        versioning = folder.find("versioning")
        assert versioning.get("type") == "simple" and {p.get("key"): p.get("val") for p in versioning.findall("param")} == {"keep": "20", "cleanoutDays": "90"}
        assert expected in [e.text for e in top.find("options").findall("listenAddress")]
        assert top.find("gui/address").text.startswith("127.0.0.1:")
        assert {d.get("id") for d in folder.findall("device")} == {inst.device_id(), peer.device_id}


# ── three live instances ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def trio(tmp_path_factory):
    if not worksync.binary():
        pytest.skip("syncthing is not installed")
    base = tmp_path_factory.mktemp("trio")
    ubuntu, mac, home = Station(base, "ubuntu"), Station(base, "mac"), Station(base, "home")
    for station in (ubuntu, mac):
        worksync.write_ignore(station.cfg, [], station.work)
        station.inst.apply([s.peer() for s in (ubuntu, mac, home) if s is not station])
    shaped = home.inst.shape_text(home.inst.config_file.read_text(), home.id, [ubuntu.peer(), mac.peer()], replica_days=30)
    home.inst.config_file.write_text(shaped)
    (home.work / ".stfolder").mkdir(exist_ok=True)
    for station in (ubuntu, mac, home):
        station.start()
    yield ubuntu, mac, home
    for station in (ubuntu, mac, home):
        station.stop()


def has(station: Station, rel: str, body: str):
    return lambda: (station.work / rel).is_file() and station.read(rel) == body


def test_home_keeps_a_third_copy_and_never_originates_a_change(trio):
    ubuntu, mac, home = trio
    ubuntu.write("proj/.claude/memory/MEMORY.md", "# память\n")
    ubuntu.write("proj/.env", "TOKEN=replica-secret-55\n")
    ubuntu.write("report.md", "first\n")
    ubuntu.inst.rescan()
    for station in (mac, home):
        until(has(station, "report.md", "first\n"), 90, f"{station.name} receives the file")
        until(has(station, "proj/.claude/memory/MEMORY.md", "# память\n"), 60, f"{station.name} receives Claude state")
        until(has(station, "proj/.env", "TOKEN=replica-secret-55\n"), 60, f"{station.name} receives the work secret")

    # a replaced file and a deleted file both stay recoverable on Home
    mac.write("report.md", "second\n")
    mac.inst.rescan()
    until(has(home, "report.md", "second\n"), 90, "home receives the edit")
    until(has(ubuntu, "report.md", "second\n"), 90, "ubuntu receives the edit")
    time.sleep(1.2)  # a version's name carries a one-second timestamp: two in one second would share it
    (ubuntu.work / "report.md").unlink()
    ubuntu.inst.rescan()
    until(lambda: not (home.work / "report.md").exists(), 90, "the deletion reaches home")
    kept = sorted(p.read_text() for p in (home.work / ".stversions").rglob("report~*.md"))
    assert kept == ["first\n", "second\n"]  # every version, however close together

    # something written on the server itself goes nowhere
    home.write("made-on-the-server.txt", "must not spread\n")
    home.inst.rescan()
    ubuntu.write("marker.txt", "m\n")
    ubuntu.inst.rescan()
    until(has(mac, "marker.txt", "m\n"), 90, "a later change has gone round")
    until(has(home, "marker.txt", "m\n"), 90, "and reached home")
    time.sleep(2)
    assert not (ubuntu.work / "made-on-the-server.txt").exists() and not (mac.work / "made-on-the-server.txt").exists()

    for station in (ubuntu, mac, home):
        assert "replica-secret-55" not in station.log.read_text(errors="replace")


def test_home_hands_work_over_when_the_workstations_are_never_on_together(trio):
    ubuntu, mac, home = trio
    mac.stop()
    ubuntu.write("handover.txt", "written while the mac was off\n")
    ubuntu.inst.rescan()
    until(has(home, "handover.txt", "written while the mac was off\n"), 90, "home receives it")
    ubuntu.stop()
    mac.start()  # ubuntu is off now: only Home can deliver
    until(has(mac, "handover.txt", "written while the mac was off\n"), 120, "the mac gets it from home")
    mac.write("reply.txt", "answer from the mac\n")
    mac.inst.rescan()
    until(has(home, "reply.txt", "answer from the mac\n"), 90, "home receives the reply")
    mac.stop()
    ubuntu.start()
    until(has(ubuntu, "reply.txt", "answer from the mac\n"), 120, "ubuntu gets the reply from home")
    mac.start()


# ── the commands, over real SSH ─────────────────────────────────────────────


@pytest.fixture
def server(ssh_lab, sandbox, tmp_path):
    if not worksync.binary():
        pytest.skip("syncthing is not installed")
    remote_home = tmp_path / "srv-home"
    remote_home.mkdir()
    stubs = tmp_path / "srv-bin"
    stubs.mkdir()
    for name, body in {
        "systemctl": '#!/bin/sh\necho "$@" >> "$HOME/systemctl.log"\ncase " $* " in *" is-active "*) [ -f "$HOME/stopped" ] && echo inactive || echo active ;; *" disable "*) : > "$HOME/stopped" ;; esac\nexit 0\n',
        "loginctl": '#!/bin/sh\ncase " $* " in *" show-user "*) echo yes ;; esac\nexit 0\n',
        "tailscale": "#!/bin/sh\necho 100.64.0.9\n",
    }.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    sshd = ssh_lab("home", home=remote_home, path=f"{stubs}:{os.path.dirname(worksync.binary())}:/usr/bin:/bin")
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user=ssh_lab.user, ssh_port=sshd.port)
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    (sandbox / ".ssh" / "known_hosts").write_text(f"[127.0.0.1]:{sshd.port} {sshd.host_key}\n")
    probe = replica.health.ssh("home", ["sh", "-c", "'printf %s \"$HOME\"'"])
    if probe.out != str(remote_home):
        pytest.skip("this sshd does not let a session's HOME be redirected")
    return remote_home


def test_setup_status_versions_restore_and_remove(server, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    cfg = cfg_for(work, replica_path="~/work-copy", replica_days=45)
    with pytest.raises(replica.ReplicaError, match="not set up on this workstation"):
        replica.setup(cfg)
    assert replica.status(cfg) == {"state": "NOT_CONFIGURED"}
    local = worksync.instance(cfg)
    local.generate()

    done = replica.setup(cfg)
    assert done["folder"] == str(server / "work-copy") and done["peers"] == ["ubuntu"] and done["private"] is True and done["linger"] == "yes"
    remote_cfg = server / ".local/state/suw/syncthing/config.xml"
    assert oct(remote_cfg.stat().st_mode & 0o777) == "0o600"
    top = ET.parse(remote_cfg).getroot()
    folder = top.find("folder")
    assert folder.get("type") == "receiveonly" and folder.get("path") == str(server / "work-copy")
    assert folder.find("versioning").get("type") == "simple"
    assert {d.get("id") for d in folder.findall("device")} == {done["id"], local.device_id()}
    assert [e.text for e in top.find("options").findall("listenAddress")] == ["tcp://100.64.0.9:22000", "quic://100.64.0.9:22000"]
    assert top.find("options/globalAnnounceEnabled").text == "false" and top.find("options/relaysEnabled").text == "false"
    unit = (server / ".config/systemd/user/suw-syncthing.service").read_text()
    assert "serve --no-browser" in unit and "tailscaled.service" in unit
    # this workstation now treats Home as an approved peer, at its stable name
    home = inventory.load()["devices"]["home"]
    assert home["syncthing_id"] == done["id"]
    assert [(p.name, p.addresses[0]) for p in worksync.peers(cfg)] == [("home", "tcp://127.0.0.1:22000")]
    assert done["id"] in local.config_file.read_text()

    again = replica.setup(cfg)  # idempotent: same identity, same folder
    assert again["id"] == done["id"]
    assert remote_cfg.with_name("config.xml.suw-prev").exists()  # the previous configuration is kept

    assert replica.status(cfg)["state"] == "ONLINE" and replica.status(cfg)["versions"] == 0
    kept = server / "work-copy" / ".stversions"
    (kept / "docs").mkdir(parents=True)
    (kept / "docs" / "plan~20261001-101500.md").write_text("the old plan\n")
    (kept / "gone~20261002-090000.txt").write_text("deleted last week\n")
    rows = replica.versions(cfg)
    assert sorted(r["path"] for r in rows) == ["docs/plan~20261001-101500.md", "gone~20261002-090000.txt"]
    assert replica.status(cfg)["versions"] == 2

    assert Path(replica.restore(cfg, "gone~20261002-090000.txt")) == work / "gone.txt"
    assert (work / "gone.txt").read_text() == "deleted last week\n"
    (work / "docs").mkdir()
    (work / "docs" / "plan.md").write_text("the current plan\n")
    restored = Path(replica.restore(cfg, "docs/plan~20261001-101500.md"))
    assert restored.name.startswith("plan.restored-") and restored.read_text() == "the old plan\n"
    assert (work / "docs" / "plan.md").read_text() == "the current plan\n"  # never overwritten
    for bad in ("../../.ssh/authorized_keys", "/etc/passwd", "nope~20260101-000000.txt", "gone~20261002-090000.txt; id"):
        with pytest.raises(replica.ReplicaError):
            replica.restore(cfg, bad)
    assert not list((work.parent).glob("*.part"))

    assert replica.remove(cfg) is True
    assert "syncthing_id" not in inventory.load()["devices"]["home"] and worksync.peers(cfg) == []
    assert (kept / "gone~20261002-090000.txt").exists()  # nothing on the server is deleted
    assert replica.status(cfg) == {"state": "NOT_CONFIGURED"}


def test_an_unreachable_or_bare_server_is_reported_not_guessed(server, tmp_path, ssh_lab, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    cfg = cfg_for(work)
    worksync.instance(cfg).generate()
    inv = inventory.load()
    inv["devices"]["home"]["ssh_port"] = 1
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    with pytest.raises(replica.ReplicaError):
        replica.setup(cfg)
    assert "syncthing_id" not in inventory.load()["devices"]["home"] and worksync.peers(cfg) == []


def test_setup_needs_syncthing_on_the_server(ssh_lab, sandbox, tmp_path):
    if not worksync.binary() or not shutil.which("sshd"):
        pytest.skip("needs syncthing and sshd")
    remote_home = tmp_path / "bare-home"
    remote_home.mkdir()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    for tool in ("sh", "cat", "mkdir", "head", "id"):
        os.symlink(shutil.which(tool), empty / tool)
    sshd = ssh_lab("home", home=remote_home, path=str(empty))
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user=ssh_lab.user, ssh_port=sshd.port)
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    (sandbox / ".ssh" / "known_hosts").write_text(f"[127.0.0.1]:{sshd.port} {sshd.host_key}\n")
    cfg = cfg_for(tmp_path / "work")
    worksync.instance(cfg).generate()
    with pytest.raises(replica.ReplicaError, match="Syncthing is not installed on the home server"):
        replica.setup(cfg)


def test_setup_can_install_syncthing_for_the_server_user_and_refuses_a_bad_download(ssh_lab, sandbox, tmp_path):
    """The download itself is replaced by a local file server: everything else is the real
    script over real SSH — checksum check, unpacking, the binary in ~/.local/bin, the replica."""
    import hashlib
    import tarfile

    if not worksync.binary() or not shutil.which("sshd"):
        pytest.skip("needs syncthing and sshd")
    remote_home = tmp_path / "bare-home"
    remote_home.mkdir()
    stubs = tmp_path / "srv-bin"
    stubs.mkdir()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine)
    if not arch:
        pytest.skip("no official build for this architecture")
    name = f"syncthing-linux-{arch}-{replica.RELEASE}"
    mirror = tmp_path / "mirror"
    (mirror / name).mkdir(parents=True)
    shutil.copy2(os.path.realpath(worksync.binary()), mirror / name / "syncthing")
    with tarfile.open(mirror / f"{name}.tar.gz", "w:gz") as tar:
        tar.add(mirror / name, arcname=name)
    digest = hashlib.sha256((mirror / f"{name}.tar.gz").read_bytes()).hexdigest()
    sums = mirror / "sha256sum.txt.asc"
    sums.write_text(f"-----BEGIN PGP SIGNED MESSAGE-----\n\n{'0' * 64}  other.zip\n{'f' * 64}  {name}.tar.gz\n")
    for tool, body in {
        "systemctl": '#!/bin/sh\ncase " $* " in *" is-active "*) echo active ;; esac\nexit 0\n',
        "loginctl": '#!/bin/sh\ncase " $* " in *" show-user "*) echo yes ;; esac\nexit 0\n',
        "tailscale": "#!/bin/sh\necho 100.64.0.9\n",
        # curl -fsSL --max-time N -o <file> <url>: serve the file of that name from the mirror
        "curl": f'#!/bin/sh\nwhile [ $# -gt 1 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\necho "$1" >> "$HOME/downloads.log"\ncp "{mirror}/${{1##*/}}" "$out"\n',
    }.items():
        (stubs / tool).write_text(body)
        (stubs / tool).chmod(0o755)
    # a server with the usual tools and no Syncthing
    for tool in ("sh", "cat", "mkdir", "head", "id", "uname", "mktemp", "awk", "sha256sum", "tar", "gzip", "install", "rm", "cp", "mv", "sleep", "df", "find", "wc", "sort"):
        os.symlink(shutil.which(tool), stubs / tool)
    sshd = ssh_lab("home", home=remote_home, path=str(stubs))
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="127.0.0.1", ssh_user=ssh_lab.user, ssh_port=sshd.port)
    inventory.save(inv)
    sshcfg.apply(inv, "ubuntu")
    (sandbox / ".ssh" / "known_hosts").write_text(f"[127.0.0.1]:{sshd.port} {sshd.host_key}\n")
    probe = replica.health.ssh("home", ["sh", "-c", "'command -v syncthing; printf %s \"$HOME\"'"])
    if probe.out != str(remote_home):
        pytest.skip("this sshd cannot hide the system Syncthing or redirect HOME")
    cfg = cfg_for(tmp_path / "work", replica_path="~/work-copy")
    worksync.instance(cfg).generate()
    installed = remote_home / ".local/bin/syncthing"

    with pytest.raises(replica.ReplicaError, match="setup --install"):  # never installs unasked
        replica.setup(cfg)
    assert not (remote_home / "downloads.log").exists()
    with pytest.raises(replica.ReplicaError, match="does not match its published checksum"):
        replica.setup(cfg, install=True)
    assert not installed.exists() and "syncthing_id" not in inventory.load()["devices"]["home"]

    sums.write_text(f"{digest}  {name}.tar.gz\n")
    done = replica.setup(cfg, install=True)
    assert installed.exists() and oct(installed.stat().st_mode & 0o777) == "0o755"
    assert done["folder"] == str(remote_home / "work-copy") and done["private"] is True
    unit = (remote_home / ".config/systemd/user/suw-syncthing.service").read_text()
    assert f"ExecStart={installed} serve" in unit
    log = (remote_home / "downloads.log").read_text()
    assert f"/releases/download/{replica.RELEASE}/{name}.tar.gz" in log
    replica.setup(cfg, install=True)  # already there: no second download
    assert (remote_home / "downloads.log").read_text() == log
    assert replica.status(cfg)["state"] == "ONLINE"
