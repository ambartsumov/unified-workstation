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

from . import gitsync, health, inventory, journal, modes, paths, policy, projects, secrets, state, syncer
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
        out.append(Check(FAIL, "Python 3.11+ required", sys.version.split()[0], "re-run the installer"))
    for warning in cfg.warnings:
        out.append(Check(WARN, "A config file has errors (last good version in use)", warning, "suw config validate"))
    disk = report.get("disk_used_pct")
    if isinstance(disk, (int, float)) and disk >= 92:
        out.append(Check(WARN, f"Disk {int(disk)}% full", "", "free space; sync and checkpoints need room"))

    linux = paths.platform() == "linux"
    if linux:
        session = os.environ.get("XDG_SESSION_TYPE", "") or "unknown"
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or "unknown"
        out.append(Check(PASS if gnome.available() else NONE, f"Desktop {desktop} on {session}", "" if gnome.available() else "mode switching changes apps only, not the desktop"))
    out.append(Check(PASS, "Mode integration", f"mode {modes.current()}"))

    if linux and gnome.available():
        shortcut = str(cfg.get("workstation.shortcut", "<Super>d"))
        used = [c for c in gnome.find_conflicts(shortcut) if c[0] != gnome.CUSTOM or "suw-toggle" not in c[1]]
        registered = "suw-toggle" in (run(["gsettings", "get", gnome.MEDIA_KEYS, "custom-keybindings"], timeout=5).out)
        if registered and not used:
            out.append(Check(PASS, "Keyboard shortcut", shortcut))
        elif registered:
            out.append(Check(WARN, "Mode shortcut conflicts with another binding", str(used[0]), "suw config set workstation.shortcut '<Super><Alt>d' && suw bootstrap"))
        else:
            out.append(Check(WARN, "Mode shortcut not registered", "", "suw bootstrap"))
        ext = gnome.extension_state()
        if ext == "active":
            out.append(Check(PASS, "Workspaces", "window placement helper active"))
        elif ext == "stale":
            out.append(Check(WARN, "Workspaces: an updated placement helper loads at next login", "until then leaving the mode closes its terminals only, never an editor window", "log out and back in once"))
        elif ext == "installed":
            out.append(Check(WARN, "Workspaces: placement helper loads at next login", "until then apps open on the current workspace", "log out and back in once"))
        else:
            out.append(Check(NONE, "Workspaces: placement helper not installed", "optional", "suw bootstrap"))

    kind, exe = browser.detect(cfg)
    urls, missing = browser.session_urls(cfg)
    if missing:
        out.append(Check(WARN, "Browser session has links without an address", ", ".join(missing), f'suw config set urls.{missing[0]} "https://…"'))
    elif urls:
        out.append(Check(PASS, "Browser", f"{exe or 'system default'}: {len(urls)} tab(s), profile {cfg.get('workstation.browser.profile', 'existing')}"))
    else:
        out.append(Check(NONE, "Browser: no workstation URLs configured", "", "suw config edit --shared"))
    editor = apps.editor(cfg)
    out.append(Check(PASS, "Editor", editor) if editor else Check(WARN, "No editor found", "", "install VS Code or Cursor, or: suw config set workstation.editor <cmd>"))
    term = apps.terminal(cfg)
    out.append(Check(PASS, "Terminal", term) if term else Check(WARN, "No terminal emulator found", "", "install WezTerm"))
    out.append(Check(PASS, "tmux", "") if have("tmux") else Check(WARN, "tmux not installed", "persistent server sessions need it", "install tmux"))

    shell = os.path.basename(os.environ.get("SHELL", "zsh"))
    rc = paths.home() / (".zshrc" if shell == "zsh" else ".bashrc")
    if rc.exists() and "suw managed" in rc.read_text(errors="replace"):
        out.append(Check(PASS, "Shell integration", shutil.which("suw") or ""))
    else:
        out.append(Check(WARN, "Shell integration missing", str(rc), "suw bootstrap"))
    wanted = ["fzf", "rg", "jq", "zoxide", "btop"]
    lacking = [t for t in wanted if not have(t)] + ([] if have("fdfind") or have("fd") else ["fd"])
    if lacking:
        fix = "installers/macos/packages.sh" if paths.platform() == "macos" else "sudo installers/ubuntu/packages.sh"
        out.append(Check(NONE, f"Optional tools not installed: {', '.join(lacking)}", "", fix))
    return out


