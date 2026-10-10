"""The public edition's own layers: product identity, platform adapters, capabilities, pairing,
settings, backup/migration, updates, support bundle, first run, and the application window.

Everything runs inside the per-test sandbox (see conftest): no real service, credential store,
network or home directory is touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from suw import platform as osplatform
from suw import product
from suw.core import backup, capabilities, config, events, i18n, inventory, ipc, migrate, onboarding, pairing, paths, settings, state, supervise, support, update

ROOT = Path(__file__).resolve().parents[1]
SYNC_A = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
SYNC_B = "ZZZZZZZ-YYYYYYY-XXXXXXX-WWWWWWW-VVVVVVV-UUUUUUU-TTTTTTT-SSSSSSS"


# ── product identity ────────────────────────────────────────────────────────


def test_channel_comes_from_the_version_and_identifiers_are_generic():
    assert [product.channel(v) for v in ("1.0.0", "1.2.0b1", "1.2.0rc2", "1.3.0.dev4", "1.0.0+local")] == ["stable", "beta", "beta", "nightly", "nightly"]
    assert product.APP_ID.startswith(product.NAMESPACE + ".") and product.SERVICE_DAEMON.startswith(product.NAMESPACE + ".")
    assert product.APP_ID.count(".") >= 2 and product.BUNDLE_ID == product.APP_ID


# ── platform adapters ───────────────────────────────────────────────────────


def test_every_platform_adapter_fulfils_the_contract(monkeypatch, sandbox):
    monkeypatch.setenv("APPDATA", str(sandbox / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(sandbox / "AppData" / "Local"))
    for name in ("linux", "macos", "windows"):
        adapter = osplatform.get(name)
        assert adapter.name == name
        dirs = [adapter.config_dir(sandbox), adapter.state_dir(sandbox), adapter.cache_dir(sandbox), adapter.runtime_dir(sandbox), adapter.default_workspace(sandbox)]
        assert all(sandbox in d.parents for d in dirs) and len(set(dirs)) == 5
        assert adapter.default_workspace(sandbox).name == "Work"
        for permission in adapter.permissions():
            assert permission.what and permission.why and permission.effect and permission.revoke  # every permission is explained
        assert isinstance(adapter.credential_backend(), str)
    windows = osplatform.get("windows")
    assert product.WINDOWS_DIR in str(windows.config_dir(sandbox)) and ".exe" in windows.executable_suffixes()
    from suw.platform.windows import command_line

    assert command_line([r"C:\Program Files\UW\uw.exe", "start"]) == r'"C:\Program Files\UW\uw.exe" start'


def test_linux_autostart_is_a_reversible_desktop_entry(sandbox):
    linux = osplatform.get("linux")
    assert not linux.autostart().enabled
    assert linux.set_autostart(["/opt/uw/unified workstation", "start"], True)
    entry = Path(linux.autostart().detail).read_text(encoding="utf-8")
    assert linux.autostart().enabled and "Exec='/opt/uw/unified workstation' start" in entry and sandbox in Path(linux.autostart().detail).parents
    assert linux.set_autostart([], False) and not linux.autostart().enabled


def test_paths_follow_the_platform_and_resources_ship_inside_the_package(monkeypatch, sandbox):
    assert paths.resources().is_dir() and (paths.resources() / "config" / "defaults.toml").is_file()
    assert PathsInsidePackage(paths.resources())
    monkeypatch.delenv("SUW_CONFIG_DIR")
    monkeypatch.delenv("SUW_STATE_DIR")
    monkeypatch.setenv("APPDATA", str(sandbox / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(sandbox / "Local"))
    monkeypatch.setattr(osplatform, "name", lambda: "windows")
    assert paths.platform() == "windows"
    assert paths.config_dir() == sandbox / "Roaming" / product.WINDOWS_DIR and paths.state_dir().parts[-2:] == (product.WINDOWS_DIR, "State")


def PathsInsidePackage(path: Path) -> bool:
    return (ROOT / "suw") in path.parents


def test_default_device_name_is_this_computers_own_name(monkeypatch):
    import socket

    monkeypatch.delenv("SUW_DEFAULT_DEVICE")
    monkeypatch.setattr(socket, "gethostname", lambda: "Studio_Laptop.local")
    assert config.default_device_name() == "studio-laptop" and inventory.valid_label(config.default_device_name())
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert config.default_device_name() == {"macos": "mac", "windows": "pc"}.get(paths.platform(), "linux")


# ── public defaults ─────────────────────────────────────────────────────────


def test_public_defaults_do_nothing_on_the_users_behalf(monkeypatch):
    monkeypatch.delenv("SUW_PROFILE_FILE")
    cfg = config.load()
    assert config.validate(cfg.data) == ([], [])
    assert cfg.get("autosync.enabled") is False and cfg.get("projects.roots") == [] and cfg.get("projects.layout") == []
    assert cfg.get("privacy.telemetry") is False and cfg.get("workstation.browser.open") == [] and cfg.get("peripherals.server") == ""
    assert cfg.get("work.path") == "~/Desktop/Work" and cfg.get("onboarding.done") is False
    assert cfg.get("urls") == {"github": "https://github.com"}  # no personal destinations ship as defaults


def test_a_profile_is_a_layer_between_defaults_and_the_users_settings(monkeypatch, sandbox):
    monkeypatch.delenv("SUW_PROFILE_FILE")
    config.profiles_dir().mkdir(parents=True)
    (config.profiles_dir() / "studio.toml").write_text('[work]\ntrash_days = 90\n[general]\ntheme = "dark"\n')
    assert config.load().get("work.trash_days") == 30
    config.set_value("profile.active", "studio", "local")
    assert config.load().get("work.trash_days") == 90 and config.load().get("general.theme") == "dark"
    config.set_value("general.theme", "light", "local")  # the user's own choice wins over the profile
    assert config.load().get("general.theme") == "light"
    config.set_value("profile.active", "../../etc/passwd", "local")
    assert config.profile_file() is None


# ── capabilities ────────────────────────────────────────────────────────────


def test_missing_components_are_never_reported_as_working(monkeypatch):
    adapter = osplatform.current()
    monkeypatch.setattr(type(adapter), "which", lambda self, tool: "")
    monkeypatch.setattr(type(adapter), "credential_backend", lambda self: "none")
    found = {f.id: f for f in capabilities.features(config.load())}
    assert found["workspace"].status == osplatform.SUPPORTED  # the folder needs nothing
    for ident in ("file_sync", "peripherals", "shared_clipboard", "servers", "git", "assistant"):
        assert found[ident].status == osplatform.NOT_INSTALLED and found[ident].reason
    assert found["credentials"].status == osplatform.UNAVAILABLE
    assert found["file_sync"].action  # …and says how to get it
    assert all(not c.installed for c in capabilities.components(with_versions=False) if c.id != "terminal")


def test_platform_limits_are_stated_not_hidden(monkeypatch):
    for system, expected in (("macos", osplatform.NEEDS_PERMISSION), ("windows", osplatform.PARTIAL)):
        monkeypatch.setattr(osplatform, "name", lambda system=system: system)
        adapter = osplatform.get(system)
        monkeypatch.setattr(type(adapter), "which", lambda self, tool: f"/bin/{tool}" if tool != "rsync" else "")
        found = {f.id: f for f in capabilities.features(config.load())}
        assert found["peripherals"].status == expected
        assert found["workstation_layout"].status != osplatform.SUPPORTED
    assert found["cloud_projects"].status == osplatform.UNAVAILABLE  # Windows ships no rsync


# ── pairing ─────────────────────────────────────────────────────────────────


def offer(name: str, sync_id: str, **extra) -> pairing.Offer:
    return pairing.Offer(name=name, device_id=f"{name}-1a2b", platform="linux", version="1.0.0", syncthing_id=sync_id, hosts=["192.0.2.10"], **extra)


def test_pairing_code_round_trips_and_rejects_damage():
    mine = offer("workstation-1", SYNC_A, files=12, bytes=3400)
    code = pairing.encode(mine)
    assert code.startswith("UW1-") and pairing.decode(code) == mine
    assert pairing.decode("  " + code.lower().replace("-", " - ") + "\n") == mine  # pasted with noise
    broken = code[:-1] + ("A" if code[-1] != "A" else "B")
    for bad in (broken, "hello", "UW1-000000-AAAA", code[: len(code) // 2], "UW1-" + "A" * 9000):
        with pytest.raises(pairing.PairingError):
            pairing.decode(bad)
    for field, value in (("name", "Bad Name; rm"), ("syncthing_id", "not-an-id"), ("deskflow_fp", "xyz"), ("syncthing_port", 99999)):
        with pytest.raises(pairing.PairingError):
            pairing.validate({**mine.as_dict(), field: value})
    assert pairing.validate({**mine.as_dict(), "hosts": ["-oProxyCommand=evil", "192.0.2.10", "ok.example.com"]}).hosts == ["192.0.2.10", "ok.example.com"]


def test_confirmation_number_is_the_same_on_both_sides_and_changes_with_identity():
    a, b = offer("workstation-1", SYNC_A), offer("workstation-2", SYNC_B)
    number = pairing.confirmation(a, b)
    assert number == pairing.confirmation(b, a) and re.fullmatch(r"\d{6}", number)
    assert pairing.confirmation(a, offer("workstation-2", SYNC_B.replace("Z", "Q"))) != number  # an impostor shows another number


def test_pairing_trusts_only_after_review_and_unpair_keeps_files(sandbox, monkeypatch):
    from suw.integrations import peripherals, worksync

    onboarding.initialise()
    cfg = config.load()
    monkeypatch.setattr(pairing, "make_offer", lambda cfg: offer(cfg.device, SYNC_A, files=5))
    theirs = offer("workstation-2", SYNC_B, files=7, deskflow_fp="ab" * 32)
    review = pairing.review(cfg, theirs)
    assert review["both_have_files"] and "merged" in review["merge_note"] and review["problems"] == []
    assert worksync.peers(cfg) == [] and peripherals.approved() == []  # reviewing changes nothing
    assert pairing.review(cfg, offer(cfg.device, SYNC_B))["problems"]  # same name
    assert pairing.review(cfg, offer("other", SYNC_A))["problems"]  # this computer's own code
    assert pairing.review(cfg, pairing.Offer(name="empty"))["problems"]

    done = pairing.accept(cfg, theirs)
    assert done == {"device": "workstation-2", "sync": True, "peripherals": True}
    assert [p.name for p in worksync.peers(cfg)] == ["workstation-2"] and peripherals.approved() == ["ab" * 32]
    work = worksync.root(cfg)
    work.mkdir(parents=True)
    (work / "keep.txt").write_text("mine")
    pairing.rename(cfg, "workstation-2", "studio")
    assert "studio" in inventory.load()["devices"] and "workstation-2" not in inventory.load()["devices"]
    with pytest.raises(pairing.PairingError):
        pairing.rename(cfg, "studio", "Not Valid")
    assert pairing.unpair(cfg, "studio") and not pairing.unpair(cfg, "studio") and not pairing.unpair(cfg, cfg.device)
    assert worksync.peers(cfg) == [] and peripherals.approved() == [] and (work / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_local_addresses_are_private_only():
    import ipaddress

    for address in pairing.local_addresses():
        assert ipaddress.ip_address(address).is_private and not ipaddress.ip_address(address).is_loopback


# ── settings ────────────────────────────────────────────────────────────────


def test_settings_are_validated_as_a_whole_and_snapshotted(sandbox):
    onboarding.initialise()
    described = settings.describe(config.load())
    assert set(described["sections"]) >= {"general", "workspace", "sync", "privacy", "advanced"}
    assert {f["key"] for f in described["fields"]} <= set(config.SCHEMA)
    clean, errors = settings.check({"work.trash_days": "45", "general.theme": "neon", "privacy.telemetry": True, "no.such": 1, "work.large_file_mb": "many"})
    assert clean["work.trash_days"] == 45 and set(errors) == {"general.theme", "privacy.telemetry", "no.such", "work.large_file_mb"}
    with pytest.raises(settings.SettingsError):
        settings.apply({"work.trash_days": 45, "general.theme": "neon"})
    assert config.load().get("work.trash_days") == 30  # all or nothing
    result = settings.apply({"work.trash_days": 45, "work.ignore": "build\ndist", "peripherals.peer_side": "left"})
    assert result["changed"] == ["peripherals.peer_side", "work.ignore", "work.trash_days"] and result["snapshot"]
    assert config.load().get("work.ignore") == ["build", "dist"]
    assert config.read_toml(config.shared_file())["peripherals"]["peer_side"] == "left"  # shared scope
    assert settings.apply({"work.trash_days": 45})["changed"] == []
    assert settings.reset(["work.trash_days", "privacy.telemetry"])["changed"] == ["work.trash_days"] and config.load().get("work.trash_days") == 30


def test_system_identity_can_not_become_the_work_folder(sandbox):
    (sandbox / ".ssh").mkdir()
    for bad in ("~", str(sandbox.parent), "~/.ssh", "~/.ssh/keys", "~/.config", str(paths.config_dir()), str(paths.state_dir() / "x"), "relative/path"):
        assert settings.workspace_problem(bad), bad
    for good in ("~/Desktop/Work", "~/Projects/shared", str(sandbox / "Documents" / "Work")):
        assert settings.workspace_problem(good) == "", good


# ── backup, export, migration ───────────────────────────────────────────────


def test_snapshot_restore_reset_and_rebuild(sandbox):
    onboarding.initialise()
    settings.apply({"work.trash_days": 60})
    saved = backup.snapshot("manual")
    settings.apply({"work.trash_days": 10})
    assert backup.restore(saved.stem) and config.load().get("work.trash_days") == 60
    assert any(s["reason"] == "before-restore" for s in backup.snapshots())
    with pytest.raises(backup.BackupError):
        backup.restore("../../etc/passwd")
    device_id = config.load().get("device.id")
    backup.reset()
    assert config.load().get("work.trash_days") == 30 and config.load().get("device.id") == device_id  # identity and pairings survive a reset
    config.load()  # refreshes the last-known-good copy
    good = config.local_file().read_text(encoding="utf-8")
    config.local_file().write_text("this is [not toml")
    actions = backup.rebuild()
    assert "last version that worked" in actions[0] and config.local_file().read_text(encoding="utf-8") == good
    assert list(config.local_file().parent.glob("config.toml.broken-*"))  # the damaged file is kept, not deleted


def test_export_never_carries_secrets_and_import_is_validated(sandbox):
    onboarding.initialise()
    settings.apply({"work.trash_days": 77, "general.theme": "dark"})
    config.set_value("secrets.api", "keychain://api-token", "local")
    blob = backup.export(include_machine=False, include_devices=True)
    document = backup.read_export(blob)
    assert document["portable"]["work.trash_days"] == 77 and document["machine"] == {} and document["secret_references"] == ["secrets.api"]
    assert "device.id" not in document["portable"] and "keychain://" not in json.dumps(document["portable"])
    assert set(next(iter(document["devices"].values()))) <= {"logical_name", "role", "platform", "host", "tailscale_name", "ssh_user", "ssh_port", "syncthing_id", "syncthing_port", "deskflow_fp"}
    backup.reset()
    assert backup.import_preview(blob)["changes"]
    result = backup.import_(blob)
    assert result["applied"] and result["secrets_to_reenter"] == ["secrets.api"] and config.load().get("work.trash_days") == 77

    def forged(portable: dict) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("unified-workstation-settings.json", json.dumps({"format": 1, "product": product.SLUG, "portable": portable}))
        return buffer.getvalue()

    for bad in (b"not a zip", forged({"work.trash_days": -5}), forged({"cloud.api_token": "s3cr3t-value"})):
        with pytest.raises(backup.BackupError):
            backup.import_(bad)
    assert config.load().get("work.trash_days") == 77


def test_migration_keeps_an_existing_folder_and_rolls_back_on_failure(sandbox, monkeypatch):
    from suw.core import tomlw

    (sandbox / "Desktop" / "work").mkdir(parents=True)
    tomlw.dump(config.local_file(), {"device": {"name": "old"}})
    assert migrate.pending() == [1]
    result = migrate.run()
    assert result["migrated"] and result["snapshot"] and config.load().get("work.path") == "~/Desktop/work"
    assert migrate.run()["migrated"] is False and migrate.pending() == []  # idempotent

    tomlw.dump(config.local_file(), {"device": {"name": "old"}, "work": {"trash_days": 44}})
    before = config.local_file().read_text(encoding="utf-8")
    monkeypatch.setitem(migrate.MIGRATIONS, 1, lambda local, shared: ({**local, "work": {"trash_days": -1}}, shared))
    failed = migrate.run()
    assert failed["migrated"] is False and "error" in failed and config.local_file().read_text(encoding="utf-8") == before


# ── updates ─────────────────────────────────────────────────────────────────


def manifest(*releases: dict) -> dict:
    return {"releases": list(releases)}


def release(version: str, channel: str, sha: str = "a" * 64, **artifact) -> dict:
    return {"version": version, "channel": channel, "notes": "fixes", "artifacts": [{"os": "linux", "arch": "x86_64", "kind": "appimage", "url": f"https://example.com/uw-{version}.AppImage", "sha256": sha, "size": 10, **artifact}]}


def test_update_selection_respects_channel_version_and_platform():
    pick = lambda m, channel="stable", current="1.0.0", kind="appimage": update.select(m, channel, current, "linux", "x86_64", kind)  # noqa: E731
    assert update.parse("1.2.0.dev1") < update.parse("1.2.0a1") < update.parse("1.2.0b1") < update.parse("1.2.0rc1") < update.parse("1.2.0") < update.parse("1.10.0")
    everything = manifest(release("1.1.0", "stable"), release("1.2.0b1", "beta"), release("1.3.0.dev1", "nightly"), release("0.9.0", "stable"))
    assert pick(everything).version == "1.1.0" and pick(everything, "beta").version == "1.2.0b1" and pick(everything, "nightly").version == "1.3.0.dev1"
    assert pick(everything, current="1.1.0") is None  # never a downgrade
    assert pick(manifest(release("2.0.0b1", "stable"))) is None  # a beta labelled Stable is refused
    assert pick(manifest(release("1.1.0", "stable", url="http://example.com/x"))) is None and pick(manifest(release("1.1.0", "stable", sha="short"))) is None
    assert pick(manifest(release("1.1.0", "stable", os="windows"))) is None
    managed = pick(everything, kind="flatpak")
    assert managed.version == "1.1.0" and managed.managed_by and not managed.url
    with pytest.raises(update.UpdateError):
        update.download(managed)


def test_download_is_used_only_when_the_checksum_matches(monkeypatch):
    payload = b"installer bytes"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/uw-1.1.0.AppImage"
    good = update.Offer("1.1.0", "stable", url=url, sha256=hashlib.sha256(payload).hexdigest(), filename="uw-1.1.0.AppImage")
    try:
        with pytest.raises(update.UpdateError, match="HTTPS"):
            update.download(good)
        monkeypatch.setenv("SUW_UPDATE_ALLOW_INSECURE", "1")
        path = update.download(good)
        assert path.read_bytes() == payload and paths.cache_dir() in path.parents and update.previous_installers()[0]["name"] == "uw-1.1.0.AppImage"
        bad = update.Offer("1.2.0", "stable", url=url, sha256="0" * 64, filename="uw-1.2.0.AppImage")
        with pytest.raises(update.UpdateError, match="checksum"):
            update.download(bad)
        assert not list(update.downloads().glob("uw-1.2.0*"))  # the rejected file is gone
        assert update.check(config.load(), url=url)["error"]  # garbage instead of a manifest: reported, not raised
    finally:
        server.shutdown()


# ── support bundle ──────────────────────────────────────────────────────────


def test_support_bundle_names_nothing_real(sandbox, monkeypatch):
    import getpass

    onboarding.initialise()
    monkeypatch.setattr(getpass, "getuser", lambda: "realperson")
    inv = inventory.load()
    inventory.ensure_device(inv, "home", role="home", host="nas.corp.internal", ssh_user="realperson", syncthing_id=SYNC_B)
    inventory.save(inv)
    events.emit("test", f"connect to realperson@nas.corp.internal (203.0.113.77) failed token=ghp_{'x' * 30} in {sandbox}/Desktop/Work id {SYNC_A}", "error")
    events.emit("test", "fingerprint SHA256:" + "Ab1" * 14 + " mail someone@example.org")
    collected = support.collect()
    text = json.dumps(collected)
    for secret in ("realperson", "nas.corp.internal", "203.0.113.77", "ghp_", str(sandbox), SYNC_A, SYNC_B, "someone@example.org", "Ab1Ab1"):
        assert secret not in text, secret
    assert "<host-" in text and "<user-" in text and "<sync-id>" in text and "<fingerprint>" in text
    assert collected["devices.json"]["device-1"]["has_sync_identity"] is True
    names = zipfile.ZipFile(io.BytesIO(support.build())).namelist()
    assert "README.txt" in names and not any("Work" in n for n in names)


# ── background plumbing ─────────────────────────────────────────────────────


@pytest.mark.parametrize("unix", [True, False])
def test_control_channel_answers_only_its_owner(monkeypatch, unix):
    if unix and not ipc.UNIX:
        pytest.skip("Unix sockets are not used on this platform")
    monkeypatch.setattr(ipc, "UNIX", unix)
    import tempfile

    monkeypatch.setenv("SUW_RUNTIME_DIR", tempfile.mkdtemp(prefix="uw"))  # a Unix socket path has a length limit

    async def scenario():
        async def handler(reader, writer, authorised):
            line = await reader.readline()
            reply = {"ok": authorised, "echo": json.loads(line or b"{}").get("cmd")} if authorised else {"ok": False}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()

        server, cleanup = await ipc.start_server(handler)
        try:
            assert await asyncio.to_thread(ipc.request, {"cmd": "ping"}) == {"ok": True, "echo": "ping"}
            if not unix:
                spec = json.loads(ipc.endpoint_file().read_text(encoding="utf-8"))
                assert server.sockets[0].getsockname()[0] == "127.0.0.1"
                ipc.endpoint_file().write_text(json.dumps({**spec, "token": "wrong"}))
                assert (await asyncio.to_thread(ipc.request, {"cmd": "ping"})) == {"ok": False}
            else:
                assert oct(paths.socket_path().stat().st_mode & 0o777) == "0o600"
        finally:
            server.close()
            cleanup()
        assert ipc.request({"cmd": "ping"}) is None and not ipc.available()

    asyncio.run(scenario())


def test_supervisor_starts_once_stops_and_restores():
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    assert supervise.start("demo", command) and supervise.running("demo")
    first = supervise.pid("demo")
    assert supervise.start("demo", command) and supervise.pid("demo") == first  # idempotent
    assert supervise.stop("demo", forget=False) and not supervise.running("demo")
    assert supervise.restore() == ["demo"] and supervise.running("demo")
    assert supervise.stop("demo") and state.load("supervised", {}) == {} and supervise.restore() == []
    assert not supervise.alive(0) and not supervise.start("ghost", ["/nonexistent/binary"], remember=False)


# ── first run ───────────────────────────────────────────────────────────────


def test_personal_setup_touches_nothing_outside_the_products_own_folders(sandbox, monkeypatch):
    monkeypatch.delenv("SUW_PROFILE_FILE")
    (sandbox / ".bashrc").write_text("# mine\n")
    before = {p.relative_to(sandbox) for p in sandbox.rglob("*")}
    assert onboarding.state()["done"] is False
    assert [s.id for s in onboarding.plan({"kind": "personal"})] == ["settings", "workspace"]
    result = onboarding.apply({"kind": "personal"})
    assert result["ok"] and result["kind"] == "personal"
    created = {p.relative_to(sandbox) for p in sandbox.rglob("*")} - before
    allowed = (Path(".config/suw"), Path(".local"), Path("Desktop"), Path(".cache"))
    assert all(any(path == root or root in path.parents or path in root.parents for root in allowed) for path in created), created
    assert (sandbox / ".bashrc").read_text(encoding="utf-8") == "# mine\n" and not (sandbox / ".ssh").exists()
    cfg = config.load()
    assert cfg.get("onboarding.done") and cfg.get("work.enabled") is False and cfg.get("peripherals.enabled") is False
    assert (sandbox / "Desktop" / "Work").is_dir() and cfg.get("device.id") and cfg.device in inventory.load()["devices"]
    assert not (paths.shared_dir() / ".git").exists()  # no repository, no remote, no account


def test_setup_refuses_an_unsafe_folder_and_never_changes_an_existing_one(sandbox):
    existing = sandbox / "Documents" / "Work"
    existing.mkdir(parents=True)
    (existing / "thesis.md").write_text("draft")
    assert onboarding.apply({"kind": "personal", "workspace": "~"})["ok"] is False and not config.load().get("onboarding.done")
    assert onboarding.apply({"kind": "personal", "workspace": str(existing)})["ok"]
    assert (existing / "thesis.md").read_text(encoding="utf-8") == "draft" and sorted(p.name for p in existing.iterdir()) == ["thesis.md"]


def test_workstation_plan_lists_every_change_and_marks_what_is_unavailable(monkeypatch):
    adapter = osplatform.current()
    monkeypatch.setattr(type(adapter), "which", lambda self, tool: "" if tool in ("syncthing", "deskflow", "deskflow-core") else f"/usr/bin/{tool}")
    steps = {s.id: s for s in onboarding.plan({"kind": "workstation", "sync": True, "peripherals": True, "integrations": ["ssh"]})}
    # "autostart" is added only where the session has no service manager (Windows, some Linux sessions)
    assert [s for s in steps if s != "autostart"] == ["settings", "workspace", "sync", "peripherals", "service", "integration.ssh"]
    assert not steps["sync"].available and "Syncthing" in steps["sync"].reason and not steps["peripherals"].available
    assert all(step.touches for step in steps.values())


# ── assistants ──────────────────────────────────────────────────────────────


def test_only_project_local_assistant_state_is_called_portable(sandbox):
    from suw.integrations import assistants

    project = sandbox / "Desktop" / "Work" / "app"
    (project / ".claude").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("rules")
    (project / ".claude" / "settings.local.json").write_text("{}")
    (sandbox / ".claude").mkdir()
    (sandbox / ".claude" / ".credentials.json").write_text("{}")
    claude = assistants.get("claude")
    items = {i.path: i for i in claude.project_state(project)}
    assert items["CLAUDE.md"].portable and items["CLAUDE.md"].present and items["CLAUDE.md"].kind == assistants.PROJECT_LOCAL
    assert items[".claude/settings.local.json"].kind == assistants.MACHINE_SPECIFIC and not items[".claude/settings.local.json"].portable
    assert all(not i.portable for i in claude.global_state())
    kinds = {i.path: i.kind for i in claude.global_state()}
    assert kinds["~/.claude/.credentials.json"] == assistants.MACHINE_SPECIFIC and kinds["~/.claude/projects"] == assistants.SESSION


# ── translations ────────────────────────────────────────────────────────────


def test_catalogs_are_complete_and_consistent():
    en, ru = i18n.catalog("en"), i18n.catalog("ru")
    assert set(en) == set(ru) and len(en) > 600
    placeholder = re.compile(r"\{(\w+)\}")
    for key in en:
        assert set(placeholder.findall(en[key])) == set(placeholder.findall(ru[key])), key
        assert en[key].strip() and ru[key].strip(), key
    script = (ROOT / "suw" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    used = {k for k in re.findall(r'\bt\("([a-zA-Z0-9_.-]+)"', script) if not k.endswith((".", "_"))}
    assert used <= set(en), sorted(used - set(en))
    for field in settings.FIELDS:
        assert f"settings.{field.key}.label" in en, field.key
    for section in settings.SECTIONS:
        assert f"settings.section.{section}" in en
    backend = (ROOT / "suw" / "app" / "backend.py").read_text(encoding="utf-8")
    for code in set(re.findall(r'Problem\(\s*"([a-z_]+)"', backend)):
        assert f"problem.{code}.what" in en, code
    assert i18n.t("pair.files", "ru", n=3, size="1 KB") == "файлов: 3, 1 KB" and i18n.t("no.such.key") == "no.such.key"
    assert i18n.resolve("ru") == "ru" and i18n.resolve("auto") in i18n.LANGUAGES


# ── the application window ──────────────────────────────────────────────────


@pytest.fixture
def window():
    from suw.app.demo import DemoBackend
    from suw.app.server import App

    app = App(DemoBackend())
    app.start()
    yield app
    app.stop()


def http(app, path: str = "/api", *, method: str | None = None, params: dict | None = None, headers: dict | None = None, token: bool = True):
    data = json.dumps({"method": method, "params": params or {}}).encode() if method else None
    all_headers = {"Content-Type": "application/json", **({"X-Session-Token": app.token} if token and method else {}), **(headers or {})}
    request = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            return reply.status, reply.headers, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_window_is_local_token_guarded_and_loads_nothing_remote(window):
    assert window.httpd.server_address[0] == "127.0.0.1"
    assert http(window, "/")[0] == 403 and http(window, "/?k=wrong")[0] == 403
    status, headers, body = http(window, f"/?k={window.token}")
    assert status == 200 and window.token.encode() in body and b"__SESSION_TOKEN__" not in body
    assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    assert http(window, method="dashboard", token=False)[0] == 403
    assert http(window, method="dashboard", headers={"Host": "evil.example"})[0] == 403  # DNS rebinding
    assert http(window, method="dashboard", headers={"Origin": "https://evil.example"})[0] == 403  # cross-site request
    assert http(window, "/static/../backend.py")[0] == 404 and http(window, "/static/app.js")[0] == 200
    for name in ("index.html", "app.js", "app.css"):
        text = (ROOT / "suw" / "app" / "static" / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!127\.0\.0\.1)", text), name  # no CDN, no fonts, no trackers
        assert "innerHTML" not in text and "eval(" not in text
    status, _, body = http(window, method="_offer_download", params={"name": "x", "blob": ""})
    assert json.loads(body)["problem"]["code"] == "unknown_request"  # private methods are not callable


def test_the_window_speaks_the_chosen_language(window, sandbox):
    """Sentences produced by program logic reach the window in the user's language, not only the
    labels the page itself looks up. The event log stays a technical record."""
    def call(method: str, **params):
        return json.loads(http(window, method=method, params=params)[2])

    prose, cyrillic = re.compile(r"[A-Za-z]{3,}[,.]? [a-z]{2,}"), re.compile(r"[А-Яа-яЁё]")
    technical = {"catalog", "rows", "path", "paths", "rules", "code", "my_code", "links", "fingerprint"}

    def english(node, where: str = "") -> list[str]:
        if isinstance(node, dict):
            return [hit for key, value in node.items() if key not in technical for hit in english(value, f"{where}.{key}")]
        if isinstance(node, list):
            return [hit for value in node for hit in english(value, where)]
        return [f"{where}: {node}"] if isinstance(node, str) and prose.search(node) and not cyrillic.search(node) else []

    choices = {"kind": "workstation", "workspace": "~/Desktop/Work", "sync": True, "peripherals": True, "integrations": ["ssh", "desktop"]}
    pages = [("dashboard", {}), ("capabilities", {}), ("settings_get", {}), ("workstations", {}), ("workspace", {}), ("sync_status", {}),
             ("peripherals_status", {}), ("servers", {}), ("assistants", {}), ("recovery", {}), ("uninstall_preview", {}), ("health", {}),
             ("onboarding_plan", {"choices": choices})]
    assert call("set_language", language="ru")["result"]["language"] == "ru"
    call("onboarding_apply", choices=choices)
    for method, params in pages:
        assert english(call(method, **params)["result"], method) == [], method
    russian = {f["id"]: f["reason"] for f in call("capabilities")["result"]["features"]}
    assert russian["workspace"] == i18n.t("cap.workspace.ok", "ru") and "Secret Service" in russian["credentials"]
    assert {row["id"]: row["detail"] for row in call("selftest")["result"]["rows"]}["cloud"] == i18n.t("selftest.optional", "ru")
    assert call("pair_review", code="nonsense")["problem"]["detail"] == i18n.t("pairing.error.not_a_code", "ru")
    page = http(window, f"/?k={window.token}")[2].decode()
    assert '<html lang="ru">' in page and i18n.t("boot.noscript", "ru") in page and "__T:" not in page and "__LANG__" not in page

    call("set_language", language="en")
    english_reason = {f["id"]: f["reason"] for f in call("capabilities")["result"]["features"]}
    assert english_reason["workspace"] == i18n.t("cap.workspace.ok") and english_reason != russian
    assert '<html lang="en">' in http(window, f"/?k={window.token}")[2].decode()


def test_catalog_sentences_read_as_english_and_stay_translatable():
    import copy

    from suw.core import capabilities, onboarding
    from suw.platform import current

    said = i18n.msg("cap.credentials.ok", store=i18n.msg("credstore.secret_service"))
    assert said == "Passwords are kept in Secret Service (system keyring), never in configuration files." and isinstance(said, str)
    assert i18n.render(said, "ru") == i18n.t("cap.credentials.ok", "ru", store=i18n.t("credstore.secret_service", "ru"))
    assert i18n.render("plain text", "ru") == "plain text"
    kept = copy.deepcopy({"rows": [said, 3, None]})   # dataclasses.asdict copies the same way
    assert i18n.localize(kept, "ru") == {"rows": [i18n.render(said, "ru"), 3, None]} and i18n.localize(kept, "en")["rows"][0] == said
    # nothing a person reads about this computer's abilities is written in program logic
    for feature in capabilities.features():
        assert isinstance(feature.reason, i18n.Msg), feature.id
    for part in capabilities.components(with_versions=False):
        assert isinstance(part.purpose, i18n.Msg) and all(isinstance(hint, i18n.Msg) for hint in part.install.values()), part.id
    from suw.core import doctor

    source = (ROOT / "suw" / "core" / "doctor.py").read_text(encoding="utf-8")
    keys = {key for key in re.findall(r'i18n\.msg\("(doctor\.[a-z0-9_.]+)"', source) if not key.endswith(".")}
    assert len(keys) > 150 and keys <= set(i18n.catalog("en")), sorted(keys - set(i18n.catalog("en")))
    assert all(isinstance(check.section, i18n.Msg) for check in doctor.run_all(config.load()))
    for permission in current().permissions():
        assert all(isinstance(getattr(permission, name), i18n.Msg) for name in ("title", "what", "why", "effect", "revoke")), permission.id
    plan = onboarding.plan({"kind": "workstation", "sync": True, "peripherals": True, "integrations": ["ssh", "desktop", "terminal", "shell", "tmux", "git"]})
    assert all(isinstance(touch, i18n.Msg) for step in plan if step.id not in ("settings", "workspace") for touch in step.touches)
    catalog = i18n.catalog("en")
    for system in ("linux", "macos", "windows"):
        source = (ROOT / "suw" / "platform" / f"{system}.py").read_text(encoding="utf-8")
        for ident in re.findall(rf'Permission\.of\("{system}", "([a-z-]+)"', source):
            assert f"permission.{system}.{ident}.title" in catalog, (system, ident)


def test_output_survives_a_legacy_console_code_page_and_a_packaged_build_never_waits_on_an_error_box(monkeypatch, sandbox, capsys):
    import io

    from suw.app import entry
    from suw.cli import main as cli

    raw = io.BytesIO()
    legacy = io.TextIOWrapper(raw, encoding="cp1252")   # what redirected output is on Windows
    monkeypatch.setattr(sys, "stdout", legacy)
    cli.utf8_output()
    print("→ «Привет»")
    legacy.flush()
    assert raw.getvalue().decode("utf-8").strip() == "→ «Привет»"
    monkeypatch.setattr(sys, "stdout", None)            # a windowed build has no streams at all
    cli.utf8_output()
    monkeypatch.undo()

    def broken(argv=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "main", broken)
    monkeypatch.setattr(sys, "argv", ["unified-workstation", "capabilities", "--json"])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert entry.main() == 1 and "RuntimeError: boom" in capsys.readouterr().err   # reported, not raised into a dialog
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError):
        entry.main()                                     # from source a developer still gets the traceback


def test_every_window_request_works_in_demo_mode_and_changes_nothing(window, sandbox):
    before = sorted(p for p in sandbox.rglob("*"))

    def call(method: str, **params):
        status, _, body = http(window, method=method, params=params)
        assert status == 200, method
        return json.loads(body)

    for method in ("bootstrap", "dashboard", "capabilities", "selftest", "health", "settings_get", "workstations", "pair_offer", "workspace", "sync_status", "peripherals_status", "servers", "cloud_projects", "assistants", "git_projects", "recovery", "update_status", "history", "uninstall_preview", "browse", "help_info"):
        assert "result" in call(method), method
    assert call("bootstrap")["result"]["demo"] is True and call("bootstrap")["result"]["onboarding"]["done"] is False
    assert call("onboarding_apply", choices={"kind": "workstation"})["result"]["ok"] and call("dashboard")["result"]["kind"] == "workstation"
    assert call("settings_apply", changes={"work.trash_days": 99999})["result"]["errors"]
    assert call("settings_apply", changes={"general.theme": "dark"})["result"]["changed"] == ["general.theme"]
    assert call("pair_accept", code="UW1-X", confirmation="000000")["problem"]["code"] == "pairing_number_mismatch"
    assert call("pair_accept", code="UW1-X", confirmation="418207", merge_confirmed=True)["result"]["device"] == "workstation-3"
    assert call("home_save", host="h.example.net", user="demo")["problem"]["code"] == "fingerprint_unconfirmed"  # no trust without review
    assert call("cloud_push", name="demo-project")["problem"]["code"] == "transfer_unconfirmed"
    assert call("sync_action", action="pause")["result"]["state"] == "PAUSED"
    token = call("support_bundle")["result"]["download"]
    assert http(window, f"/download?id={token}")[0] == 403 and http(window, f"/download?id={token}&k={window.token}")[0] == 200
    assert http(window, f"/download?id={token}&k={window.token}")[0] == 404  # one use
    assert call("no_such_method")["problem"]["code"] == "unknown_request" and call("dashboard", bogus=1)["problem"]["code"] == "unknown_request"
    demo_text = (ROOT / "suw" / "app" / "demo.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(?!192\.0\.2\.|127\.0\.0\.1)(?:\d{1,3}\.){3}\d{1,3}\b", demo_text)  # documentation addresses only
    assert sorted(p for p in sandbox.rglob("*")) == before


def test_real_backend_on_a_fresh_computer(sandbox, monkeypatch):
    from suw.app.backend import Backend, Problem

    monkeypatch.delenv("SUW_PROFILE_FILE")
    backend = Backend()
    boot = backend.call("bootstrap", {})
    assert boot["onboarding"]["done"] is False and boot["demo"] is False and boot["catalog"]["page.dashboard"]
    assert backend.call("onboarding_plan", {"choices": {"kind": "personal", "workspace": "~"}})["workspace_problem"]
    assert backend.call("onboarding_apply", {"choices": {"kind": "personal"}})["ok"]
    dash = backend.call("dashboard", {})
    assert dash["ready"] and dash["mode"] == "default" and dash["sync"]["state"] == "NOT_CONFIGURED" and dash["workspace"]["state"] == "READY"
    assert [s["status"] for s in dash["servers"]] == ["NOT_CONFIGURED", "NOT_CONFIGURED"]
    test = backend.call("selftest", {})
    assert test["verdict"] in ("pass", "warn") and {r["id"] for r in test["rows"]} >= {"application", "workspace", "git", "ssh"}
    assert backend.call("settings_apply", {"changes": {"work.path": "~/.ssh"}})["errors"]["work.path"]
    (sandbox / "Desktop" / "Work").rmdir()
    assert [a["code"] for a in backend.call("dashboard", {})["attention"]] == ["workspace_missing"]
    assert backend.call("repair", {"what": "create_workspace"})["done"] and backend.call("dashboard", {})["ready"]
    for method, params, code in (
        ("pair_offer", {}, "pairing_not_ready"),
        ("pair_review", {"code": "garbage"}, "pairing_code_invalid"),
        ("server_scan", {"host": "-oProxyCommand=x"}, "host_invalid"),
        ("home_save", {"host": "nas.example.net", "user": "me"}, "fingerprint_unconfirmed"),
        ("home_save", {"host": "nas.example.net", "user": "bad user;"}, "user_invalid"),
        ("home_remove", {}, "server_not_configured"),
        ("make_folder", {"parent": "~", "name": "../x"}, "bad_folder_name"),
        ("snapshot_restore", {"snapshot": "nope"}, "snapshot_not_restored"),
        ("config_import", {"data": "bm90IGEgemlw"}, "import_rejected"),
        ("update_download", {}, "no_update"),
        ("bootstrap_", {}, "unknown_request"),
    ):
        with pytest.raises(Problem) as caught:
            backend.call(method, params)
        assert caught.value.code == code, method
        assert set(caught.value.as_dict()) == {"code", "actions", "detail", "params"}
    listing = backend.call("browse", {"path": ""})
    assert listing["path"] == str(sandbox.resolve()) and "Desktop" in listing["folders"] and listing["problem"]
    assert not any(name.startswith(".") for name in listing["folders"])


def test_window_profile_avoids_hidden_folders_for_a_confined_browser(sandbox, monkeypatch):
    from suw.app import launch

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: sandbox))
    confined = launch._profile_dir("/snap/bin/chromium")
    assert confined == sandbox / "snap" / "chromium" / "common" / product.SLUG and confined.is_dir()
    assert not any(part.startswith(".") for part in confined.relative_to(sandbox).parts)
    assert launch._profile_dir("/usr/bin/google-chrome") == paths.cache_dir() / "window"
    assert launch._open_and_stay(["/nonexistent/browser"]) is False
    assert launch._open_and_stay([sys.executable, "-c", "raise SystemExit(3)"]) is False  # a browser that gives up at once
    assert launch._open_and_stay([sys.executable, "-c", "pass"]) is True
