"""Keyboard/mouse sharing: the Deskflow 1.27 profile SUW writes, trust, and honest status.

The format facts asserted here were established against a real Deskflow 1.27 core (see
docs/peripherals.md): computers live in the settings file, the layout file holds only
`links` and `options`, trusted peers are `v2:sha256:<hex>` lines.
"""

from __future__ import annotations

import shutil
import ssl
import subprocess

import pytest

from suw.core import config, inventory
from suw.integrations import peripherals


def test_layout_is_links_and_options_only():
    text = peripherals.layout("ubuntu", ["mac"], "right")
    assert "section: screens" not in text and "keystroke" not in text
    assert "\tworkstation-ubuntu:\n\t\tright = workstation-mac" in text
    assert "\tworkstation-mac:\n\t\tleft = workstation-ubuntu" in text
    assert "clipboardSharing = true" in text
    assert "clipboardSharing = false" in peripherals.layout("ubuntu", ["mac"], "left", clipboard=False)
    alone = peripherals.layout("ubuntu", [], "right")
    assert "workstation-mac" not in alone and alone.count("end") == 2


def test_no_ip_address_in_names_or_layout():
    import re

    text = peripherals.layout("ubuntu", ["mac"], "right") + str(peripherals.settings("mac", "client", ["ubuntu", "mac"], 24800, "", "ubuntu.tail1234.ts.net", True))
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


def test_merge_ini_keeps_what_deskflow_stored():
    theirs = "[server]\nxdpRestoreToken=abc-123\nenableClipboard=false\n\n[gui]\nwindowGeometry=@ByteArray(\\x1\\xd9)\n"
    wanted = peripherals.settings("ubuntu", "server", ["ubuntu", "mac"], 24800, "100.64.0.1", "", True)
    merged = peripherals.merge_ini(theirs, wanted)
    assert "xdpRestoreToken=abc-123" in merged and "windowGeometry=@ByteArray(\\x1\\xd9)" in merged
    assert "enableClipboard=true" in merged and "enableClipboard=false" not in merged
    assert "[computer_workstation-mac]\nname=workstation-mac" in merged
    assert "[core]" in merged and "interface=100.64.0.1" in merged
    assert peripherals.merge_ini(merged, wanted) == merged  # idempotent


def test_merge_ini_is_stable_after_deskflow_reorders_the_file():
    wanted = peripherals.settings("mac", "client", ["ubuntu", "mac"], 24800, "", "ubuntu.example.ts.net", True)
    first = peripherals.merge_ini("", wanted)
    assert "[client]\nremoteHost=ubuntu.example.ts.net" in first
    assert "interface=\n" in first  # a client never listens
    blocks = sorted(first.strip().split("\n\n"))  # Qt writes sections sorted by name
    reordered = "\n\n".join(blocks) + "\n"
    assert peripherals.merge_ini(reordered, wanted) == reordered


def _pem(tmp_path, name="a"):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    key, crt = tmp_path / f"{name}.key", tmp_path / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=t", "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    return key.read_text() + crt.read_text(), crt


def test_fingerprint_matches_openssl(tmp_path):
    pem, crt = _pem(tmp_path)
    seen = subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"], capture_output=True, text=True).stdout
    expect = seen.split("=", 1)[1].strip().replace(":", "").lower()
    assert peripherals.fingerprint_of(pem) == expect
    assert peripherals.pretty(expect).replace(":", "").lower() == expect
    assert peripherals.fingerprint_of("not a certificate") == ""
    assert ssl.PEM_cert_to_DER_cert  # stdlib only


def test_trust_lines_format_and_rejects_garbage():
    good = "ab" * 32
    assert peripherals.trust_lines([good, good, "zz", "../../etc/passwd", good.upper()]) == f"v2:sha256:{good}\n"
    assert peripherals.trust_lines([]) == ""


def _two_workstations(fp=""):
    inv = inventory.load()
    inventory.ensure_device(inv, "ubuntu", role="workstation", tailscale_name="ubuntu.example.ts.net")
    inventory.ensure_device(inv, "mac", role="workstation", tailscale_name="mac.example.ts.net", deskflow_fp=fp)
    inventory.save(inv)