# ── Sync ────────────────────────────────────────────────────────────────────


def _auth_check(rows: list[dict]) -> Check:
    if any(r["status"] == "AUTH_REQUIRED" for r in rows):
        names = ", ".join(r["name"] for r in rows if r["status"] == "AUTH_REQUIRED")
        fix = "gh auth login && gh auth setup-git" if have("gh") else "configure a git credential helper or an SSH key for your Git host"
        return Check(FAIL, "Git remote rejects the credentials", names, fix)
    if have("gh"):
        if run(["gh", "auth", "status"], timeout=15).ok:
            return Check(PASS, "Authentication", "GitHub CLI signed in")
        return Check(WARN, "GitHub CLI is installed but not signed in (or offline)", "", "gh auth login")
    helper = run(["git", "config", "--get", "credential.helper"], timeout=5).out.strip()
    if helper:
        return Check(PASS, "Authentication", f"git credential helper: {helper.split()[0]}")
    return Check(WARN, "No Git credential mechanism detected", "GitHub CLI (`gh`) is not installed and no credential helper is set", "install gh then `gh auth login`, or use SSH remotes")


def check_sync(cfg: Config) -> list[Check]:
    from ..daemon import client

    out = []
    runtime = state.load("runtime")
    if client.running():
        watcher = runtime.get("watcher", {})
        how = f"file watching: {watcher.get('watches', 0)} watches" if watcher.get("available") else "periodic scan (no native file watching)"
        out.append(Check(PASS, "Daemon running", how))
    else:
        fix = "launchctl kickstart -k gui/$(id -u)/io.github.ambartsumov.suwd" if paths.platform() == "macos" else "systemctl --user enable --now suwd"
        out.append(Check(FAIL, "Daemon not running", "background sync and health checks are paused", fix))

    roots = projects.roots(cfg)
    absent = [str(r) for r in roots if not r.is_dir()]
    out.append(Check(WARN, "Project root missing", ", ".join(absent), "suw bootstrap") if absent else Check(PASS, "Project roots", ", ".join(projects.tilde(r) for r in roots)))

    name = run(["git", "config", "--global", "user.name"], timeout=5).out.strip()
    email = run(["git", "config", "--global", "user.email"], timeout=5).out.strip()
    if name and email:
        out.append(Check(PASS, "Git identity", f"{name} <{email}>"))
    else:
        out.append(Check(FAIL, "Git identity missing", "", 'git config --global user.name "…" && git config --global user.email "…"'))

    rows = runtime.get("projects") if client.running() and runtime.get("projects") is not None else syncer.local_rows(cfg)
    work = [r for r in rows if r["path"] != str(paths.shared_dir())]
    no_remote = [r["name"] for r in rows if not r.get("upstream") and r["status"] != "RECOVERY_REQUIRED"]
    out.append(Check(WARN, f"{len(no_remote)} repo(s) have no remote", ", ".join(no_remote), "git remote add origin … && git push -u origin HEAD") if no_remote else Check(PASS, "Remotes", f"{len(rows)} repo(s)"))
    out.append(_auth_check(rows))

    counts = syncer.counts(rows)
    bad = [r for r in rows if r["status"] in ("CONFLICT", "DIVERGED", "RECOVERY_REQUIRED")]
    if bad:
        out.append(Check(FAIL, f"{len(bad)} project(s) need a decision", ", ".join(f"{r['name']} ({r['status']})" for r in bad), f"suw project recovery {bad[0]['name']}"))
    else:
        out.append(Check(PASS, "Conflicts", "0"))
    stuck = [r for r in rows if r["status"] in ("PUSH_FAILED", "OFFLINE")]
    if stuck:
        out.append(Check(WARN, f"{len(stuck)} project(s) waiting for the remote", ", ".join(f"{r['name']} ({r['status']})" for r in stuck), "suw sync retry"))
    else:
        out.append(Check(PASS, "Pending operations", str(counts["pending"])))
    manual = [r["name"] for r in work if r.get("dirty") and not r.get("autosync")]
    if manual:
        out.append(Check(WARN, f"{len(manual)} project(s) have uncommitted changes and automatic checkpoints off", ", ".join(manual), "commit, or: suw project autosync on"))

    blocked = [r for r in rows if r.get("held")]
    exposed = []
    for row in work:
        current = gitsync.collect(row["path"])
        pol = gitsync.project_policy(row["path"], cfg.data)
        exposed += [f"{row['name']}/{f}" for f in current.new if policy.sensitive_name(f, pol["deny"], pol["allow"])]
    if exposed:
        out.append(Check(WARN, "Secret-looking files are not ignored by Git", ", ".join(exposed[:5]), "add them to .gitignore (SUW never commits them, but `git add` by hand would)"))
    elif blocked:
        out.append(Check(WARN, "Files held back from checkpoints", ", ".join(f"{r['name']}: {h['path']} ({h['detail']})" for r in blocked for h in r["held"][:2]), "suw project status <name>"))
    else:
        out.append(Check(PASS, "Ignored secrets", "no secret-looking files waiting to be committed"))
    out.append(Check(PASS, "Large-file policy", f"warn > {cfg.get('autosync.warn_file_mb', 5)} MB, hold back > {cfg.get('autosync.max_file_mb', 50)} MB and model/dataset files"))

    found = projects.scan(cfg)["unmanaged"]
    if found:
        out.append(Check(NONE, f"{len(found)} Git repositories found outside SUW management", ", ".join(projects.tilde(paths.expand(p)) for p in found[:4]), "suw project add <path>"))
    if not work:
        out.append(Check(NONE, "No projects enrolled yet", "", "clone or move a repository into ~/Projects/active, or: suw project add <path>"))
    return out


