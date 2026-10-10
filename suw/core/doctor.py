"""`suw doctor` — diagnoses, not package lists. Every finding carries a remediation.

Levels:  PASS ✓   WARN ⚠   FAIL ✗   NONE ○ (not configured — optional, never a problem)
         NA – (not testable right now: the other machine is away; nothing is claimed about it).
Verdict: READY / READY WITH WARNINGS / NOT READY. An absent optional component (no cloud
rented, MacBook not enrolled yet) never makes the system "not ready".
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
from dataclasses import asdict, dataclass

from . import gitsync, health, i18n, inventory, journal, modes, paths, policy, projects, secrets, state, syncer
from .config import Config
from .proc import have, run

PASS, WARN, FAIL, NONE, NA = "PASS", "WARN", "FAIL", "NONE", "NA"
OK, INFO = PASS, NONE  # names used by 0.1 callers
MARK = {PASS: "✓", WARN: "⚠", FAIL: "✗", NONE: "○", NA: "–"}
WORD = {PASS: "PASS", WARN: "WARNING", FAIL: "FAIL", NONE: "NOT CONFIGURED", NA: "NOT TESTABLE"}
SECTIONS = ["Workstation", "Sync", "Work folder", "Peripherals", "Network", "Clipboard", "Security", "Startup", "Recovery"]


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""
    fix: str = ""
    section: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 22)
        return True
    except OSError:
        return False


# ── Workstation ─────────────────────────────────────────────────────────────


def check_workstation(cfg: Config) -> list[Check]:
    from ..integrations import apps, browser, gnome

    report = health.probe_local()
    out = [Check(PASS, f"{report.get('os', sys.platform)}", f"device '{cfg.device}'" + (f" · id {cfg.get('device.id')}" if cfg.get("device.id") else ""))]
    if sys.version_info < (3, 11):
        out.append(Check(FAIL, i18n.msg("doctor.python_3_11_required.d181"), sys.version.split()[0], i18n.msg("doctor.re_run_the_installer.d22b")))
    for warning in cfg.warnings:
        out.append(Check(WARN, i18n.msg("doctor.a_config_file_has_errors_last.9bb2"), warning, "suw config validate"))
    disk = report.get("disk_used_pct")
    if isinstance(disk, (int, float)) and disk >= 92:
        out.append(Check(WARN, f"Disk {int(disk)}% full", "", i18n.msg("doctor.free_space_sync_and_checkpoints_need.f984")))

    linux = paths.platform() == "linux"
    if linux:
        session = os.environ.get("XDG_SESSION_TYPE", "") or "unknown"
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or "unknown"
        out.append(Check(PASS if gnome.available() else NONE, i18n.msg("doctor.desktop_on.0277", p1=desktop, p2=session), "" if gnome.available() else i18n.msg("doctor.mode_switching_changes_apps_only_not.9e89")))
    out.append(Check(PASS, i18n.msg("doctor.mode_integration.a5b2"), f"mode {modes.current()}"))

    if linux and gnome.available():
        shortcut = str(cfg.get("workstation.shortcut", "<Super>d"))
        used = [c for c in gnome.find_conflicts(shortcut) if c[0] != gnome.CUSTOM or "suw-toggle" not in c[1]]
        registered = "suw-toggle" in (run(["gsettings", "get", gnome.MEDIA_KEYS, "custom-keybindings"], timeout=5).out)
        if registered and not used:
            out.append(Check(PASS, i18n.msg("doctor.keyboard_shortcut.eaf4"), shortcut))
        elif registered:
            out.append(Check(WARN, i18n.msg("doctor.mode_shortcut_conflicts_with_another_binding.5ec7"), str(used[0]), "suw config set workstation.shortcut '<Super><Alt>d' && suw bootstrap"))
        else:
            out.append(Check(WARN, i18n.msg("doctor.mode_shortcut_not_registered.6fd4"), "", "suw bootstrap"))
        ext = gnome.extension_state()
        if ext == "active":
            out.append(Check(PASS, "Workspaces", i18n.msg("doctor.window_placement_helper_active.83dc")))
        elif ext == "stale":
            out.append(Check(WARN, i18n.msg("doctor.workspaces_an_updated_placement_helper_loads.c0c0"), i18n.msg("doctor.until_then_leaving_the_mode_closes.3b21"), i18n.msg("doctor.log_out_and_back_in_once.df82")))
        elif ext == "installed":
            out.append(Check(WARN, i18n.msg("doctor.workspaces_placement_helper_loads_at_next.a255"), i18n.msg("doctor.until_then_apps_open_on_the.120e"), i18n.msg("doctor.log_out_and_back_in_once.df82")))
        else:
            out.append(Check(NONE, i18n.msg("doctor.workspaces_placement_helper_not_installed.12b2"), "optional", "suw bootstrap"))

    kind, exe = browser.detect(cfg)
    urls, missing = browser.session_urls(cfg)
    if missing:
        out.append(Check(WARN, i18n.msg("doctor.browser_session_has_links_without_an.4307"), ", ".join(missing), i18n.msg("doctor.suw_config_set_urls_https.9328", p1=missing[0])))
    elif urls:
        out.append(Check(PASS, "Browser", f"{exe or 'system default'}: {len(urls)} tab(s), profile {cfg.get('workstation.browser.profile', 'existing')}"))
    else:
        out.append(Check(NONE, i18n.msg("doctor.browser_no_workstation_urls_configured.4a34"), "", "suw config edit --shared"))
    editor = apps.editor(cfg)
    out.append(Check(PASS, "Editor", editor) if editor else Check(WARN, i18n.msg("doctor.no_editor_found.0dc8"), "", i18n.msg("doctor.install_vs_code_or_cursor_or.df26")))
    term = apps.terminal(cfg)
    out.append(Check(PASS, "Terminal", term) if term else Check(WARN, i18n.msg("doctor.no_terminal_emulator_found.8f93"), "", "install WezTerm"))
    out.append(Check(PASS, "tmux", "") if have("tmux") else Check(WARN, i18n.msg("doctor.tmux_not_installed.66dd"), i18n.msg("doctor.persistent_server_sessions_need_it.5137"), i18n.msg("doctor.install_tmux.1623")))

    shell = os.path.basename(os.environ.get("SHELL", "zsh"))
    rc = paths.home() / (".zshrc" if shell == "zsh" else ".bashrc")
    if rc.exists() and "suw managed" in rc.read_text(errors="replace"):
        out.append(Check(PASS, i18n.msg("doctor.shell_integration.b0e8"), shutil.which("suw") or ""))
    else:
        out.append(Check(WARN, i18n.msg("doctor.shell_integration_missing.aeec"), str(rc), "suw bootstrap"))
    wanted = ["fzf", "rg", "jq", "zoxide", "btop"]
    lacking = [t for t in wanted if not have(t)] + ([] if have("fdfind") or have("fd") else ["fd"])
    if lacking:
        fix = "installers/macos/packages.sh" if paths.platform() == "macos" else "sudo installers/ubuntu/packages.sh"
        out.append(Check(NONE, i18n.msg("doctor.optional_tools_not_installed.2fff", p1=', '.join(lacking)), "", fix))
    return out


# ── Sync ────────────────────────────────────────────────────────────────────


def _auth_check(rows: list[dict]) -> Check:
    if any(r["status"] == "AUTH_REQUIRED" for r in rows):
        names = ", ".join(r["name"] for r in rows if r["status"] == "AUTH_REQUIRED")
        fix = "gh auth login && gh auth setup-git" if have("gh") else "configure a git credential helper or an SSH key for your Git host"
        return Check(FAIL, i18n.msg("doctor.git_remote_rejects_the_credentials.0c10"), names, fix)
    if have("gh"):
        if run(["gh", "auth", "status"], timeout=15).ok:
            return Check(PASS, "Authentication", i18n.msg("doctor.github_cli_signed_in.a87f"))
        return Check(WARN, i18n.msg("doctor.github_cli_is_installed_but_not.f80b"), "", "gh auth login")
    helper = run(["git", "config", "--get", "credential.helper"], timeout=5).out.strip()
    if helper:
        return Check(PASS, "Authentication", i18n.msg("doctor.git_credential_helper.5583", p1=helper.split()[0]))
    return Check(WARN, i18n.msg("doctor.no_git_credential_mechanism_detected.3837"), i18n.msg("doctor.github_cli_gh_is_not_installed.0a49"), i18n.msg("doctor.install_gh_then_gh_auth_login.64dc"))


def check_sync(cfg: Config) -> list[Check]:
    from ..daemon import client

    out = []
    runtime = state.load("runtime")
    if client.running():
        watcher = runtime.get("watcher", {})
        how = f"file watching: {watcher.get('watches', 0)} watches" if watcher.get("available") else "periodic scan (no native file watching)"
        out.append(Check(PASS, i18n.msg("doctor.daemon_running.7809"), how))
    else:
        fix = "launchctl kickstart -k gui/$(id -u)/io.github.ambartsumov.suwd" if paths.platform() == "macos" else "systemctl --user enable --now suwd"
        out.append(Check(FAIL, i18n.msg("doctor.daemon_not_running.63b1"), i18n.msg("doctor.background_sync_and_health_checks_are.5fb5"), fix))

    roots = projects.roots(cfg)
    absent = [str(r) for r in roots if not r.is_dir()]
    out.append(Check(WARN, i18n.msg("doctor.project_root_missing.e4ac"), ", ".join(absent), "suw bootstrap") if absent else Check(PASS, i18n.msg("doctor.project_roots.7fc2"), ", ".join(projects.tilde(r) for r in roots)))

    name = run(["git", "config", "--global", "user.name"], timeout=5).out.strip()
    email = run(["git", "config", "--global", "user.email"], timeout=5).out.strip()
    if name and email:
        out.append(Check(PASS, i18n.msg("doctor.git_identity.17d9"), f"{name} <{email}>"))
    else:
        out.append(Check(FAIL, i18n.msg("doctor.git_identity_missing.656f"), "", 'git config --global user.name "…" && git config --global user.email "…"'))

    rows = runtime.get("projects") if client.running() and runtime.get("projects") is not None else syncer.local_rows(cfg)
    work = [r for r in rows if r["path"] != str(paths.shared_dir())]
    no_remote = [r["name"] for r in rows if not r.get("upstream") and r["status"] != "RECOVERY_REQUIRED"]
    out.append(Check(WARN, i18n.msg("doctor.repo_s_have_no_remote.a8ff", p1=len(no_remote)), ", ".join(no_remote), "git remote add origin … && git push -u origin HEAD") if no_remote else Check(PASS, "Remotes", f"{len(rows)} repo(s)"))
    out.append(_auth_check(rows))

    counts = syncer.counts(rows)
    bad = [r for r in rows if r["status"] in ("CONFLICT", "DIVERGED", "RECOVERY_REQUIRED")]
    if bad:
        out.append(Check(FAIL, f"{len(bad)} project(s) need a decision", ", ".join(f"{r['name']} ({r['status']})" for r in bad), i18n.msg("doctor.suw_project_recovery.204c", p1=bad[0]['name'])))
    else:
        out.append(Check(PASS, "Conflicts", "0"))
    stuck = [r for r in rows if r["status"] in ("PUSH_FAILED", "OFFLINE")]
    if stuck:
        out.append(Check(WARN, i18n.msg("doctor.project_s_waiting_for_the_remote.1454", p1=len(stuck)), ", ".join(f"{r['name']} ({r['status']})" for r in stuck), "suw sync retry"))
    else:
        out.append(Check(PASS, i18n.msg("doctor.pending_operations.9e3a"), str(counts["pending"])))
    manual = [r["name"] for r in work if r.get("dirty") and not r.get("autosync")]
    if manual:
        out.append(Check(WARN, i18n.msg("doctor.project_s_have_uncommitted_changes_and.eee3", p1=len(manual)), ", ".join(manual), i18n.msg("doctor.commit_or_suw_project_autosync_on.f016")))

    blocked = [r for r in rows if r.get("held")]
    exposed = []
    for row in work:
        current = gitsync.collect(row["path"])
        pol = gitsync.project_policy(row["path"], cfg.data)
        exposed += [f"{row['name']}/{f}" for f in current.new if policy.sensitive_name(f, pol["deny"], pol["allow"])]
    if exposed:
        out.append(Check(WARN, i18n.msg("doctor.secret_looking_files_are_not_ignored.1f4e"), ", ".join(exposed[:5]), i18n.msg("doctor.add_them_to_gitignore_suw_never.de31")))
    elif blocked:
        out.append(Check(WARN, i18n.msg("doctor.files_held_back_from_checkpoints.896e"), ", ".join(f"{r['name']}: {h['path']} ({h['detail']})" for r in blocked for h in r["held"][:2]), "suw project status <name>"))
    else:
        out.append(Check(PASS, i18n.msg("doctor.ignored_secrets.df20"), i18n.msg("doctor.no_secret_looking_files_waiting_to.ddf7")))
    out.append(Check(PASS, i18n.msg("doctor.large_file_policy.c55a"), i18n.msg("doctor.warn_mb_hold_back_mb_and.51de", p1=cfg.get('autosync.warn_file_mb', 5), p2=cfg.get('autosync.max_file_mb', 50))))

    found = projects.scan(cfg)["unmanaged"]
    if found:
        out.append(Check(NONE, i18n.msg("doctor.git_repositories_found_outside_suw_management.a47d", p1=len(found)), ", ".join(projects.tilde(paths.expand(p)) for p in found[:4]), "suw project add <path>"))
    if not work:
        out.append(Check(NONE, i18n.msg("doctor.no_projects_enrolled_yet.a0bb"), "", i18n.msg("doctor.clone_or_move_a_repository_into.923c")))
    return out


# ── Network ─────────────────────────────────────────────────────────────────


def check_network(cfg: Config) -> list[Check]:
    from ..integrations import tailscale

    out = []
    st = tailscale.status()
    if not st["installed"]:
        out.append(Check(FAIL, i18n.msg("doctor.tailscale_not_installed.fe6e"), "", i18n.msg("doctor.see_https_tailscale_com_download_and.e826")))
    elif not st["running"]:
        out.append(Check(FAIL, i18n.msg("doctor.tailscale_not_connected.e143"), st.get("state", ""), "tailscale up"))
    else:
        if tailscale.cut_off(st):
            reason = (st.get("health") or ["no connection to the coordination server"])[0]
            out.append(Check(WARN, i18n.msg("doctor.tailscale_is_running_but_cut_off.2955"), reason, i18n.msg("doctor.check_this_network_captive_portal_firewall.6795")))
        else:
            out.append(Check(PASS, "Tailscale", st["self"].get("dns", "")))
        if st.get("magic_dns") and _resolves(st["self"].get("dns", "")):
            out.append(Check(PASS, "DNS", i18n.msg("doctor.magicdns_names_resolve.9266")))
        else:
            out.append(Check(WARN, i18n.msg("doctor.magicdns_names_do_not_resolve.2a46"), "", "sudo tailscale set --accept-dns=true  (and enable MagicDNS in the admin console)"))

    inv = inventory.load()
    home = inv["devices"].get("home", {})
    if not inventory.device_host(home):
        out.append(Check(NONE, i18n.msg("doctor.home_server_is_not_configured.6fa3"), "", "suw home set <tailscale-name> --user <user>"))
    else:
        report = health.probe_remote("home")
        status = health.server_status(True, report)
        if status == health.ONLINE:
            out.append(Check(PASS, "Home", str(report.get("os", ""))))
        elif status == health.UNTRUSTED:
            out.append(Check(FAIL if health.identity_changed(report) else WARN, i18n.msg("doctor.home_identity_is_not_trusted.57e8"), report.get("error", ""), "suw home trust"))
        elif status == health.AUTH_REQUIRED:
            out.append(Check(WARN, i18n.msg("doctor.home_refuses_your_ssh_key.7733"), report.get("error", ""), "ssh-copy-id home"))
        elif tailscale.cut_off(st) and tailscale.on_tailnet(inventory.device_host(home), st):
            out.append(Check(NA, i18n.msg("doctor.home_cannot_be_tested_from_here.8c60"), i18n.msg("doctor.this_machine_has_no_path_to.10b6")))
        else:
            out.append(Check(WARN, f"Home {status.lower()}", report.get("error", ""), "ssh home   (check Tailscale on the server)"))

    label, node = inventory.cloud_current(inv)
    if not node:
        out.append(Check(NONE, i18n.msg("doctor.cloud_is_not_configured.2d5a"), i18n.msg("doctor.nothing_rented_right_now.0c86"), "suw cloud add"))
    else:
        report = health.probe_remote("cloud")
        status = health.server_status(True, report, direct=not node.get("tailscale_name"))
        if status == health.ONLINE:
            out.append(Check(PASS, "Cloud", f"{label} {health.gpu_label(report.get('gpus', []))}".strip()))
        elif status == health.DEGRADED:
            out.append(Check(WARN, i18n.msg("doctor.cloud_reachable_only_over_its_public.b123"), label, "suw cloud replace   (with a Tailscale auth key) or accept the degraded path"))
        elif health.identity_changed(report):
            out.append(Check(FAIL, i18n.msg("doctor.cloud_identity_changed_unexpectedly_connection_blocked.03fc"), label, "suw cloud verify   then `suw cloud replace` if intentional"))
        elif tailscale.cut_off(st) and node.get("tailscale_name") and status == health.OFFLINE:
            out.append(Check(NA, i18n.msg("doctor.cloud_cannot_be_tested_from_here.509e"), i18n.msg("doctor.this_machine_has_no_path_to.10b6")))
        else:
            out.append(Check(WARN, f"Cloud {status.lower().replace('_', ' ')}", report.get("error", ""), "suw cloud status   (or `suw cloud replace` if it was destroyed)"))

    for name, device in sorted(inv["devices"].items()):
        if device.get("role") == "workstation" and name != cfg.device and not device.get("tailscale_name"):
            nice = "MacBook" if device.get("platform") == "macos" else name
            out.append(Check(NONE, i18n.msg("doctor.has_not_been_enrolled.b886", p1=nice), "", i18n.msg("doctor.on_that_machine_docs_mac_day.b49b")))
    return out


# ── Clipboard ───────────────────────────────────────────────────────────────


def check_home(cfg: Config) -> list[Check]:
    """`suw home doctor`: the path to the home server, one hop at a time."""
    import socket as sock

    inv = inventory.load()
    home = inv["devices"].get("home", {})
    host = inventory.device_host(home)
    if not host:
        return [Check(NONE, i18n.msg("doctor.home_server_is_not_configured.6fa3"), i18n.msg("doctor.suw_never_guesses_which_machine_is.8705"), "suw home set <tailscale-name> --user <user>")]
    out = [Check(PASS, i18n.msg("doctor.logical_name.0dd6"), i18n.msg("doctor.ssh_home.38bd", p1=home.get('ssh_user', ''), p2=host))]
    if not _resolves(host):
        out.append(Check(FAIL, i18n.msg("doctor.the_name_does_not_resolve.4aa0"), host, "tailscale status   (is this machine and the server on the tailnet?)"))
        return out
    out.append(Check(PASS, i18n.msg("doctor.name_resolves.87f1"), host))
    port = int(home.get("ssh_port", 22))
    try:
        with sock.create_connection((host, port), timeout=6):
            pass
        out.append(Check(PASS, i18n.msg("doctor.ssh_port_reachable.6993"), f"{port}/tcp"))
    except OSError as exc:
        out.append(Check(FAIL, i18n.msg("doctor.ssh_port_is_not_reachable.d35e"), str(exc)[:120], "is the server on? `tailscale ping " + host.split(".")[0] + "`"))
        return out
    report = health.probe_remote("home", list(cfg.get("home.services", [])))
    status = health.server_status(True, report)
    if status == health.UNTRUSTED:
        changed = health.identity_changed(report)
        out.append(Check(FAIL if changed else WARN, "Host identity " + ("CHANGED since it was trusted — connection blocked" if changed else "is not trusted yet"), report.get("error", ""), "suw home trust   (compare the fingerprint first)"))
        return out
    if status == health.AUTH_REQUIRED:
        out.append(Check(FAIL, i18n.msg("doctor.the_server_refuses_your_ssh_key.2ec7"), report.get("error", ""), "ssh-copy-id home"))
        return out
    if not report.get("online"):
        out.append(Check(FAIL, i18n.msg("doctor.a_command_could_not_be_run.b4e0"), report.get("error", ""), "ssh -v home"))
        return out
    out.append(Check(PASS, i18n.msg("doctor.host_identity_verified_key_accepted_command.5ad1"), str(report.get("os", ""))))
    disk = report.get("disk_used_pct")
    if isinstance(disk, (int, float)):
        out.append(Check(WARN if disk >= 90 else PASS, f"Disk {int(disk)}% used", ""))
    for unit, st in (report.get("services") or {}).items():
        out.append(Check(PASS if st == "active" else WARN, f"Service {unit}", st, "" if st == "active" else i18n.msg("doctor.suw_home_logs.f43e", p1=unit)))
    return out


def check_clipboard(cfg: Config) -> list[Check]:
    from ..integrations import clipboard

    caps = clipboard.capabilities(cfg)
    out = [Check(PASS, i18n.msg("doctor.local_clipboard.72dc")) if caps["local"] == "ONLINE" else Check(WARN, i18n.msg("doctor.no_local_clipboard_tool.b8ff"), "", "sudo apt install wl-clipboard")]
    term = caps["terminal"]
    if caps["terminal_osc52"]:
        out.append(Check(PASS, i18n.msg("doctor.terminal_supports_osc_52.b636"), term))
    elif caps["terminal_osc52"] is False:
        fix = "brew install --cask wezterm" if paths.platform() == "macos" else "sudo installers/ubuntu/packages.sh   (installs WezTerm)"
        out.append(Check(WARN, i18n.msg("doctor.terminal_cannot_receive_the_ssh_clipboard.2a79"), i18n.msg("doctor.ignores_osc_52_copying_on_a.5585", p1=term), fix))
    else:
        out.append(Check(WARN, i18n.msg("doctor.terminal_osc_52_support_unknown.459f"), term or i18n.msg("doctor.no_terminal_found.152f"), "suw clipboard test   (inside the terminal you use)"))
    if not have("tmux"):
        out.append(Check(WARN, i18n.msg("doctor.tmux_not_installed.66dd"), "", i18n.msg("doctor.install_tmux.1623")))
    elif caps["tmux"]:
        out.append(Check(PASS, i18n.msg("doctor.tmux_clipboard.25c5"), i18n.msg("doctor.set_clipboard_on.ed0b")))
    else:
        out.append(Check(WARN, i18n.msg("doctor.tmux_clipboard_not_configured.a1de"), "", "suw bootstrap"))
    last = state.load("clipboard_test")
    if last.get("ok"):
        import time

        days = int((time.time() - float(last["when"])) / 86400) if last.get("when") else None
        when = "date unknown" if days is None else "today" if days == 0 else f"{days} day(s) ago"
        detail = f"{last.get('via', 'local')} via {last.get('terminal') or 'terminal'}, {when}"
        if days is None or days > 30:
            out.append(Check(NONE, i18n.msg("doctor.clipboard_round_trip_not_proven_recently.f0d1"), detail, "suw clipboard test"))
        else:
            out.append(Check(PASS, i18n.msg("doctor.clipboard_round_trip_proven.dd58"), detail))
    else:
        out.append(Check(NONE, i18n.msg("doctor.clipboard_round_trip_not_proven_yet.0399"), last.get("detail", ""), "suw clipboard test"))
    return out


# ── Security ────────────────────────────────────────────────────────────────


def check_security(cfg: Config) -> list[Check]:
    out = []
    ssh = paths.ssh_dir()
    if ssh.is_dir():
        mode = stat.S_IMODE(ssh.stat().st_mode)
        out.append(Check(PASS, i18n.msg("doctor.ssh_directory_permissions.c794"), oct(mode)[2:]) if not mode & 0o077 else Check(WARN, i18n.msg("doctor.ssh_is_readable_by_others.c887"), oct(mode)[2:], "chmod 700 ~/.ssh"))
        loose = []
        for key in sorted(ssh.glob("id_*")):
            if key.suffix != ".pub" and key.is_file() and stat.S_IMODE(key.stat().st_mode) & 0o077:
                loose.append(key.name)
        out.append(Check(FAIL, i18n.msg("doctor.private_key_readable_by_others.ba6e"), ", ".join(loose), "chmod 600 ~/.ssh/" + loose[0]) if loose else Check(PASS, i18n.msg("doctor.private_key_permissions.003e")))
    else:
        out.append(Check(NONE, i18n.msg("doctor.no_ssh_directory_yet.77c3"), "", "ssh-keygen -t ed25519"))

    conf = ssh / "suw.conf"
    text = conf.read_text(errors="replace") if conf.exists() else ""
    weak = [ln.strip() for ln in text.splitlines() if ln.strip().lower().replace("=", " ").split()[:2] in (["stricthostkeychecking", "no"], ["stricthostkeychecking", "off"], ["userknownhostsfile", "/dev/null"])]
    out.append(Check(FAIL, i18n.msg("doctor.host_key_verification_is_disabled_in.490b"), weak[0], "suw bootstrap --only ssh") if weak else Check(PASS, i18n.msg("doctor.host_verification.7453"), i18n.msg("doctor.strict_cloud_keys_pinned_per_node.7477")))

    leaks = []
    for path, _pol in syncer.managed(cfg):
        if gitsync.url_has_credentials(gitsync.remote_url(path)):
            leaks.append(path.name)
    out.append(Check(FAIL, i18n.msg("doctor.a_git_remote_url_contains_a.dd72"), ", ".join(leaks), i18n.msg("doctor.git_remote_set_url_origin_url.c996")) if leaks else Check(PASS, i18n.msg("doctor.secret_scanning.f6a3"), i18n.msg("doctor.no_credentials_in_remote_urls_checkpoints.7d22")))

    sock = paths.socket_path()
    if sock.exists():
        mode = stat.S_IMODE(sock.stat().st_mode)
        mine = sock.stat().st_uid == os.getuid()
        out.append(Check(PASS, i18n.msg("doctor.daemon_socket.e2b8"), i18n.msg("doctor.owner_only.dace", p1=oct(mode)[2:])) if mine and not mode & 0o077 else Check(FAIL, i18n.msg("doctor.daemon_socket_is_accessible_to_others.f8d9"), oct(mode)[2:], "suw daemon restart"))
    out.append(Check(PASS, i18n.msg("doctor.dangerous_git_operations.eeff"), i18n.msg("doctor.automation_cannot_overwrite_remote_history_or.b418")))
    backend = secrets.backend()
    out.append(Check(PASS, i18n.msg("doctor.secret_store.965d"), backend) if backend != "none" else Check(WARN, i18n.msg("doctor.no_secret_store.0787"), "", "sudo apt install libsecret-tools gnome-keyring"))
    return out


# ── Recovery ────────────────────────────────────────────────────────────────


def check_recovery(cfg: Config) -> list[Check]:
    out = []
    entries = journal.entries()
    backups = [e for e in entries if e["action"] == "backup"]
    lost = [e["target"] for e in backups if not os.path.exists(e["backup"])]
    damaged = journal.verify_backups()
    if lost or damaged:
        out.append(Check(WARN, i18n.msg("doctor.some_pre_install_backups_are_missing.b175"), ", ".join([*lost, *damaged][:3]), "suw rollback --dry-run"))
    else:
        out.append(Check(PASS, "Backups", i18n.msg("doctor.file_s_saved_before_modification.8f79", p1=len(backups))))
    out.append(Check(PASS, i18n.msg("doctor.rollback_metadata.0bee"), i18n.msg("doctor.journaled_change_s.6d2e", p1=len(entries))) if entries else Check(NONE, i18n.msg("doctor.nothing_installed_through_the_journal_yet.0a44"), "", "suw bootstrap"))
    store = state.health()
    if store["unreadable"]:
        out.append(Check(FAIL, i18n.msg("doctor.state_store_has_unreadable_documents.70ed"), ", ".join(store["unreadable"]), "suw daemon restart   (repairs from the previous version)"))
    else:
        note = f"schema {store['schema']}, {store['documents']} document(s)" + (f", {len(store['quarantined'])} quarantined after a repair" if store["quarantined"] else "")
        out.append(Check(PASS, i18n.msg("doctor.state_database.c2a5"), note))
    shared = paths.shared_dir()
    if (shared / ".git").exists() and gitsync.remote_url(shared):
        out.append(Check(PASS, i18n.msg("doctor.configuration_backup.7819"), i18n.msg("doctor.shared_config_has_a_remote.d5ae")))
    else:
        out.append(Check(WARN, i18n.msg("doctor.shared_configuration_exists_only_on_this.d263"), "", i18n.msg("doctor.create_a_private_repository_and_git.b23b")))
    pinned = [(path.name, len(gitsync.recovery_branches(path))) for path, _ in syncer.managed(cfg)]
    pinned = [(n, c) for n, c in pinned if c]
    if pinned:
        out.append(Check(NONE, i18n.msg("doctor.recovery_branches_are_still_present.1077"), ", ".join(f"{n} ({c})" for n, c in pinned), i18n.msg("doctor.suw_project_recovery_done_after_you.b1fc", p1=pinned[0][0])))
    return out


def check_startup(cfg: Config) -> list[Check]:
    """Will the background pieces come back by themselves after a reboot or a new login?"""
    from ..integrations import peripherals, worksync

    wanted = [("suwd", "io.github.ambartsumov.suwd", "Background daemon")]
    if worksync.enabled(cfg) and worksync.instance(cfg).configured():
        wanted.append(("suw-syncthing", "io.github.ambartsumov.syncthing", "Work folder sync"))
    if peripherals.enabled(cfg) and peripherals.status(cfg)["state"] not in ("DISABLED", "NOT_INSTALLED", "NOT_CONFIGURED"):
        wanted.append(("suw-deskflow", "io.github.ambartsumov.deskflow", "Keyboard/mouse sharing"))
    out = []
    for unit, label, title in wanted:
        if paths.platform() == "macos":
            there = (paths.home() / "Library" / "LaunchAgents" / f"{label}.plist").exists()
        else:
            there = run(["systemctl", "--user", "is-enabled", f"{unit}.service"], timeout=10).out.strip() == "enabled"
        out.append(Check(PASS, i18n.msg("doctor.starts_at_login.eb23", p1=title)) if there else Check(WARN, i18n.msg("doctor.does_not_start_at_login.3bac", p1=title), i18n.msg("doctor.it_runs_now_but_would_not.a349"), "suw bootstrap"))
    return out


# ── Work folder ─────────────────────────────────────────────────────────────


def check_work(cfg: Config) -> list[Check]:
    from ..integrations import worksync

    report = worksync.status(cfg)
    state_word = report["state"]
    path = report["path"].replace(str(paths.home()), "~")
    if state_word == "DISABLED":
        return [Check(NONE, i18n.msg("doctor.shared_work_folder_is_switched_off.e1d5"), "", "suw config set work.enabled true && suw work setup")]
    if state_word == "NOT_INSTALLED":
        fix = "installers/macos/packages.sh" if paths.platform() == "macos" else "sudo installers/ubuntu/packages.sh"
        return [Check(WARN, i18n.msg("doctor.shared_work_folder_syncthing_is_not.d7c7"), path, fix)]
    if state_word == "NOT_CONFIGURED":
        return [Check(WARN, i18n.msg("doctor.shared_work_folder_is_not_set.38a2"), path, "suw work setup")]
    out = [Check(PASS, i18n.msg("doctor.work_folder.e445"), f"{path} · {report.get('files', 0)} files") if report["exists"] else Check(FAIL, i18n.msg("doctor.work_folder_is_missing.1745"), path, "suw work setup")]
    if state_word == "STOPPED":
        out.append(Check(FAIL, i18n.msg("doctor.work_sync_service_is_not_running.e9b6"), "", "suw work setup"))
        return out
    out.append(Check(PASS, i18n.msg("doctor.work_sync_service.015b"), i18n.msg("doctor.private_syncthing_instance_loopback_api_only.faca")))
    if report.get("paused"):
        out.append(Check(WARN, i18n.msg("doctor.work_sync_is_paused.30a4"), "", "suw work resume"))
    for error in report.get("errors", [])[:3]:
        out.append(Check(FAIL, i18n.msg("doctor.work_sync_error.ff93"), (f"{error['path']}: " if error["path"] else "") + error["error"][:160], "suw work status"))
    peers = report.get("peers", [])
    if not peers:
        waiting = report.get("unpaired", [])
        out.append(Check(NONE, i18n.msg("doctor.no_other_workstation_is_paired_yet.773f"), i18n.msg("doctor.files_stay_on_this_machine_until.d167"), i18n.msg("doctor.suw_work_pair.9549", p1=waiting[0]) if waiting else i18n.msg("doctor.run_suw_bootstrap_on_the_other.f026")))
    for peer in peers:
        if peer["connected"]:
            pending = peer["need_items"] + report.get("need_items", 0)
            out.append(Check(PASS, f"Workstation '{peer['name']}' connected", "up to date" if not pending else f"{pending} item(s) in transit"))
        else:
            out.append(Check(NA, f"Workstation '{peer['name']}' is offline", i18n.msg("doctor.changes_are_queued_and_delivered_when.255d")))
    for name in report.get("unpaired", []) if peers else []:
        out.append(Check(WARN, i18n.msg("doctor.workstation_is_waiting_to_be_paired.b6c4", p1=name), "", i18n.msg("doctor.suw_work_pair.9549", p1=name)))
    conflicts = report.get("conflicts", [])
    if conflicts:
        out.append(Check(WARN, i18n.msg("doctor.conflict_copies_in_the_work_folder.fcbe", p1=len(conflicts)), conflicts[0]["original"], "suw work conflicts"))
    held = [item for item in report.get("large", []) if item.get("held")]
    if held:
        out.append(Check(NONE, i18n.msg("doctor.large_file_s_are_not_synchronised.bbf9", p1=len(held)), f"{held[0]['path']} ({held[0]['mb']} MB)", "suw work allow <path>   or   suw artifact push <path>"))
    for item in report.get("unsupported", [])[:3]:
        out.append(Check(WARN, i18n.msg("doctor.a_file_cannot_be_shared.a73d"), f"{item['path']}: {item['reason']}", i18n.msg("doctor.rename_or_remove_it.0c6f")))
    detached = worksync.detached_repos(cfg) if report["exists"] else []
    if detached:
        out.append(Check(WARN, i18n.msg("doctor.project_folder_s_arrived_without_their.a2e2", p1=len(detached)), detached[0]["path"], "suw work repos --attach"))
    if paths.platform() == "macos" and (paths.home() / "Library/Mobile Documents/com~apple~CloudDocs/Desktop").exists() and "Desktop" in report["path"]:
        out.append(Check(WARN, i18n.msg("doctor.icloud_also_syncs_the_desktop_folder.3da0"), i18n.msg("doctor.two_sync_engines_on_one_folder.99fa"), i18n.msg("doctor.system_settings_apple_id_icloud_drive.941c")))
    return out


# ── Peripherals ─────────────────────────────────────────────────────────────


def check_peripherals(cfg: Config) -> list[Check]:
    from ..integrations import peripherals

    report = peripherals.status(cfg)
    word = report["state"]
    if word == "DISABLED":
        return [Check(NONE, i18n.msg("doctor.keyboard_mouse_sharing_is_switched_off.4546"), "", "suw config set peripherals.enabled true")]
    if word == "NOT_INSTALLED":
        return [Check(NONE, i18n.msg("doctor.keyboard_mouse_sharing_deskflow_is_not.3507"), i18n.msg("doctor.each_machine_uses_its_own_keyboard.6fdd"), i18n.msg("doctor.installers_packages_sh_then_suw_peripherals.c575"))]
    if word == "NOT_CONFIGURED":
        return [Check(NONE, i18n.msg("doctor.keyboard_mouse_sharing_is_not_set.6ed3"), i18n.msg("doctor.each_machine_uses_its_own_keyboard.6fdd"), "suw peripherals setup")]
    role = f"{report['role']}, screen name {report['screen']}"
    if word == "STOPPED":
        return [Check(WARN, i18n.msg("doctor.keyboard_mouse_sharing_service_is_not.751d"), role, "suw peripherals setup   (logs: journalctl --user -u suw-deskflow)")]
    out = [Check(PASS, i18n.msg("doctor.deskflow_core_running.e0a2"), role)]
    if report["role"] == "server" and not report.get("listening"):
        out.append(Check(WARN, i18n.msg("doctor.deskflow_is_running_but_not_listening.a7b4", p1=report['port']), i18n.msg("doctor.the_desktop_s_permission_dialog_may.8470"), i18n.msg("doctor.answer_the_dialog_logs_journalctl_user.fdaf")))
    for name in report.get("waiting", []):
        out.append(Check(NONE, i18n.msg("doctor.workstation_is_waiting_to_be_approved.1707", p1=name), "", i18n.msg("doctor.suw_peripherals_pair.1ca4", p1=name)))
    if word == "CONNECTED":
        out.append(Check(PASS, i18n.msg("doctor.other_workstation_connected.1ece"), i18n.msg("doctor.one_keyboard_one_mouse_shared_clipboard.72e4")))
    elif word == "UNPAIRED":
        out.append(Check(NONE, i18n.msg("doctor.no_other_workstation_is_approved_yet.8845"), i18n.msg("doctor.this_machine_keeps_its_own_keyboard.9565")))
    else:
        out.append(Check(NA, i18n.msg("doctor.other_workstation_is_not_connected.203f"), i18n.msg("doctor.this_machine_keeps_its_own_keyboard.acf9")))
    if peripherals.exposed(cfg):
        out.append(Check(NONE, i18n.msg("doctor.deskflow_listens_on_every_network_interface.78dd", p1=report['port']), i18n.msg("doctor.protected_by_tls_and_approved_fingerprints.0df2"), 'suw config set peripherals.bind tailnet'))
    return out


CHECKS = [
    ("Workstation", check_workstation),
    ("Sync", check_sync),
    ("Work folder", check_work),
    ("Peripherals", check_peripherals),
    ("Network", check_network),
    ("Clipboard", check_clipboard),
    ("Security", check_security),
    ("Startup", check_startup),
    ("Recovery", check_recovery),
]


def run_all(cfg: Config) -> list[Check]:
    results: list[Check] = []
    for section, check in CHECKS:
        try:
            found = check(cfg)
        except Exception as exc:  # a broken check is itself a finding, not a crash
            found = [Check(WARN, i18n.msg("doctor.checks_could_not_run.7445", p1=section), str(exc)[:200], "suw history")]
        for item in found:
            item.section = i18n.msg("doctor.section." + section.lower().replace(" ", "_"))
        results.extend(found)
    return results


def verdict(results: list[Check]) -> str:
    if any(c.level == FAIL for c in results):
        return "NOT READY"
    if any(c.level == WARN for c in results):
        return "READY WITH WARNINGS"
    return "READY"


def notes(results: list[Check]) -> list[str]:
    """The numbered summary: everything that is not a plain PASS."""
    return [c.title + ("." if not c.title.endswith(".") else "") for c in results if c.level != PASS]