def test_published_fingerprint_is_not_trusted_until_approved_here(tmp_path, monkeypatch):
    fp = "cd" * 32
    _two_workstations(fp)
    cfg = config.load()
    monkeypatch.setattr(peripherals, "make_certificate", lambda: False)
    peripherals.prepare(cfg, interface="")
    trusted = peripherals.profile_dir() / "tls" / "trusted-clients"
    assert trusted.read_text() == ""
    assert peripherals.unpaired(cfg) == [("mac", fp)]
    peripherals.approve(fp)
    assert peripherals.prepare(cfg, interface="") is True
    assert trusted.read_text() == f"v2:sha256:{fp}\n"
    assert peripherals.unpaired(cfg) == []
    assert peripherals.prepare(cfg, interface="") is False  # idempotent
    peripherals.revoke(fp)
    peripherals.prepare(cfg, interface="")
    assert trusted.read_text() == ""


def test_profile_is_private_and_holds_no_ip(tmp_path):
    pem, _ = _pem(tmp_path)
    _two_workstations()
    cfg = config.load()
    peripherals.paths.ensure(peripherals.certificate().parent)
    peripherals.certificate().write_text(pem)
    peripherals.prepare(cfg, interface="")
    conf = peripherals.settings_file()
    assert conf.stat().st_mode & 0o077 == 0
    text = conf.read_text()
    assert "computerName=workstation-" in text and "tlsEnabled=true" in text and "checkPeerFingerprints=true" in text
    assert "BEGIN" not in text
    if peripherals.role(cfg) == "server":
        assert "section: links" in peripherals.layout_file().read_text()


def test_certificate_is_created_once_and_private(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    assert peripherals.make_certificate() is True
    first = peripherals.fingerprint()
    assert len(first) == 64
    assert peripherals.certificate().stat().st_mode & 0o077 == 0
    assert peripherals.make_certificate() is False and peripherals.fingerprint() == first
    assert "PRIVATE KEY" in peripherals.certificate().read_text()


def test_bind_defaults_to_the_private_network(monkeypatch):
    cfg = config.load()
    monkeypatch.setattr(peripherals.tailscale, "status", lambda: {"running": True, "self": {"ip": "100.64.0.9"}})
    assert peripherals.bind_address(cfg) == "100.64.0.9"
    monkeypatch.setattr(peripherals.tailscale, "status", lambda: {"running": False, "installed": True})
    assert peripherals.bind_address(cfg) is None  # not ready: the service retries later, never exposes the port
    config.set_value("peripherals.bind", "all", "local")
    assert peripherals.bind_address(config.load()) == ""


def test_status_words_cover_every_state(monkeypatch):
    cfg = config.load()
    monkeypatch.setattr(peripherals, "core", lambda: [])
    assert peripherals.status(cfg)["state"] == "NOT_INSTALLED"
    monkeypatch.setattr(peripherals, "core", lambda: ["deskflow-core"])
    assert peripherals.status(cfg)["state"] == "NOT_CONFIGURED"
    monkeypatch.setattr(peripherals, "fingerprint", lambda: "ab" * 32)
    peripherals.paths.ensure(peripherals.profile_dir())
    peripherals.settings_file().write_text("[core]\n")
    monkeypatch.setattr(peripherals, "running", lambda: False)
    assert peripherals.status(cfg)["state"] == "STOPPED"
    monkeypatch.setattr(peripherals, "running", lambda: True)
    monkeypatch.setattr(peripherals, "_sockets", lambda port: (True, 0))
    assert peripherals.status(cfg)["state"] == "UNPAIRED"
    monkeypatch.setattr(peripherals, "_sockets", lambda port: (True, 1))
    assert peripherals.status(cfg)["state"] == "CONNECTED"
    config.set_value("peripherals.enabled", False, "local")
    assert peripherals.status(config.load())["state"] == "DISABLED"
    assert set(peripherals.WORDS) == {"DISABLED", "NOT_INSTALLED", "NOT_CONFIGURED", "STOPPED", "UNPAIRED", "WAITING", "CONNECTED"}


def test_run_refuses_to_start_without_the_private_network(monkeypatch, capsys):
    from suw.cli import main

    monkeypatch.setattr(peripherals, "core", lambda: ["deskflow-core"])
    monkeypatch.setattr(peripherals.tailscale, "status", lambda: {"running": False})
    called = []
    monkeypatch.setattr(main.os, "execvp", lambda *a: called.append(a))
    cfg = config.load()
    if peripherals.role(cfg) != "server":
        config.set_value("peripherals.server", cfg.device, "local")
    assert main.main(["peripherals", "run"]) == peripherals.TEMPFAIL
    assert not called and "waiting" in capsys.readouterr().err