# ── Network ─────────────────────────────────────────────────────────────────


def check_network(cfg: Config) -> list[Check]:
    from ..integrations import tailscale

    out = []
    st = tailscale.status()
    if not st["installed"]:
        out.append(Check(FAIL, "Tailscale not installed", "", "see https://tailscale.com/download and run its installer"))
    elif not st["running"]:
        out.append(Check(FAIL, "Tailscale not connected", st.get("state", ""), "tailscale up"))
    else:
        if tailscale.cut_off(st):
            reason = (st.get("health") or ["no connection to the coordination server"])[0]
            out.append(Check(WARN, "Tailscale is running but cut off from the tailnet", reason, "check this network (captive portal, firewall); local work and Git are unaffected"))
        else:
            out.append(Check(PASS, "Tailscale", st["self"].get("dns", "")))
        if st.get("magic_dns") and _resolves(st["self"].get("dns", "")):
            out.append(Check(PASS, "DNS", "MagicDNS names resolve"))
        else:
            out.append(Check(WARN, "MagicDNS names do not resolve", "", "sudo tailscale set --accept-dns=true  (and enable MagicDNS in the admin console)"))

    inv = inventory.load()
    home = inv["devices"].get("home", {})
    if not inventory.device_host(home):
        out.append(Check(NONE, "Home server is not configured", "", "suw home set <tailscale-name> --user <user>"))
    else:
        report = health.probe_remote("home")
        status = health.server_status(True, report)
        if status == health.ONLINE:
            out.append(Check(PASS, "Home", str(report.get("os", ""))))
        elif status == health.UNTRUSTED:
            out.append(Check(FAIL if health.identity_changed(report) else WARN, "Home identity is not trusted", report.get("error", ""), "suw home trust"))
        elif status == health.AUTH_REQUIRED:
            out.append(Check(WARN, "Home refuses your SSH key", report.get("error", ""), "ssh-copy-id home"))
        elif tailscale.cut_off(st) and tailscale.on_tailnet(inventory.device_host(home), st):
            out.append(Check(NA, "Home cannot be tested from here", "this machine has no path to the tailnet right now; nothing is claimed about the server"))
        else:
            out.append(Check(WARN, f"Home {status.lower()}", report.get("error", ""), "ssh home   (check Tailscale on the server)"))

    label, node = inventory.cloud_current(inv)
    if not node:
        out.append(Check(NONE, "Cloud is not configured", "nothing rented right now", "suw cloud add"))
    else:
        report = health.probe_remote("cloud")
        status = health.server_status(True, report, direct=not node.get("tailscale_name"))
        if status == health.ONLINE:
            out.append(Check(PASS, "Cloud", f"{label} {health.gpu_label(report.get('gpus', []))}".strip()))
        elif status == health.DEGRADED:
            out.append(Check(WARN, "Cloud reachable only over its public endpoint", label, "suw cloud replace   (with a Tailscale auth key) or accept the degraded path"))
        elif health.identity_changed(report):
            out.append(Check(FAIL, "Cloud identity changed unexpectedly - connection blocked", label, "suw cloud verify   then `suw cloud replace` if intentional"))
        elif tailscale.cut_off(st) and node.get("tailscale_name") and status == health.OFFLINE:
            out.append(Check(NA, "Cloud cannot be tested from here", "this machine has no path to the tailnet right now; nothing is claimed about the server"))
        else:
            out.append(Check(WARN, f"Cloud {status.lower().replace('_', ' ')}", report.get("error", ""), "suw cloud status   (or `suw cloud replace` if it was destroyed)"))

    for name, device in sorted(inv["devices"].items()):
        if device.get("role") == "workstation" and name != cfg.device and not device.get("tailscale_name"):
            nice = "MacBook" if device.get("platform") == "macos" else name
            out.append(Check(NONE, f"{nice} has not been enrolled", "", "on that machine: docs/mac-day-one.md"))
    return out


