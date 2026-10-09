"""`suw` — the canonical interface. Everything the UI does is available here.

Every major command accepts `--json`; commands that change the machine accept `--dry-run`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .. import __version__
from ..core import config, events, gitsync, health, inventory, modes, paths, projects, state, status, syncer, tomlw
from ..core.proc import run
from ..ui import text
from ..ui.text import paint


def out(message: str = "") -> None:
    print(message)


def emit_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1))


def err(message: str) -> int:
    print(paint("error: ", "err", text.color_enabled(sys.stderr)) + message, file=sys.stderr)
    return 1


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)
    return answer or default


def confirm(prompt: str, default: bool = False) -> bool:
    if not interactive():
        return False
    answer = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not answer else answer in ("y", "yes")


def heading(title: str) -> None:
    out(paint(title.upper(), "bold"))
    out()


# ── status / mode ───────────────────────────────────────────────────────────


def cmd_status(args) -> int:
    cfg = config.load()
    render = text.render_status if getattr(args, "panel", False) else text.render_summary
    if args.watch:
        try:
            while True:
                snap = status.build(config.load())
                sys.stdout.write("\033[2J\033[H" + text.render_status(snap) + "\n")
                sys.stdout.flush()
                time.sleep(5)
        except KeyboardInterrupt:
            return 0
    snap = status.build(cfg, live=args.live)
    if args.json:
        emit_json(snap)
    else:
        out(render(snap))
    return 0


def cmd_mode(args) -> int:
    cfg = config.load()
    action = args.action or "status"
    if action == "status":
        mode = modes.current()
        if args.json:
            emit_json({"mode": "WORKSTATION" if modes.is_work(mode) else "DEFAULT", "state": mode})
        else:
            out(f"{'WORKSTATION' if modes.is_work(mode) else 'DEFAULT'} MODE")
        return 0
    if action == "summary":
        from ..ui import exit_dialog

        summary = modes.leave_summary(cfg)
        if args.json:
            emit_json(summary)
        else:
            for label, value in exit_dialog.summary_lines(summary):
                out(f"{label:<36} {value}")
        return 0
    with modes.switching() as free:
        if not free or (action == "toggle" and modes.bounced()):
            if args.json:
                emit_json({"mode": "WORKSTATION" if modes.is_work() else "DEFAULT", "steps": [], "ignored": True})
            else:
                out("A mode switch is already in progress; nothing was done twice.")
            return 0
        return _switch_mode(cfg, action, args)


def _switch_mode(cfg, action: str, args) -> int:
    if action == "toggle" and args.ask and modes.is_work():
        action = "off"  # leaving from the keyboard goes through the same question as Super+W
    if action == "toggle":
        mode, steps = modes.toggle(cfg)
    elif action == "on":
        mode, steps = modes.WORKSTATION, modes.on(cfg, relaunch=args.relaunch)
    else:
        save, close = args.save or args.close_all, args.close_all
        if args.ask:
            from ..ui import exit_dialog

            choice = exit_dialog.ask(cfg, modes.leave_summary(cfg))
            if choice == "cancel":
                out("Cancelled; still in Workstation Mode.")
                return 1
            save, close = True, choice == "close"
        asked_at = time.time()
        mode, steps = modes.DEFAULT, modes.off(cfg, save=save, close=close)
        if close and paths.platform() == "linux":
            from ..integrations import gnome

            remaining = modes.close_report(asked_at) if gnome.available() and gnome.extension_state() == "active" else None
            if remaining:
                steps.append(f"still open (waiting for your answer about unsaved work): {', '.join(t for t in remaining if t)[:160]}")
    if args.json:
        emit_json({"mode": mode, "steps": steps})
        return 0
    out(paint("● WORKSTATION MODE", "accent") if mode == modes.WORKSTATION else paint("○ DEFAULT MODE", "dim"))
    for step in steps:
        out(f"  {step}")
    return 0


# ── sync / projects ─────────────────────────────────────────────────────────


def _print_rows(rows: list[dict], details: bool = True) -> None:
    if not rows:
        out(paint("No repositories are managed yet.", "dim"))
        out("Put a repository under ~/Projects/active, or run: suw project add <path>")
        return
    width = max(len(r["name"]) for r in rows)
    for row in rows:
        out(f"{row['name']:<{width}}  {text.sync_label(row['status'])}  {paint(row.get('branch') or '–', 'dim')}  {text.project_line(row)}")
        if details and row.get("detail"):
            out(paint(f"{'':<{width}}  {row['detail']}", "dim"))


def _rows(cfg) -> list[dict]:
    from ..daemon import client

    runtime = state.load("runtime")
    rows = runtime.get("projects") if client.running() else None
    return rows if rows is not None else syncer.local_rows(cfg)


def cmd_sync(args) -> int:
    from ..daemon import client

    cfg = config.load()
    if args.action == "status":
        rows = _rows(cfg)
        if args.json:
            emit_json({"summary": syncer.counts(rows), "projects": rows})
        else:
            _print_rows(rows)
        return 0
    command = "retry" if args.action == "retry" else "sync"
    reply = None if args.checkpoint else client.call(command, timeout=200)
    if reply and reply.get("ok"):
        rows = reply["projects"]
    else:
        rows = syncer.sync_all(cfg, do_fetch=True, force_checkpoint=args.checkpoint)
    if args.json:
        emit_json({"summary": syncer.counts(rows), "projects": rows})
    else:
        _print_rows(rows)
    attention = {s.value for s in gitsync.ATTENTION}
    return 1 if any(r["status"] in attention for r in rows) else 0


def _project_recovery(cfg, args, target: Path) -> int:
    name = target.name
    if args.merge or args.rebase:
        how = "rebase" if args.rebase else "merge"
        if not args.yes and not confirm(f"Run a {how} of the remote branch into {name}? It is aborted automatically on conflict"):
            out("Cancelled; nothing was changed.")
            return 1
        gitsync.fetch(target)
        outcome = gitsync.manual_reconcile(target, how)
        out(f"{name}: {outcome.detail}")
        if outcome.changed:
            memo = state.load("sync_memo")
            memo.pop(str(target), None)
            state.save("sync_memo", memo)
            out("Run `suw sync` to push the result.")
            return 0
        return 1
    if args.branch:
        branch = gitsync.create_recovery_branch(target, cfg.device)
        out(f"{name}: your side is pinned at {branch}" if branch else f"{name}: could not create a recovery branch")
        return 0 if branch else 1
    if args.done:
        removed, kept = [], []
        for branch in gitsync.recovery_branches(target):
            # `-d` (never `-D`): git itself refuses unless the branch is fully merged.
            (removed if gitsync.git(target, "branch", "-d", branch).ok else kept).append(branch)
        out(f"{name}: removed {len(removed)} resolved recovery branch(es)" + (f"; kept {len(kept)} that are not merged yet" if kept else ""))
        return 0
    info = gitsync.recovery_info(target)
    if args.json:
        emit_json(info)
        return 0
    heading(f"recovery · {name}")
    if info["error"]:
        out(paint(f"✗ {info['error']}", "err"))
        return 1
    out(f"Local HEAD        {info['local_head']}  ({info['branch'] or 'detached'})")
    out(f"Remote HEAD       {info['remote_head'] or '–'}  ({info['upstream'] or 'no upstream'})")
    out(f"Last synced HEAD  {info['last_synced_head'] or '–'}")
    out()
    out(f"Unpushed commits ({len(info['unpushed'])})")
    for line in info["unpushed"][:10]:
        out(f"  {line}")
    out(f"Incoming commits ({len(info['incoming'])})")
    for line in info["incoming"][:10]:
        out(f"  {line}")
    out(f"Uncommitted files ({len(info['uncommitted'])})")
    for line in info["uncommitted"][:10]:
        out(f"  {line}")
    if info["recovery_branches"]:
        out("Recovery branches")
        for line in info["recovery_branches"]:
            out(f"  {line}")
    out()
    out("No data was deleted.")
    out()
    out("Actions:")
    out(f"  1. Inspect                 git -C {projects.tilde(target)} log --oneline --graph --all -n 20")
    out(f"  2. Create recovery branch  suw project recovery {name} --branch")
    out(f"  3. Rebase                  suw project recovery {name} --rebase")
    out(f"  4. Merge                   suw project recovery {name} --merge")
    out("  5. Abort                   do nothing; both sides stay exactly as they are")
    out(f"  After resolving            suw project recovery {name} --done")
    return 0


def cmd_project(args) -> int:
    cfg = config.load()
    action = args.action or "status"
    words = list(args.args or [])

    if action == "list":
        rows = []
        for path, policy in syncer.managed(cfg):
            if path == paths.shared_dir():
                continue
            rows.append({"name": path.name, "path": str(path), "autosync": bool(policy["enabled"]), "policy": policy["policy"]})
        if args.json:
            emit_json(rows)
            return 0
        if not rows:
            out(paint("No projects are managed yet.", "dim") + " Put a repository under ~/Projects/active, or: suw project add <path>")
        for row in rows:
            out(f"{row['name']:<28} {'autosync on' if row['autosync'] else 'autosync off':<13} {projects.tilde(Path(row['path']))}")
        return 0

    if action == "scan":
        found = projects.scan(cfg)
        if args.json:
            emit_json(found)
            return 0
        out(f"Searched: {', '.join(projects.tilde(Path(r)) for r in found['roots'])}")
        out(f"Managed:  {len(found['managed'])}")
        if not found["unmanaged"]:
            out(paint("Nothing else found. SUW never looks outside these folders.", "dim"))
            return 0
        out()
        out(f"{len(found['unmanaged'])} Git repositories found outside SUW management.")
        for path in found["unmanaged"]:
            out(f"  {projects.tilde(Path(path))}")
        out()
        if not args.add or args.dry_run:
            out("Use:")
            out("  suw project add <path>        (one)")
            out("  suw project scan --add        (all of the above, after confirmation)")
            return 0
        if not args.yes and not confirm(f"Let SUW manage these {len(found['unmanaged'])} repositories (automatic checkpoints follow each project's .suw.toml)?"):
            out("Cancelled; nothing was changed.")
            return 1
        for path in found["unmanaged"]:
            projects.add(config.load(), Path(path))
            out(f"added: {path}")
        return 0

    if action in ("add", "remove"):
        if not words:
            return err(f"usage: suw project {action} <path>")
        path = projects.resolve(cfg, words[0]) if action == "remove" else Path(words[0]).expanduser().resolve()
        if path is None or (action == "add" and not (path / ".git").exists()):
            return err(f"{words[0]} is not a git repository")
        if args.dry_run:
            out(f"Would {'start' if action == 'add' else 'stop'} managing {path}. Nothing was changed.")
            return 0
        changed = projects.add(cfg, path) if action == "add" else projects.remove(cfg, path)
        events.emit("project." + ("added" if action == "add" else "removed"), f"{path.name}: {'now' if action == 'add' else 'no longer'} managed", project=path.name)
        if action == "add":
            policy = gitsync.project_policy(path, config.load().data)
            out(f"{'added' if changed else 'already managed'}: {path}")
            out(f"automatic checkpoints: {'on' if policy['enabled'] else 'off'}   (change: suw project autosync on|off {path.name})")
        else:
            out(f"{'removed from SUW management' if changed else 'was not managed'}: {path}")
            out(paint("The repository itself was not touched.", "dim"))
        return 0

    if action == "autosync":
        if not words or words[0] not in ("on", "off"):
            return err("usage: suw project autosync on|off [project]")
        repo = projects.resolve(cfg, words[1]) if len(words) > 1 else projects.containing()
        if repo is None:
            return err("not inside a git repository (or name a project: suw project autosync on <name>)")
        file = repo / ".suw.toml"
        data = config.read_toml(file)
        data.setdefault("project", {}).setdefault("name", repo.name)
        data.setdefault("autosync", {})["enabled"] = words[0] == "on"
        tomlw.dump(file, data, mode=0o644)
        out(f"{repo.name}: automatic checkpoints {words[0]}  ({file.name})")
        return 0

    if action == "allow-once":
        from ..core import policy as filepolicy

        repo = projects.containing()
        if repo is None or not words:
            return err("usage (inside the repository): suw project allow-once <file>")
        rel = os.path.relpath(Path(words[0]).resolve(), repo)
        if not filepolicy.allow_once(repo, rel):
            return err(f"{words[0]} is not a file in this repository")
        events.emit("sync.allowed", f"{repo.name}: {rel} allowed once past the secret scan", "warn", project=repo.name)
        out(f"{rel}: its current content will be accepted by the next checkpoint (once).")
        out(paint("To always skip the scan for a path: add it to [autosync] secret_ignore in .suw.toml", "dim"))
        return 0

    if action == "restore":
        wanted = projects.missing(cfg)
        if args.json:
            emit_json([{"name": n, "remote": r, "path": str(p)} for n, r, p in wanted])
            return 0
        if not wanted:
            out("Every project in the shared list is already on this machine.")
            return 0
        out(f"{len(wanted)} project(s) from the shared list are not on this machine:")
        for name, remote, dest in wanted:
            out(f"  {name:<28} {remote}  →  {projects.tilde(dest)}")
        if args.dry_run:
            return 0
        if not args.yes and not confirm("Clone them now?", default=True):
            return 1
        failed = 0
        for name, remote, dest in wanted:
            res = run(["git", "clone", "--quiet", "--", remote, str(dest)], timeout=1800, env=gitsync.GIT_ENV)
            failed += 0 if res.ok else 1
            out(f"{paint('✓', 'ok') if res.ok else paint('✗', 'err')} {name}" + ("" if res.ok else paint(f"  {(res.err.strip().splitlines() or ['failed'])[-1]}", "dim")))
        return 1 if failed else 0

    target = projects.resolve(cfg, words[0] if words else None)
    if action == "recovery":
        if target is None:
            return err("usage: suw project recovery <project>")
        return _project_recovery(cfg, args, target)

    if target is None:
        if args.json:
            emit_json({})
        else:
            out(paint("No project here.", "dim") + " cd into a repository or add one with `suw project add <path>`.")
        return 0
    projects.remember(target)
    managed = {str(p): pol for p, pol in syncer.managed(cfg)}
    policy = managed.get(str(target)) or gitsync.project_policy(target, cfg.data)
    row = next((r for r in _rows(cfg) if r["path"] == str(target)), None) or syncer.local_row(target, policy)
    row = {**row, "managed": str(target) in managed, "policy": policy["policy"], "autosync": bool(policy["enabled"])}
    if args.json:
        emit_json(row)
        return 0
    heading("project")
    out(paint(row["name"], "bold"))
    out(f"path:     {row['path']}")
    out(f"branch:   {row['branch'] or '–'}" + (f"  → {row['upstream']}" if row.get("upstream") else "  (no upstream)"))
    out(f"state:    {text.sync_label(row['status'])} • {text.project_line(row)}")
    if row.get("detail"):
        out(f"          {paint(row['detail'], 'dim')}")
    out(f"sync:     {'managed by suwd' if row['managed'] else 'not managed (suw project add .)'}")
    out(f"autosync: {'on' if row['autosync'] else 'off'}   diverged history: {row['policy']}")
    for item in row.get("held") or []:
        out(paint(f"held back: {item['path']} — {item['detail']}", "warn"))
    if row.get("held"):
        out("          No data was deleted. [Inspect] the file · [Allow once] suw project allow-once <file> · [Ignore] add to .gitignore")
    if row["status"] in ("DIVERGED", "CONFLICT", "RECOVERY_REQUIRED"):
        out(f"next:     suw project recovery {row['name']}")
    return 0


def cmd_code(args) -> int:
    from ..core.proc import spawn
    from ..integrations import apps

    cfg = config.load()
    exe = apps.editor(cfg)
    if not exe:
        return err("no editor found (set one: suw config set workstation.editor <command>)")
    target = Path(args.path).expanduser() if args.path else projects.active(cfg)
    if target is None:
        return err("no project to open")
    projects.remember(Path(target).resolve())
    spawn([exe, str(target)])
    out(f"{exe}: {target}")
    return 0


def cmd_browser(args) -> int:
    from ..integrations import browser

    if args.session == "default":
        out("Default session: no tabs are forced.")
        return 0
    ok, note = browser.open_session(config.load(), "workstation", force=not args.if_missing)
    out(note)
    return 0 if ok else 1


# ── home ────────────────────────────────────────────────────────────────────


def trust_host(alias: str, host: str, port: int = 22) -> int:
    """Explicit, human-confirmed host key trust. Never automatic."""
    from ..cloud import CloudError, scan_host_key

    try:
        key, fingerprint = scan_host_key(host, port)
    except CloudError as exc:
        return err(str(exc))
    known = paths.ssh_dir() / "known_hosts"
    lines = known.read_text(errors="replace").splitlines() if known.exists() else []
    same_key = [ln.split()[0] for ln in lines if key.split()[1] in ln]
    if any(ln.split()[0] == host and key.split()[1] in ln for ln in lines if ln.strip()):
        out(f"{alias}: already trusted ({fingerprint})")
        return 0
    plain = [name for name in same_key if not name.startswith("|")]
    if same_key:
        # Not a new trust decision: this exact key was already verified by the user under
        # another name of the same machine. Only the name is added.
        from ..core import journal

        journal.backup_file(known)
        with known.open("a") as handle:
            handle.write(f"{host} {key}\n")
        events.emit("ssh.trust", f"{alias}: known key bound to {host}", fingerprint=fingerprint)
        out(f"{alias}: trusted ({fingerprint}) - the same key you already trust" + (f" as {', '.join(plain[:3])}" if plain else ""))
        return 0
    out(paint("UNKNOWN HOST", "warn"))
    out()
    out(f"Host:         {host}")
    out(f"Fingerprint:  {paint(fingerprint, 'bold')}")
    out()
    if same_key and not same_key[0].startswith("|"):
        out(f"This is the same key you already trust as: {', '.join(same_key[:3])}")
    else:
        out("Compare it with the fingerprint shown on the server itself (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub).")
    if not interactive() or ask("[Trust] / [Cancel] - type 'trust' to continue", "cancel").lower() != "trust":
        out("Not trusted; nothing was changed.")
        return 1
    from ..core import journal

    journal.backup_file(known)
    with known.open("a") as handle:
        handle.write(f"{host} {key}\n")
    known.chmod(0o600)
    events.emit("ssh.trust", f"host key trusted for {alias}", fingerprint=fingerprint)
    out(f"{alias}: trusted.")
    return 0


def cmd_link(args) -> int:
    """Stay connected to one machine: `ssh <name>`, started again whenever the link drops.
    This is what the windows of the servers terminal run."""
    import subprocess

    name = args.name
    inv = inventory.load()
    known = [n for n, d in inv["devices"].items() if inventory.device_host(d) and n != config.load().device] + (["cloud"] if inventory.cloud_current(inv)[1] else [])
    if name not in known:
        return err(f"'{name}' is not a machine SUW knows an address for" + (f" (known: {', '.join(sorted(known))})" if known else ""))
    delay = 5
    while True:
        out(paint(f"◌ {name}: connecting…", "dim"))
        started = time.monotonic()
        try:
            code = subprocess.call(["ssh", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", name])
        except KeyboardInterrupt:
            code = 130
        if code == 0:  # you logged out yourself: do not jump straight back in
            out(paint(f"○ {name}: session closed.", "dim") + "  Enter = connect again · Ctrl-C = local shell")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                out()
                return 0
            delay = 5
            continue
        if time.monotonic() - started > 60:
            delay = 5  # it was a working session that dropped, not a machine that refuses
        out(paint(f"⚠ {name}: not connected", "warn") + paint(f" (ssh exit {code}); next attempt in {delay} s · Ctrl-C = local shell", "dim"))
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            out()
            return 0
        delay = min(60, delay * 2)


def cmd_home(args) -> int:
    from ..integrations import ssh as sshcfg
    from ..integrations import tailscale

    cfg = config.load()
    inv = inventory.load()
    home = inv["devices"].get("home", {})
    action = args.action or "status"
    if action in ("set", "set-endpoint"):
        if not args.target:
            peers = [p for p in tailscale.status().get("peers", []) if p["os"] == "linux"]
            out("usage: suw home set-endpoint <tailscale-name-or-ip> [--user <ssh-user>]")
            if peers:
                out("Linux machines on your tailnet: " + ", ".join(p["name"] for p in peers))
                out(paint("SUW never guesses which of them is your home server.", "dim"))
            return 1
        peer = tailscale.find(args.target)
        host = peer["dns"] if peer else args.target
        if not inventory.valid_host(host):
            return err(f"'{host}' is not a valid host name")
        user = args.user or home.get("ssh_user") or os.environ.get("USER", "")
        if not inventory.valid_user(user):
            return err(f"'{user}' is not a valid SSH username")
        identity = args.identity or home.get("ssh_identity", "")
        if identity and (identity.startswith("-") or any(c in identity for c in " \n\t\"'") or not paths.expand(identity).is_file()):
            return err(f"'{identity}' is not a readable key file")
        if args.dry_run:
            out(f"Would set home → {user}@{host} and re-render ~/.ssh/suw.conf. Nothing was changed.")
            return 0
        inventory.ensure_device(inv, "home", role="home", tailscale_name=host, ssh_user=user, ssh_identity=identity)
        inventory.save(inv)
        sshcfg.apply(inv, cfg.device)
        events.emit("home.set", f"home server set to {host}")
        out(f"home → {user}@{host}" + ("" if peer else paint("  (not found on the tailnet; using the name as given — reported as degraded)", "warn")))
        out("Next: suw home trust   then   ssh home")
        return 0
    host = inventory.device_host(home)
    if action == "endpoint":
        out(f"home → {home.get('ssh_user', '')}@{host}" + (f":{home['ssh_port']}" if home.get("ssh_port") else "") if host else "not configured: suw home set-endpoint <name-or-ip>")
        out(paint("Change it in one place: suw home set-endpoint <name-or-ip>   (`ssh home` keeps working)", "dim"))
        return 0 if host else 1
    if not host:
        if args.json:
            emit_json({"status": health.NOT_CONFIGURED})
            return 0
        out(paint("○ NOT CONFIGURED", "dim") + "   Home server is not configured.")
        out("Run: suw home set <tailscale-name> --user <ssh-user>")
        return 1
    if action == "trust":
        return trust_host("home", host, int(home.get("ssh_port", 22)))
    if action == "credentials":
        from . import cloud_cmd

        return cloud_cmd.credentials("home", args.forget, str(home.get("ssh_identity", "")))
    if action == "replica":
        from ..integrations import replica, worksync

        sub = args.target or "status"
        try:
            if sub == "setup":
                if args.dry_run:
                    out(f"Would create a receive-only copy of the work folder in {replica.folder(cfg)} on the home server,")
                    out(f"keeping replaced and deleted files for {cfg.get('work.replica_days', 365)} days. Nothing was changed.")
                    return 0
                done = replica.setup(cfg, install=getattr(args, "install", False))
                events.emit("home.replica", "home keeps a copy of the work folder", folder=done["folder"])
                out(f"{paint('✓', 'ok')} Home keeps a copy of the work folder in {done['folder']}")
                out(f"  receives from: {', '.join(done['peers'])}   versions kept: {cfg.get('work.replica_days', 365)} days")
                if done["linger"] != "yes":
                    out(paint("  ⚠ the copy stops when nobody is logged in on the server. There, once: sudo loginctl enable-linger " + str(home.get("ssh_user", "")), "warn"))
                if not done["private"]:
                    out(paint("  ⚠ the server has no Tailscale address, so the copy listens on every network interface (only approved devices are accepted).", "warn"))
                out("  progress: suw work status")
                return 0
            if sub == "remove":
                if args.dry_run or not (args.yes or confirm("Stop keeping a copy on the home server? (files already there are kept)")):
                    out("Nothing was changed.")
                    return 0 if args.dry_run else 1
                replica.remove(cfg)
                events.emit("home.replica", "home no longer keeps a copy of the work folder")
                out("Stopped. The files on the home server were left in place.")
                return 0
            report = replica.status(cfg)
        except replica.ReplicaError as exc:
            return err(str(exc))
        if args.json:
            emit_json(report)
            return 0 if report["state"] == "ONLINE" else 1
        if report["state"] == "NOT_CONFIGURED":
            out(paint("○ NOT CONFIGURED", "dim") + "   Home does not keep a copy of the work folder yet.")
            out("Run: suw home replica setup")
            return 1
        peer = next((p for p in worksync.status(cfg, deep=False).get("peers", []) if p["name"] == "home"), None)
        out(f"{paint('●', 'ok') if report['state'] == 'ONLINE' else paint('⚠', 'warn')} {report['state']}" + (paint(f"   {report.get('error', '')}", "dim") if report.get("error") else ""))
        if peer:
            out(f"Copy      {'complete' if peer['connected'] and peer['need_items'] == 0 else ('receiving, ' + format(peer['completion'], '.0f') + '%' if peer['connected'] else 'not connected to this workstation right now')}")
        if report["state"] == "ONLINE":
            out(f"Versions  {report['versions']} older file version(s) kept   free space {report['free_bytes'] / 1073741824:.1f} GB")
            if report["linger"] != "yes":
                out(paint("⚠ runs only while someone is logged in on the server (sudo loginctl enable-linger " + str(home.get("ssh_user", "")) + ")", "warn"))
        return 0 if report["state"] == "ONLINE" else 1
    if action in ("shell", "ssh", "connect"):
        # Persistent session: re-attach to tmux on the server when it is there.
        os.execvp("ssh", ["ssh", "-t", "home", "command -v tmux >/dev/null 2>&1 && exec tmux new-session -A -s main || exec $SHELL -l"])
    services = list(cfg.get("home.services", []))
    if action == "doctor":
        from ..core import doctor

        checks = doctor.check_home(cfg)
        if args.json:
            emit_json([c.as_dict() for c in checks])
        else:
            for check in checks:
                out(f"{doctor.MARK[check.level]} {doctor.WORD[check.level]:<15} {check.title}" + (paint(f"   {check.detail}", "dim") if check.detail else ""))
                if check.fix and check.level != doctor.PASS:
                    out(f"    → {check.fix}")
        return 1 if any(c.level == doctor.FAIL for c in checks) else 0
    if action == "logs":
        unit = args.target or (services[0] if services else "")
        if not unit or not health._SERVICE.match(unit):
            return err("usage: suw home logs <service>")
        remote = f"journalctl -u {unit} -n 80 --no-pager 2>/dev/null || journalctl --user -u {unit} -n 80 --no-pager 2>/dev/null || docker logs --tail 80 {unit}"
        os.execvp("ssh", ["ssh", "home", remote])
    report = health.probe_remote("home", services)
    state_word = health.server_status(True, report)
    if args.json:
        emit_json({"status": state_word, "host": host, **{k: v for k, v in report.items() if k != "checked"}})
        return 0 if report.get("online") else 1
    if action == "services":
        if not services:
            out(paint("No services configured.", "dim") + ' Add them: suw config set home.services \'["nginx","my-bot"]\' --shared')
            return 0
        for unit, st in report.get("services", {}).items():
            out(f"{text.dot(st == 'active')} {unit:<24} {st}")
        return 0 if report.get("online") else 1
    heading("home server")
    if not report.get("online"):
        out(f"{text.mark(state_word)} {text.word(state_word)}   {paint(report.get('error', ''), 'dim')}")
        out()
        if state_word == health.UNTRUSTED:
            out("The server's identity is not trusted" + (" and CHANGED since you trusted it. Connection blocked." if health.identity_changed(report) else " yet."))
            out("Run: suw home trust")
        elif state_word == health.AUTH_REQUIRED:
            out("Your SSH key is not accepted. Run: ssh-copy-id home")
        else:
            out("Your local environment is unaffected.")
        return 1
    out(f"{text.mark(state_word)} {text.word(state_word)}   {paint(str(report.get('os', '')), 'dim')}")
    uptime = report.get("uptime_s")
    if isinstance(uptime, (int, float)):
        out(f"Uptime {int(uptime // 86400)}d {int(uptime % 86400 // 3600)}h")
    out(f"CPU {text.pct(report.get('cpu_pct'))}")
    out(f"RAM {text.pct(report.get('ram_used_pct'))}")
    out(f"Disk {text.pct(report.get('disk_used_pct'))}")
    if report.get("services"):
        out()
        out("Services")
        for unit, st in report["services"].items():
            out(f"{text.dot(st == 'active')} {unit}" + ("" if st == "active" else f"  {st}"))
    return 0


# ── artifacts ───────────────────────────────────────────────────────────────


def cmd_peripherals(args) -> int:
    from ..core import bootstrap, doctor, inventory
    from ..integrations import peripherals

    cfg = config.load()
    action = args.action or "status"
    if action == "run":
        # the service entry point: refresh the profile, then become the Deskflow core
        if not peripherals.enabled(cfg):
            return 0
        start = peripherals.command(cfg)
        if not start:
            print("error: Deskflow is not installed", file=sys.stderr)
            return peripherals.TEMPFAIL
        address = peripherals.bind_address(cfg) if peripherals.role(cfg) == "server" else ""
        if address is None:
            print("waiting: the private network (Tailscale) is not up yet; set peripherals.bind = \"all\" to share over the LAN", file=sys.stderr)
            return peripherals.TEMPFAIL
        peripherals.prepare(cfg, interface=address)
        if not peripherals.fingerprint():
            print("error: no TLS certificate (suw peripherals setup)", file=sys.stderr)
            return peripherals.TEMPFAIL
        if peripherals.role(cfg) == "client" and not peripherals.server_host(cfg):
            print("waiting: the server workstation is not in the inventory yet", file=sys.stderr)
            return peripherals.TEMPFAIL
        os.execvp(start[0], start)
    if action == "setup":
        if args.dry_run:
            out("Would write the Deskflow profile in ~/.config/suw/deskflow and install the sharing service. Nothing was changed.")
            return 0
        title, note = bootstrap.step_peripherals(cfg)
        out(f"{title}: {note}")
        if not peripherals.core():
            out()
            if paths.platform() == "macos":
                out("Install Deskflow first:  brew install --cask deskflow   (or installers/macos/packages.sh)")
            else:
                out("Install Deskflow first (needs your password once):  installers/ubuntu/packages.sh")
            return 1
        out()
        out(f"This workstation: {peripherals.role(cfg)} · screen name {peripherals.screen_name(cfg.device)}")
        out(f"Fingerprint       {peripherals.pretty(peripherals.fingerprint())}")
        for line in peripherals.permissions():
            out(line)
        return 1 if note.startswith("FAILED") else 0
    if action in ("pair", "unpair"):
        inv = inventory.load()
        if action == "unpair":
            known = dict(peripherals.offered(cfg, inv))
            name = args.target or next(iter(known), "")
            if name not in known:
                out("No such workstation is paired.")
                return 1
            if not args.dry_run:
                peripherals.revoke(known[name])
                peripherals.prepare(cfg)
                peripherals.service("restart")
            out(f"'{name}' can no longer share the keyboard and mouse with this machine.")
            return 0
        if args.fingerprint:
            name, fp = args.target or "peer", args.fingerprint.replace(":", "").strip().lower()
            if not inventory.valid_label(name) or len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
                print("error: expected a device name and a SHA-256 fingerprint (suw peripherals id on the other workstation)", file=sys.stderr)
                return 1
            if not args.dry_run:
                inventory.ensure_device(inv, name, role="workstation", deskflow_fp=fp)
                inventory.save(inv)
        waiting = dict(peripherals.unpaired(cfg, inv))
        if args.target and args.target not in waiting:
            done = args.target in dict(peripherals.offered(cfg, inv))
            out(f"'{args.target}' is already approved." if done else f"No workstation named '{args.target}' has published a fingerprint yet.")
            return 0 if done else 1
        if not waiting:
            out("No workstation is waiting to be approved.")
            out("On the other workstation run `suw bootstrap`; its fingerprint arrives here through the shared configuration.")
            return 0
        name = args.target or next(iter(waiting))
        out(f"Workstation '{name}' asks to share keyboard, mouse and clipboard with this machine.")
        out(f"Its fingerprint:  {peripherals.pretty(waiting[name])}")
        out(f"Compare it with the output of `suw peripherals id` on '{name}'.")
        if args.dry_run:
            out("Dry run: nothing was changed.")
            return 0
        if not (args.yes or confirm("Do the fingerprints match?")):
            out("Nothing was changed.")
            return 1
        peripherals.approve(waiting[name])
        peripherals.prepare(cfg)
        peripherals.service("restart")
        out(f"{paint('✓', 'ok')} '{name}' approved. Approve this machine there too: suw peripherals pair {cfg.device}")
        return 0
    if action == "id":
        fp = peripherals.fingerprint()
        out(peripherals.pretty(fp) if fp else "not set up yet: suw peripherals setup")
        return 0 if fp else 1
    if action == "doctor":
        checks = doctor.check_peripherals(cfg)
        if args.json:
            emit_json([c.as_dict() for c in checks])
        else:
            for check in checks:
                out(f"{doctor.MARK[check.level]} {doctor.WORD[check.level]:<15} {check.title}" + (paint(f"   {check.detail}", "dim") if check.detail else ""))
                if check.fix and check.level != doctor.PASS:
                    out(f"    → {check.fix}")
        return 1 if any(c.level == doctor.FAIL for c in checks) else 0
    if action == "layout":
        inv = inventory.load()
        server = str(cfg.get("peripherals.server", "ubuntu"))
        out(peripherals.layout(server, [n for n in peripherals.workstations(inv) if n != server], str(cfg.get("peripherals.peer_side", "right")), bool(cfg.get("peripherals.clipboard", True))))
        return 0
    report = peripherals.status(cfg)
    if args.json:
        emit_json(report)
    else:
        heading("keyboard · mouse · clipboard")
        symbol = {"CONNECTED": paint("●", "ok"), "STOPPED": paint("⚠", "warn")}.get(report["state"], paint("○", "dim"))
        out(f"{symbol} {report['state'].replace('_', ' ')}   {paint(peripherals.WORDS[report['state']], 'dim')}")
        out(f"Role     {report['role']}   screen name {report['screen']}")
        if report["role"] == "client":
            out(f"Server   {report['server'] or 'not known yet'}")
        for name in report.get("waiting", []):
            out(f"Waiting  '{name}' asks to be approved: suw peripherals pair {name}")
        if report["state"] in ("NOT_INSTALLED", "NOT_CONFIGURED", "STOPPED"):
            out()
            out("Run: suw peripherals setup")
    return 0 if report["state"] in ("CONNECTED", "WAITING", "UNPAIRED", "DISABLED") else 1


def cmd_artifact(args) -> int:
    from ..core import artifacts
    from ..core.artifacts import ArtifactError

    cfg = config.load()
    action = args.action or "list"
    words = list(args.args or [])

    def project_name() -> str:
        if args.project:
            return args.project
        here = projects.containing()
        if here is None:
            raise ArtifactError("which project? run inside a repository or pass --project <name>")
        return here.name

    try:
        store = artifacts.from_config(cfg)
        if action == "list":
            items = store.list(args.project or None)
            if args.json:
                emit_json({"store": store.describe(), "artifacts": [a.as_dict() for a in items]})
                return 0
            out(paint(f"store: {store.describe()}", "dim"))
            if not items:
                out("No artifacts yet. Add one: suw artifact put <file-or-directory>")
            for a in items:
                out(f"{a.project:<22} {a.name:<34} {a.size / 1048576:>9.1f} MB  {a.created[:10]}  {a.sha256[:12]}  {a.retention}")
            return 0
        if action == "put":
            if not words:
                return err("usage: suw artifact put <file-or-directory> [--project p] [--name n] [--retention keep|30d]")
            meta = dict(item.split("=", 1) for item in (args.meta or []) if "=" in item)
            if args.dry_run:
                out(f"Would store {words[0]} as {project_name()}/{args.name or Path(words[0]).name} in {store.describe()}. Nothing was changed.")
                return 0
            item = store.put(Path(words[0]).expanduser(), project_name(), args.name, retention=args.retention, metadata=meta)
            events.emit("artifact.put", f"{item.project}/{item.name} stored ({item.size} bytes)", project=item.project, status="ok")
            if args.json:
                emit_json(item.as_dict())
            else:
                out(f"{paint('✓', 'ok')} {item.project}/{item.name}  {item.size / 1048576:.1f} MB  sha256 {item.sha256[:16]}…  → {store.describe()}")
            return 0
        if action == "get":
            if not words:
                return err("usage: suw artifact get <name> [--project p] [-o destination]")
            dest = store.get(project_name(), words[0], Path(args.output or "."))
            events.emit("artifact.get", f"{project_name()}/{words[0]} fetched", project=project_name(), status="ok")
            out(f"{paint('✓', 'ok')} {dest}  (checksum verified)")
            return 0
        if action == "stat":
            if not words:
                return err("usage: suw artifact stat <name> [--project p]")
            item = store.stat(project_name(), words[0])
            if item is None:
                return err(f"no artifact '{words[0]}' in project '{project_name()}'")
            emit_json(item.as_dict())
            return 0
        if action == "verify":
            results = store.verify(args.project or None, words[0] if words else None)
            if args.json:
                emit_json([{**a.as_dict(), "intact": ok} for a, ok in results])
            else:
                for a, ok in results:
                    out(f"{paint('✓', 'ok') if ok else paint('✗', 'err')} {a.project}/{a.name}" + ("" if ok else "  checksum differs or the file is gone"))
                if not results:
                    out("Nothing to verify.")
            return 0 if all(ok for _, ok in results) else 1
        if action == "rm":
            if not words:
                return err("usage: suw artifact rm <name> [--project p]")
            if args.dry_run:
                out(f"Would delete {project_name()}/{words[0]} from {store.describe()}. Nothing was changed.")
                return 0
            if not args.yes and not confirm(f"Delete {project_name()}/{words[0]} from {store.describe()}? This cannot be undone"):
                out("Cancelled.")
                return 1
            gone = store.delete(project_name(), words[0])
            if gone:
                events.emit("artifact.deleted", f"{project_name()}/{words[0]} deleted", "warn", project=project_name())
            out("deleted" if gone else "no such artifact")
            return 0 if gone else 1
    except ArtifactError as exc:
        return err(str(exc))
    return err(f"unknown action '{action}'")


# ── devices / clipboard ─────────────────────────────────────────────────────


def cmd_device(args) -> int:
    from ..core import bootstrap

    cfg = config.load()
    action = args.action or "list"
    if action == "id":
        out(str(cfg.get("device.id", "")) or "(not enrolled: suw device enroll)")
        return 0
    if action == "enroll":
        if args.dry_run:
            out(f"Would register this machine as workstation '{cfg.device}' in the shared inventory and render the SSH aliases. Nothing was changed.")
            return 0
        heading("device enrollment")
        for title, outcome in bootstrap.run_all(["dirs", "config", "ssh"]):
            out(f"{paint('✓', 'ok')} {title:<28} {paint(outcome, 'dim')}")
        cfg = config.load()
        inv = inventory.load()
        out()
        out(f"Logical identity   {cfg.device}  ({cfg.get('device.id', '')})")
        out(f"Shared config      {paths.shared_dir()}" + ("" if gitsync.remote_url(paths.shared_dir()) else paint("  (no remote yet: this machine only)", "warn")))
        out(f"SSH roles          {', '.join(sorted(n for n, d in inv['devices'].items() if n != cfg.device and inventory.device_host(d))) or '–'}" + ("  cloud" if inventory.cloud_current(inv)[1] else ""))
        wanted = projects.missing(cfg)
        out(f"Projects           {len(projects.discover(cfg))} here, {len(wanted)} more in the shared list" + ("  → suw project restore" if wanted else ""))
        out(paint("Secrets stay local: sign in to your Git host on this machine (e.g. `gh auth login`).", "dim"))
        events.emit("device.enrolled", f"{cfg.device} enrolled", device_id=cfg.get("device.id", ""))
        return 0
    snap = status.build(cfg)
    if args.json:
        emit_json(snap["workstations"])
        return 0
    for ws in snap["workstations"]:
        out(text.workstation_line(ws) + (paint(f"   {ws['id']}", "dim") if ws.get("id") else ""))
    return 0


def cmd_clipboard(args) -> int:
    from ..integrations import clipboard

    cfg = config.load()
    if (args.action or "status") == "status":
        caps = clipboard.capabilities(cfg)
        last = state.load("clipboard_test")
        if args.json:
            emit_json({**caps, "last_test": last})
            return 0
        out(f"{text.mark(caps['local'])} Local clipboard")
        out(f"{text.mark(caps['ssh'])} SSH (OSC 52)   terminal: {caps['terminal'] or 'none'}   tmux configured: {'yes' if caps['tmux'] else 'no'}")
        out(f"{text.mark(caps['peer'])} Workstation ↔ workstation (Deskflow)   see docs/clipboard.md")
        if last:
            out(paint(f"last test: {'passed' if last.get('ok') else 'failed'} ({last.get('via')}, {last.get('terminal') or 'unknown terminal'}) {last.get('detail', '')}", "dim"))
        else:
            out(paint("not proven yet: run `suw clipboard test` inside your terminal", "dim"))
        return 0
    if args.window:
        from ..integrations import apps

        result = clipboard.window_test(apps.suw_cmd(), args.via or "", args.repeat if args.repeat > 1 else 3)
    else:
        result = clipboard.repeated(args.via or "", args.repeat)
    result["when"] = time.time()
    state.save("clipboard_test", result)
    events.emit("clipboard.test", f"clipboard test ({result['via']}): {result['detail']}", "info" if result["ok"] else "warn", status="ok" if result["ok"] else "failed")
    if args.report:
        Path(args.report).write_text(json.dumps(result, indent=1))
    if args.json:
        emit_json(result)
    else:
        runs = f"  ({result['passed']}/{result['runs']} runs)" if result.get("runs", 1) > 1 else ""
        out(f"{paint('✓ PASS', 'ok') if result['ok'] else paint('✗ FAIL', 'err')}  {result['detail']}{runs}")
        out(paint(f"path: {result['via']} → {'tmux → ' if result['tmux'] else ''}{result['terminal'] or 'terminal'} → system clipboard", "dim"))
    return 0 if result["ok"] else 1


# ── diagnostics / history / config ──────────────────────────────────────────


def cmd_doctor(args) -> int:
    from ..core import doctor

    cfg = config.load()
    results = doctor.run_all(cfg)
    verdict = doctor.verdict(results)
    code = 1 if verdict == "NOT READY" else 0
    if args.json:
        emit_json({"overall": verdict, "checks": [c.as_dict() for c in results], "notes": doctor.notes(results)})
        return code
    heading(f"{cfg.get('brand.short', 'SUW')} doctor")
    styles = {doctor.PASS: "ok", doctor.WARN: "warn", doctor.FAIL: "err", doctor.NONE: "dim", doctor.NA: "dim"}
    for section in doctor.SECTIONS:
        items = [c for c in results if c.section == section]
        if not items:
            continue
        out(paint(section, "bold"))
        for check in items:
            out(f"  {paint(doctor.MARK[check.level], styles[check.level])} {check.title}" + (paint(f"  {check.detail}", "dim") if check.detail else ""))
            if check.fix and check.level != doctor.PASS:
                out(f"      → {check.fix}")
        out()
    out("Overall: " + paint(verdict, {"READY": "ok", "NOT READY": "err"}.get(verdict, "warn")))
    notes = doctor.notes(results)
    if notes:
        out()
        out("Notes:")
        for index, note in enumerate(notes, 1):
            out(f"{index}. {note}")
    out()
    out(paint("✓ PASS   ⚠ WARNING   ✗ FAIL   ○ NOT CONFIGURED   – NOT TESTABLE", "dim"))
    return code


def cmd_history(args) -> int:
    kinds: tuple[str, ...] = ()
    if args.sync:
        kinds += ("sync.",)
    if args.cloud:
        kinds += ("cloud.",)
    if args.home:
        kinds += ("home.",)
    records = events.read(args.lines, args.grep, args.level, kinds=kinds, project=args.project)
    if args.json:
        emit_json(records)
        return 0
    for record in records:
        out(events.format_record(record))
    if not records:
        out(paint("nothing recorded yet", "dim"))
    return 0


def cmd_logs(args) -> int:
    if args.follow:
        for record in events.read(args.lines, args.grep):
            out(events.format_record(record))
        try:
            for record in events.follow():
                if not args.grep or args.grep.lower() in json.dumps(record).lower():
                    out(events.format_record(record))
        except KeyboardInterrupt:
            return 0
    records = events.read(args.lines, args.grep, args.level)
    if args.json:
        for record in records:
            out(json.dumps(record, ensure_ascii=False))
    else:
        for record in records:
            out(events.format_record(record))
        if not records:
            out(paint("no events", "dim"))
    return 0


def cmd_config(args) -> int:
    cfg = config.load()
    action = args.action or "show"
    scope = "shared" if args.shared else "local"
    files = {"local": config.local_file(), "shared": config.shared_file(), "urls": config.urls_file()}
    if action == "path":
        for name, path in files.items():
            out(f"{name:<9} {path}")
        out(f"{'inventory':<9} {inventory.file()}")
        out(f"{'state':<9} {paths.state_dir()}")
        return 0
    if action == "show":
        if args.json:
            emit_json(cfg.data)
        else:
            out(tomlw.dumps(cfg.data))
        return 0
    if action == "validate":
        errors, warnings = config.validate(cfg.data)
        errors = [*cfg.warnings, *errors]
        if args.json:
            emit_json({"valid": not errors, "errors": errors, "warnings": warnings})
            return 1 if errors else 0
        for line in errors:
            out(f"{paint('✗', 'err')} {line}")
        for line in warnings:
            out(f"{paint('⚠', 'warn')} {line}")
        out(paint("✓ configuration is valid", "ok") if not errors else paint(f"{len(errors)} error(s); the last good version stays in use", "err"))
        return 1 if errors else 0
    if action == "diff":
        changes = config.diff()
        if args.json:
            emit_json([{"key": k, "default": d, "value": v, "source": s} for k, d, v, s in changes])
            return 0
        if not changes:
            out("No differences from the shipped defaults.")
        for key, default, value, source in changes:
            out(f"{key}  {paint(f'({source})', 'dim')}")
            out(paint(f"  - {json.dumps(default, ensure_ascii=False)}", "err"))
            out(paint(f"  + {json.dumps(value, ensure_ascii=False)}", "ok"))
        return 0
    if action == "get":
        value = cfg.get(args.key or "")
        if value is None:
            return 1
        out(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        return 0
    if action == "set":
        if not args.key or args.value is None:
            return err("usage: suw config set <key> <value> [--shared] [--dry-run]")
        if args.key.startswith("urls."):
            scope = "urls"
        value = config.parse_cli_value(args.value)
        problems = config.check_set(args.key, value, scope)  # parse → validate → (dry-run) → apply
        if problems:
            return err("; ".join(problems) + " — nothing was changed")
        if args.dry_run:
            out(f"Would set {args.key} = {json.dumps(value, ensure_ascii=False)} in {files[scope]}. Nothing was changed.")
            return 0
        path = config.set_value(args.key, value, scope)
        events.emit("config", f"{args.key} updated ({scope})")
        out(f"{args.key} saved to {path}")
        return 0
    if action == "edit":
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        os.execvp(editor.split()[0], [*editor.split(), str(files[scope])])
    return err(f"unknown action '{action}'")


def cmd_secret(args) -> int:
    from ..core import secrets

    if args.action == "backend":
        out(secrets.backend())
        return 0
    if not args.name:
        return err(f"usage: suw secret {args.action} <name>")
    if args.action == "set":
        import getpass

        value = getpass.getpass(f"Value for '{args.name}' (hidden): ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        if not value:
            return err("empty value; nothing stored")
        try:
            secrets.put(args.name, value)
        except secrets.SecretError as exc:
            return err(str(exc))
        out(f"stored in {secrets.backend()} as keychain://{args.name}")
        return 0
    if args.action == "get":
        value = secrets.get(args.name)
        if value is None:
            return 1
        out(value)
        return 0
    return 0 if secrets.delete(args.name) else 1


# ── lifecycle ───────────────────────────────────────────────────────────────


def cmd_bootstrap(args) -> int:
    from ..core import bootstrap, doctor

    target = args.target or "auto"
    here = "macos" if paths.platform() == "macos" else "ubuntu"
    if target not in ("auto", "new-device", here):
        return err(f"this machine is {here}; run `suw bootstrap {here}` (or just `suw bootstrap`)")
    if getattr(args, "dry_run", False):
        preview = bootstrap.preview(config.load())
        out("Files:")
        for line in preview["files"]:
            out(f"  {line}")
        out()
        out("Changes:")
        for line in preview["changes"]:
            out(f"  + {line}")
        out()
        out(paint("Nothing was changed. Every change is journaled; undo with `suw rollback`.", "dim"))
        return 0
    known = [step.__name__.removeprefix("step_") for step in bootstrap.STEPS]
    unknown = [name for name in (args.only or []) if name not in known]
    if unknown:
        return err(f"unknown bootstrap step: {', '.join(unknown)}   (steps: {', '.join(known)})")
    heading(f"bootstrap · {here}")
    for title, outcome in bootstrap.run_all(args.only):
        failed = outcome.startswith("FAILED") or "NOT bound" in outcome
        mark = paint("✕", "err") if failed else paint("✓", "ok")
        out(f"{mark} {title:<28} {paint(outcome, 'err' if failed else 'dim')}")
    out()
    verdict = doctor.verdict(doctor.run_all(config.load()))
    out(f"Done. {verdict}   (details: suw doctor)")
    return 0


def cmd_rollback(args) -> int:
    from ..core import journal

    lines = journal.rollback(dry_run=True)
    if not lines:
        out("Nothing to roll back.")
        return 0
    for line in lines:
        out(f"  {line}")
    if args.dry_run:
        out(paint("Dry run: nothing was changed.", "dim"))
        return 0
    if not args.yes and not confirm(f"Undo these {len(lines)} SUW-managed change(s)?"):
        out("Cancelled.")
        return 1
    journal.rollback()
    out("Rolled back. Your own files, packages and projects were not touched.")
    return 0


def cmd_uninstall(args) -> int:
    from ..core import bootstrap, journal

    preview = journal.rollback(dry_run=True)
    out("The following SUW-owned resources will be removed:")
    for line in preview:
        out(f"  {line}")
    out("  stop                 the suwd background service")
    if args.purge:
        out(f"  remove               {paths.config_dir()} and {paths.state_dir()}")
    out()
    out("The following user resources will remain:")
    out(f"  your projects and every Git repository ({len(projects.discover(config.load()))} managed)")
    out("  the rest of your shell, tmux, git and SSH configuration (only the marked SUW blocks go)")
    out("  installed packages, SSH keys, and secrets in the system keychain")
    out(f"  the SUW repository itself ({paths.REPO_ROOT})")
    if not args.purge:
        out(f"  SUW configuration and history ({paths.config_dir()}, {paths.state_dir()}) — add --purge to remove")
    if args.dry_run:
        out()
        out(paint("Dry run: nothing was changed.", "dim"))
        return 0
    if not args.yes and not confirm("Uninstall?"):
        out("Cancelled.")
        return 1
    bootstrap.uninstall(purge=args.purge)
    out("Uninstalled.")
    return 0


def cmd_daemon(args) -> int:
    from ..daemon import client

    action = args.action or "status"
    if action == "run":
        from ..daemon import suwd

        return suwd.main()
    if action == "status":
        reply = client.call("ping", timeout=2)
        if reply and reply.get("ok"):
            out(f"{text.dot(True)} suwd {reply['version']} running (pid {reply['pid']}, up {syncer.age(time.time() - reply['uptime']).replace(' ago', '')})")
            return 0
        out(f"{text.dot(False)} suwd not running")
        return 1
    from ..core import supervise

    if supervise.needed():
        if action in ("stop", "restart"):
            supervise.stop("suwd", forget=action == "stop")
        if action in ("start", "restart") and not supervise.start("suwd", [str(paths.launcher()), "daemon", "run"]):
            return err("the background service could not be started")
        out(f"{action}: ok")
        return 0
    if paths.platform() == "macos":
        target = f"gui/{os.getuid()}/io.github.ambartsumov.suwd"
        cmd = {"start": ["launchctl", "kickstart", target], "restart": ["launchctl", "kickstart", "-k", target], "stop": ["launchctl", "kill", "TERM", target]}[action]
    else:
        cmd = ["systemctl", "--user", action, "suwd.service"]
    res = run(cmd, timeout=20)
    out(res.text or f"{action}: ok")
    return res.rc


def cmd_update(args) -> int:
    repo = paths.REPO_ROOT
    if not gitsync.git(repo, "remote").out.strip():
        out("This checkout has no remote; nothing to update from.")
        return 0
    net = gitsync.fetch(repo)
    if net.state != "ok":
        return err("cannot reach the remote" + (f": {net.detail}" if net.detail else ""))
    current = gitsync.collect(repo)
    if not current.behind:
        out(f"suw {__version__} is up to date.")
        return 0
    log = gitsync.git(repo, "log", "--oneline", "HEAD..@{upstream}").out.strip()
    out(f"{current.behind} update(s) available:\n{log}")
    if args.check:
        return 0
    if not current.clean:
        return err("the SUW checkout has local changes; commit or stash them first")
    res = gitsync.git(repo, "merge", "--ff-only", "--quiet", "@{upstream}")
    if not res.ok:
        return err(res.text)
    events.emit("update", f"suw updated ({current.behind} commit(s))")
    out("Updated. Re-applying configuration…")
    return cmd_bootstrap(argparse.Namespace(target="auto", only=None, dry_run=False))


def cmd_ui(args) -> int:
    try:
        from ..ui import control_center
    except Exception as exc:
        out(text.render_status(status.build(config.load())))
        print(paint(f"\n(graphical Control Center unavailable: {exc})", "dim"), file=sys.stderr)
        return 0
    return control_center.main()


def cmd_handoff(args) -> int:
    from ..core import handoff

    path = handoff.build(config.load(), Path(args.output).expanduser() if args.output else None)
    out(f"Handoff bundle: {path}")
    out("Send it to the new machine (AirDrop, USB, or `tailscale file cp`), then follow docs/mac-day-one.md.")
    return 0


def cmd_app(args) -> int:
    """Open the application window (the normal way to use the product)."""
    from ..app import launch

    return launch.run(demo=args.demo, port=args.port, window=args.window)


def cmd_start(args) -> int:
    """Start background components that should be running (used by start-at-sign-in)."""
    from ..core import migrate, supervise

    migrate.run()
    started = supervise.restore() if supervise.needed() else []
    if not args.quiet:
        out("started: " + ", ".join(started) if started else "background components are running")
    return 0


def cmd_capabilities(args) -> int:
    from ..core import capabilities

    data = capabilities.matrix(config.load())
    if args.json:
        emit_json(data)
        return 0
    info = data["platform"]
    heading(f"capabilities — {info['os']} {info['os_version']} {info['desktop']} {info['session']}".strip())
    for feature in data["features"]:
        out(f"  {feature['status']:<22} {feature['id']:<20} {feature['reason']}")
    out()
    for part in data["components"]:
        out(f"  {'installed' if part['installed'] else 'missing':<10} {part['name']:<20} {part['version']}")
    return 0


def cmd_support(args) -> int:
    from ..core import support

    target = Path(args.output) if args.output else Path.cwd() / support.filename()
    target.write_bytes(support.build())
    out(f"Support bundle written: {target}")
    out("It contains no work files, passwords, keys or tokens; names and addresses are placeholders. Review it before sharing.")
    return 0


def cmd_pair(args) -> int:
    """Pair with another workstation using pairing codes (scriptable form of the window's flow)."""
    from ..core import pairing

    cfg = config.load()
    if args.action == "code":
        offer = pairing.make_offer(cfg)
        if args.json:
            emit_json({"code": pairing.encode(offer), "offer": offer.as_dict()})
        else:
            out(pairing.encode(offer))
        return 0
    if not args.code:
        return err("usage: suw pair accept <code> [--confirm <number>]")
    try:
        theirs = pairing.decode(args.code)
    except pairing.PairingError as exc:
        return err(str(exc))
    review = pairing.review(cfg, theirs)
    if args.json and args.action == "review":
        emit_json(review)
        return 0
    out(f"Computer: {theirs.name} ({theirs.platform or 'unknown system'}) — {theirs.files} files there, {review['mine']['files']} here")
    out(review["merge_note"])
    out(f"Confirmation number: {review['confirmation']}   (the other computer must show the same)")
    for problem in review["problems"]:
        out(f"  ! {problem}")
    if args.action == "review" or review["problems"]:
        return 1 if review["problems"] else 0
    if args.confirm != review["confirmation"]:
        if not (interactive() and ask("Type the number shown on the other computer", "") == review["confirmation"]):
            return err("the confirmation number does not match; nothing was changed")
    done = pairing.accept(cfg, theirs)
    out(f"Paired with {done['device']}. Enter this computer's code there as well:  suw pair code")
    return 0


def cmd_version(args) -> int:
    out(f"suw {__version__}")
    return 0


# ── parser ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="suw", description="Unified Workstation control plane.")
    parser.add_argument("--version", action="version", version=f"suw {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def flags(p, *, json_out: bool = True, dry: bool = False, yes: bool = False):
        if json_out:
            p.add_argument("--json", action="store_true", help="machine-readable output")
        if dry:
            p.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
        if yes:
            p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
        return p

    p = flags(sub.add_parser("status", help="can I work right now, and is everything healthy?"))
    p.add_argument("--watch", action="store_true", help="live panel, refreshed every 5 s")
    p.add_argument("--panel", action="store_true", help="the compact Control Center panel")
    p.add_argument("--live", action="store_true", help="probe servers now instead of using the daemon cache")
    p.set_defaults(func=cmd_status)

    p = flags(sub.add_parser("doctor", help="diagnose the workstation, with fixes"))
    p.set_defaults(func=cmd_doctor)

    p = flags(sub.add_parser("mode", help="show or switch Default / Workstation mode"))
    p.add_argument("action", nargs="?", choices=["on", "off", "toggle", "status", "summary"])
    p.add_argument("--relaunch", action="store_true", help="reopen the workstation apps")
    p.add_argument("--ask", action="store_true", help="off / toggle: when leaving, show what is in flight and ask: Save & Close, or Cancel")
    p.add_argument("--save", action="store_true", help="off: checkpoint and push every managed project first")
    p.add_argument("--close-all", action="store_true", help="off: save, then close the workstation windows")
    p.set_defaults(func=cmd_mode)

    p = flags(sub.add_parser("project", help="projects: list, add, remove, status, scan, autosync, recovery"), dry=True, yes=True)
    p.add_argument("action", nargs="?", choices=["status", "list", "add", "remove", "scan", "autosync", "recovery", "allow-once", "restore"])
    p.add_argument("args", nargs="*")
    p.add_argument("--add", action="store_true", help="scan: enroll what was found (asks first)")
    p.add_argument("--merge", action="store_true", help="recovery: merge the remote branch (aborted on conflict)")
    p.add_argument("--rebase", action="store_true", help="recovery: rebase onto the remote branch (aborted on conflict)")
    p.add_argument("--branch", action="store_true", help="recovery: pin the local side on a recovery branch")
    p.add_argument("--done", action="store_true", help="recovery: remove recovery branches that are fully merged")
    p.set_defaults(func=cmd_project)

    p = flags(sub.add_parser("sync", help="reconcile all managed repositories now"))
    p.add_argument("action", nargs="?", choices=["status", "retry"])
    p.add_argument("--checkpoint", action="store_true", help="also commit pending work in autosync projects right now")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("link", help="stay connected to a machine (reconnects by itself)")
    p.add_argument("name", help="home, mac, cloud …")
    p.set_defaults(func=cmd_link)

    p = flags(sub.add_parser("home", help="the always-on home server"), dry=True, yes=True)
    p.add_argument("action", nargs="?", choices=["status", "doctor", "connect", "shell", "ssh", "services", "logs", "set", "set-endpoint", "endpoint", "trust", "credentials", "replica"])
    p.add_argument("target", nargs="?", help="set-endpoint: name or address · replica: setup|status|remove · logs: service")
    p.add_argument("--forget", action="store_true", help="credentials: remove the stored password")
    p.add_argument("--install", action="store_true", help="replica setup: put the official Syncthing release into the server user's home if the server has none (no administrator rights)")
    p.add_argument("--user")
    p.add_argument("--identity", help="set: path of the SSH key file to use for this server")
    p.set_defaults(func=cmd_home)

    p = flags(sub.add_parser("peripherals", help="one keyboard, mouse and clipboard across the workstations"), dry=True, yes=True)
    p.add_argument("action", nargs="?", choices=["status", "setup", "pair", "unpair", "id", "doctor", "layout", "run"])
    p.add_argument("target", nargs="?", help="pair/unpair: the other workstation's name")
    p.add_argument("--fingerprint", help="pair: the other workstation's fingerprint, when it is not in the shared configuration yet")
    p.set_defaults(func=cmd_peripherals)

    from . import cloud_cmd, work_cmd

    work_cmd.add_parser(sub, flags)

    cloud_cmd.add_parser(sub)

    p = flags(sub.add_parser("artifact", help="large files that do not belong in Git"), dry=True, yes=True)
    p.add_argument("action", nargs="?", choices=["list", "put", "get", "stat", "verify", "rm"])
    p.add_argument("args", nargs="*")
    p.add_argument("--project")
    p.add_argument("--name")
    p.add_argument("--retention", default="keep")
    p.add_argument("--meta", action="append", help="key=value (repeatable)")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_artifact)

    p = flags(sub.add_parser("device", help="workstations: list, enroll this one"), dry=True)
    p.add_argument("action", nargs="?", choices=["list", "enroll", "id"])
    p.set_defaults(func=cmd_device)

    p = flags(sub.add_parser("clipboard", help="prove that copy over SSH reaches this machine"))
    p.add_argument("action", nargs="?", choices=["status", "test"])
    p.add_argument("--via", help="run the test from a server: home | cloud")
    p.add_argument("--repeat", type=int, default=1, help="test: run this many times; every run must pass")
    p.add_argument("--window", action="store_true", help="test: run in a WezTerm window of its own (works from a script; focus-independent)")
    p.add_argument("--report", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_clipboard)

    p = flags(sub.add_parser("history", help="what happened? (readable event history)"))
    p.add_argument("-n", "--lines", type=int, default=30)
    p.add_argument("--project")
    p.add_argument("--sync", action="store_true")
    p.add_argument("--cloud", action="store_true")
    p.add_argument("--home", action="store_true")
    p.add_argument("--grep")
    p.add_argument("--level", choices=["info", "warn", "error"])
    p.set_defaults(func=cmd_history)

    p = flags(sub.add_parser("logs", help="raw event log (see also: suw history)"))
    p.add_argument("-n", "--lines", type=int, default=30)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--grep")
    p.add_argument("--level", choices=["info", "warn", "error"])
    p.set_defaults(func=cmd_logs)

    p = flags(sub.add_parser("config", help="show, validate or change configuration"), dry=True)
    p.add_argument("action", nargs="?", choices=["show", "get", "set", "path", "edit", "validate", "diff"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--shared", action="store_true", help="store for every workstation instead of this machine")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("secret", help="values in the OS keychain (never in config)")
    p.add_argument("action", choices=["set", "get", "rm", "backend"])
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_secret)

    p = sub.add_parser("code", help="open the active project in the editor")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_code)

    p = sub.add_parser("browser", help="open a named browser session")
    p.add_argument("session", nargs="?", default="workstation", choices=["workstation", "default"])
    p.add_argument("--if-missing", action="store_true", help="do nothing when the session is already open")
    p.set_defaults(func=cmd_browser)

    p = flags(sub.add_parser("bootstrap", help="set this machine up (idempotent, resumable)"), json_out=False, dry=True)
    p.add_argument("target", nargs="?", choices=["auto", "ubuntu", "macos", "new-device"])
    p.add_argument("--only", action="append", help="run a single step (repeatable)")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("update", help="update SUW itself (controlled, logged)")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_update)

    p = flags(sub.add_parser("rollback", help="undo SUW-managed changes to this machine"), json_out=False, dry=True, yes=True)
    p.set_defaults(func=cmd_rollback)

    p = flags(sub.add_parser("uninstall", help="stop services and undo everything SUW changed"), json_out=False, dry=True, yes=True)
    p.add_argument("--purge", action="store_true", help="also delete SUW config and state")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("daemon", help="control the background daemon")
    p.add_argument("action", nargs="?", choices=["status", "start", "stop", "restart", "run"])
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("ui", help="open the Control Center")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("handoff", help="bundle everything a new workstation needs")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("app", help="open the application window")
    p.add_argument("--demo", action="store_true", help="sample data only; touches nothing on this computer")
    p.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--window", choices=["auto", "webview", "browser", "none"], default="auto", help="how to show the window (none: print the address)")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("start", help="start background components (used at sign-in)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_start)

    p = flags(sub.add_parser("capabilities", help="what this computer supports, detected"))
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("support", help="write a redacted support bundle")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_support)

    p = flags(sub.add_parser("pair", help="pair with another workstation using a pairing code"))
    p.add_argument("action", choices=["code", "review", "accept"])
    p.add_argument("code", nargs="?")
    p.add_argument("--confirm", default="")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("version", help="print the version")
    p.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_status(argparse.Namespace(json=False, watch=False, live=False, panel=False))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