# ── Clipboard ───────────────────────────────────────────────────────────────


def check_home(cfg: Config) -> list[Check]:
    """`suw home doctor`: the path to the home server, one hop at a time."""
    import socket as sock

    inv = inventory.load()
    home = inv["devices"].get("home", {})
    host = inventory.device_host(home)
    if not host:
        return [Check(NONE, "Home server is not configured", "SUW never guesses which machine is home", "suw home set <tailscale-name> --user <user>")]
    out = [Check(PASS, "Logical name", f"ssh home → {home.get('ssh_user', '')}@{host}")]
    if not _resolves(host):
        out.append(Check(FAIL, "The name does not resolve", host, "tailscale status   (is this machine and the server on the tailnet?)"))
        return out
    out.append(Check(PASS, "Name resolves", host))
    port = int(home.get("ssh_port", 22))
    try:
        with sock.create_connection((host, port), timeout=6):
            pass
        out.append(Check(PASS, "SSH port reachable", f"{port}/tcp"))
    except OSError as exc:
        out.append(Check(FAIL, "SSH port is not reachable", str(exc)[:120], "is the server on? `tailscale ping " + host.split(".")[0] + "`"))
        return out
    report = health.probe_remote("home", list(cfg.get("home.services", [])))
    status = health.server_status(True, report)
    if status == health.UNTRUSTED:
        changed = health.identity_changed(report)
        out.append(Check(FAIL if changed else WARN, "Host identity " + ("CHANGED since it was trusted — connection blocked" if changed else "is not trusted yet"), report.get("error", ""), "suw home trust   (compare the fingerprint first)"))
        return out
    if status == health.AUTH_REQUIRED:
        out.append(Check(FAIL, "The server refuses your SSH key", report.get("error", ""), "ssh-copy-id home"))
        return out
    if not report.get("online"):
        out.append(Check(FAIL, "A command could not be run on the server", report.get("error", ""), "ssh -v home"))
        return out
    out.append(Check(PASS, "Host identity verified, key accepted, command executed", str(report.get("os", ""))))
    disk = report.get("disk_used_pct")
    if isinstance(disk, (int, float)):
        out.append(Check(WARN if disk >= 90 else PASS, f"Disk {int(disk)}% used", ""))
    for unit, st in (report.get("services") or {}).items():
        out.append(Check(PASS if st == "active" else WARN, f"Service {unit}", st, "" if st == "active" else f"suw home logs {unit}"))
    return out


def check_clipboard(cfg: Config) -> list[Check]:
    from ..integrations import clipboard

    caps = clipboard.capabilities(cfg)
    out = [Check(PASS, "Local clipboard") if caps["local"] == "ONLINE" else Check(WARN, "No local clipboard tool", "", "sudo apt install wl-clipboard")]
    term = caps["terminal"]
    if caps["terminal_osc52"]:
        out.append(Check(PASS, "Terminal supports OSC 52", term))
    elif caps["terminal_osc52"] is False:
        fix = "brew install --cask wezterm" if paths.platform() == "macos" else "sudo installers/ubuntu/packages.sh   (installs WezTerm)"
        out.append(Check(WARN, "Terminal cannot receive the SSH clipboard", f"{term} ignores OSC 52; copying on a server will not reach this machine", fix))
    else:
        out.append(Check(WARN, "Terminal OSC 52 support unknown", term or "no terminal found", "suw clipboard test   (inside the terminal you use)"))
    if not have("tmux"):
        out.append(Check(WARN, "tmux not installed", "", "install tmux"))
    elif caps["tmux"]:
        out.append(Check(PASS, "tmux clipboard", "set-clipboard on"))
    else:
        out.append(Check(WARN, "tmux clipboard not configured", "", "suw bootstrap"))
    last = state.load("clipboard_test")
    if last.get("ok"):
        import time

        days = int((time.time() - float(last["when"])) / 86400) if last.get("when") else None
        when = "date unknown" if days is None else "today" if days == 0 else f"{days} day(s) ago"
        detail = f"{last.get('via', 'local')} via {last.get('terminal') or 'terminal'}, {when}"
        if days is None or days > 30:
            out.append(Check(NONE, "Clipboard round-trip not proven recently", detail, "suw clipboard test"))
        else:
            out.append(Check(PASS, "Clipboard round-trip proven", detail))
    else:
        out.append(Check(NONE, "Clipboard round-trip not proven yet", last.get("detail", ""), "suw clipboard test"))
    return out


# ── Security ────────────────────────────────────────────────────────────────


def check_security(cfg: Config) -> list[Check]:
    out = []
    ssh = paths.ssh_dir()
    if ssh.is_dir():
        mode = stat.S_IMODE(ssh.stat().st_mode)
        out.append(Check(PASS, "SSH directory permissions", oct(mode)[2:]) if not mode & 0o077 else Check(WARN, "~/.ssh is readable by others", oct(mode)[2:], "chmod 700 ~/.ssh"))
        loose = []
        for key in sorted(ssh.glob("id_*")):
            if key.suffix != ".pub" and key.is_file() and stat.S_IMODE(key.stat().st_mode) & 0o077:
                loose.append(key.name)
        out.append(Check(FAIL, "Private key readable by others", ", ".join(loose), "chmod 600 ~/.ssh/" + loose[0]) if loose else Check(PASS, "Private key permissions"))
    else:
        out.append(Check(NONE, "No SSH directory yet", "", "ssh-keygen -t ed25519"))

    conf = ssh / "suw.conf"
    text = conf.read_text(errors="replace") if conf.exists() else ""
    weak = [ln.strip() for ln in text.splitlines() if ln.strip().lower().replace("=", " ").split()[:2] in (["stricthostkeychecking", "no"], ["stricthostkeychecking", "off"], ["userknownhostsfile", "/dev/null"])]
    out.append(Check(FAIL, "Host key verification is disabled in the managed SSH config", weak[0], "suw bootstrap --only ssh") if weak else Check(PASS, "Host verification", "strict; cloud keys pinned per node"))

    leaks = []
    for path, _pol in syncer.managed(cfg):
        if gitsync.url_has_credentials(gitsync.remote_url(path)):
            leaks.append(path.name)
    out.append(Check(FAIL, "A Git remote URL contains a token", ", ".join(leaks), "git remote set-url origin <url without credentials>, then use a credential helper") if leaks else Check(PASS, "Secret scanning", "no credentials in remote URLs; checkpoints scan content"))

    sock = paths.socket_path()
    if sock.exists():
        mode = stat.S_IMODE(sock.stat().st_mode)
        mine = sock.stat().st_uid == os.getuid()
        out.append(Check(PASS, "Daemon socket", f"{oct(mode)[2:]}, owner only") if mine and not mode & 0o077 else Check(FAIL, "Daemon socket is accessible to others", oct(mode)[2:], "suw daemon restart"))
    out.append(Check(PASS, "Dangerous Git operations", "automation cannot overwrite remote history or reset a worktree"))
    backend = secrets.backend()
    out.append(Check(PASS, "Secret store", backend) if backend != "none" else Check(WARN, "No secret store", "", "sudo apt install libsecret-tools gnome-keyring"))
    return out


# ── Recovery ────────────────────────────────────────────────────────────────


def check_recovery(cfg: Config) -> list[Check]:
    out = []
    entries = journal.entries()
    backups = [e for e in entries if e["action"] == "backup"]
    lost = [e["target"] for e in backups if not os.path.exists(e["backup"])]
    damaged = journal.verify_backups()
    if lost or damaged:
        out.append(Check(WARN, "Some pre-install backups are missing or changed", ", ".join([*lost, *damaged][:3]), "suw rollback --dry-run"))
    else:
        out.append(Check(PASS, "Backups", f"{len(backups)} file(s) saved before modification"))
    out.append(Check(PASS, "Rollback metadata", f"{len(entries)} journaled change(s)") if entries else Check(NONE, "Nothing installed through the journal yet", "", "suw bootstrap"))
    store = state.health()
    if store["unreadable"]:
        out.append(Check(FAIL, "State store has unreadable documents", ", ".join(store["unreadable"]), "suw daemon restart   (repairs from the previous version)"))
    else:
        note = f"schema {store['schema']}, {store['documents']} document(s)" + (f", {len(store['quarantined'])} quarantined after a repair" if store["quarantined"] else "")
        out.append(Check(PASS, "State database", note))
    shared = paths.shared_dir()
    if (shared / ".git").exists() and gitsync.remote_url(shared):
        out.append(Check(PASS, "Configuration backup", "shared config has a remote"))
    else:
        out.append(Check(WARN, "Shared configuration exists only on this machine", "", "create a private repository and: git -C ~/.config/suw/shared remote add origin <url>"))
    pinned = [(path.name, len(gitsync.recovery_branches(path))) for path, _ in syncer.managed(cfg)]
    pinned = [(n, c) for n, c in pinned if c]
    if pinned:
        out.append(Check(NONE, "Recovery branches are still present", ", ".join(f"{n} ({c})" for n, c in pinned), f"suw project recovery {pinned[0][0]} --done   (after you resolved it)"))
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
        out.append(Check(PASS, f"{title} starts at login") if there else Check(WARN, f"{title} does not start at login", "it runs now but would not return after a reboot", "suw bootstrap"))
    return out


# ── Work folder ─────────────────────────────────────────────────────────────


def check_work(cfg: Config) -> list[Check]:
    from ..integrations import worksync

    report = worksync.status(cfg)
    state_word = report["state"]
    path = report["path"].replace(str(paths.home()), "~")
    if state_word == "DISABLED":
        return [Check(NONE, "Shared work folder is switched off", "", "suw config set work.enabled true && suw work setup")]
    if state_word == "NOT_INSTALLED":
        fix = "installers/macos/packages.sh" if paths.platform() == "macos" else "sudo installers/ubuntu/packages.sh"
        return [Check(WARN, "Shared work folder: Syncthing is not installed", path, fix)]
    if state_word == "NOT_CONFIGURED":
        return [Check(WARN, "Shared work folder is not set up", path, "suw work setup")]
    out = [Check(PASS, "Work folder", f"{path} · {report.get('files', 0)} files") if report["exists"] else Check(FAIL, "Work folder is missing", path, "suw work setup")]
    if state_word == "STOPPED":
        out.append(Check(FAIL, "Work sync service is not running", "", "suw work setup"))
        return out
    out.append(Check(PASS, "Work sync service", "private Syncthing instance, loopback API only"))
    if report.get("paused"):
        out.append(Check(WARN, "Work sync is paused", "", "suw work resume"))
    for error in report.get("errors", [])[:3]:
        out.append(Check(FAIL, "Work sync error", (f"{error['path']}: " if error["path"] else "") + error["error"][:160], "suw work status"))
    peers = report.get("peers", [])
    if not peers:
        waiting = report.get("unpaired", [])
        out.append(Check(NONE, "No other workstation is paired yet", "files stay on this machine until one is", f"suw work pair {waiting[0]}" if waiting else "run `suw bootstrap` on the other workstation, then: suw work pair"))
    for peer in peers:
        if peer["connected"]:
            pending = peer["need_items"] + report.get("need_items", 0)
            out.append(Check(PASS, f"Workstation '{peer['name']}' connected", "up to date" if not pending else f"{pending} item(s) in transit"))
        else:
            out.append(Check(NA, f"Workstation '{peer['name']}' is offline", "changes are queued and delivered when it returns"))
    for name in report.get("unpaired", []) if peers else []:
        out.append(Check(WARN, f"Workstation '{name}' is waiting to be paired", "", f"suw work pair {name}"))
    conflicts = report.get("conflicts", [])
    if conflicts:
        out.append(Check(WARN, f"{len(conflicts)} conflict cop{'y' if len(conflicts) == 1 else 'ies'} in the work folder", conflicts[0]["original"], "suw work conflicts"))
    held = [item for item in report.get("large", []) if item.get("held")]
    if held:
        out.append(Check(NONE, f"{len(held)} large file(s) are not synchronised", f"{held[0]['path']} ({held[0]['mb']} MB)", "suw work allow <path>   or   suw artifact push <path>"))
    for item in report.get("unsupported", [])[:3]:
        out.append(Check(WARN, "A file cannot be shared", f"{item['path']}: {item['reason']}", "rename or remove it"))
    detached = worksync.detached_repos(cfg) if report["exists"] else []
    if detached:
        out.append(Check(WARN, f"{len(detached)} project folder(s) arrived without their Git history", detached[0]["path"], "suw work repos --attach"))
    if paths.platform() == "macos" and (paths.home() / "Library/Mobile Documents/com~apple~CloudDocs/Desktop").exists() and "Desktop" in report["path"]:
        out.append(Check(WARN, "iCloud also syncs the Desktop folder", "two sync engines on one folder produce duplicates", "System Settings → Apple ID → iCloud Drive → turn off Desktop & Documents"))
    return out


# ── Peripherals ─────────────────────────────────────────────────────────────


def check_peripherals(cfg: Config) -> list[Check]:
    from ..integrations import peripherals

    report = peripherals.status(cfg)
    word = report["state"]
    if word == "DISABLED":
        return [Check(NONE, "Keyboard/mouse sharing is switched off", "", "suw config set peripherals.enabled true")]
    if word == "NOT_INSTALLED":
        return [Check(NONE, "Keyboard/mouse sharing: Deskflow is not installed", "each machine uses its own keyboard and mouse", "installers/*/packages.sh, then: suw peripherals setup")]
    if word == "NOT_CONFIGURED":
        return [Check(NONE, "Keyboard/mouse sharing is not set up", "each machine uses its own keyboard and mouse", "suw peripherals setup")]
    role = f"{report['role']}, screen name {report['screen']}"
    if word == "STOPPED":
        return [Check(WARN, "Keyboard/mouse sharing service is not running", role, "suw peripherals setup   (logs: journalctl --user -u suw-deskflow)")]
    out = [Check(PASS, "Deskflow core running", role)]
    if report["role"] == "server" and not report.get("listening"):
        out.append(Check(WARN, f"Deskflow is running but not listening on port {report['port']}", "the desktop's permission dialog may be waiting for an answer", "answer the dialog; logs: journalctl --user -u suw-deskflow"))
    for name in report.get("waiting", []):
        out.append(Check(NONE, f"Workstation '{name}' is waiting to be approved", "", f"suw peripherals pair {name}"))
    if word == "CONNECTED":
        out.append(Check(PASS, "Other workstation connected", "one keyboard, one mouse, shared clipboard"))
    elif word == "UNPAIRED":
        out.append(Check(NONE, "No other workstation is approved yet", "this machine keeps its own keyboard and mouse"))
    else:
        out.append(Check(NA, "Other workstation is not connected", "this machine keeps its own keyboard and mouse meanwhile"))
    if peripherals.exposed(cfg):
        out.append(Check(NONE, f"Deskflow listens on every network interface (port {report['port']})", "protected by TLS and approved fingerprints", 'suw config set peripherals.bind tailnet'))
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
            found = [Check(WARN, f"{section} checks could not run", str(exc)[:200], "suw history")]
        for item in found:
            item.section = section
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
