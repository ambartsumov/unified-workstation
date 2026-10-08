"""`suw work` — the shared work folder (same files on every workstation)."""

from __future__ import annotations

import json
import sys
import time
from collections import Counter

from ..core import config, events, inventory
from ..integrations import worksync
from ..ui import text
from ..ui.text import paint

GOOD = ("IN_SYNC", "NO_PEER", "PEER_OFFLINE", "SYNCING")
ACTIONS = ["status", "setup", "pair", "unpair", "conflicts", "resolve", "rescan", "pause", "resume", "doctor", "ignored", "versions", "restore", "repos", "allow", "id", "claude"]


def out(message: str = "") -> None:
    print(message)


def size(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit in ("B", "KB") else f"{count:.1f} {unit}"
        count /= 1024
    return ""


def symbol(state: str) -> str:
    if state == "IN_SYNC":
        return paint("✓", "ok")
    if state in ("ERROR", "STOPPED", "NOT_INSTALLED"):
        return paint("✗", "err")
    if state in ("NOT_CONFIGURED", "DISABLED"):
        return paint("○", "dim")
    return paint("⚠", "warn") if state in ("CONFLICTS", "PAUSED") else paint("●", "accent")


def render(report: dict) -> list[str]:
    state = report["state"]
    lines = [f"{symbol(state)} {state.replace('_', ' ')}   {paint(worksync.WORDS.get(state, ''), 'dim')}", f"Folder   {report['path']}" + ("" if report["exists"] else paint("   (missing)", "err"))]
    if "files" in report:
        lines.append(f"Shared   {report['files']} files, {size(report['bytes'])}" + (paint("   (scan cut short)", "dim") if report.get("partial") else ""))
    for peer in report.get("peers", []):
        if peer["connected"]:
            note = "up to date" if peer["need_items"] == 0 else f"{peer['need_items']} item(s) still to send ({peer['completion']:.0f}%)"
            lines.append(f"{paint('●', 'ok')} {peer['name']:<8} online, {note}")
        else:
            seen = peer.get("last_seen", "")[:16].replace("T", " ")
            lines.append(f"{paint('○', 'dim')} {peer['name']:<8} offline" + (f" (last seen {seen})" if seen and not seen.startswith("1970") else " (never connected)"))
    if report.get("need_items"):
        lines.append(f"Pending  {report['need_items']} item(s) to receive, {size(report.get('need_bytes', 0))}")
    if report.get("last_sync"):
        lines.append(f"Last in sync  {text.ago(report['last_sync'])}")
    for name in report.get("unpaired", []):
        lines.append(paint(f"⚠ workstation '{name}' is waiting to be paired: suw work pair {name}", "warn"))
    for error in report.get("errors", [])[:5]:
        lines.append(paint("✗ ", "err") + (f"{error['path']}: " if error["path"] else "") + error["error"])
    conflicts = report.get("conflicts", [])
    if conflicts:
        lines.append(paint(f"⚠ {len(conflicts)} conflict cop{'y' if len(conflicts) == 1 else 'ies'} — both versions are kept: suw work conflicts", "warn"))
    not_shared = []
    ignored = Counter(item["path"].rsplit("/", 1)[-1] for item in report.get("ignored", []))
    if ignored:
        not_shared.append("Not copied (by design): " + ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in ignored.most_common(6)) + ("…" if len(ignored) > 6 else "") + "   → suw work ignored")
    held = [item for item in report.get("large", []) if item.get("held")]
    if held:
        not_shared.append(f"Held back (larger than the limit): {len(held)} file(s), e.g. {held[0]['path']} ({held[0]['mb']} MB)   → suw work allow <path>")
    for item in report.get("unsupported", [])[:5]:
        not_shared.append(paint("⚠ ", "warn") + f"{item['path']}: {item['reason']}")
    if not_shared:
        lines += ["", *not_shared]
    hint = {
        "NOT_CONFIGURED": "Run: suw work setup",
        "NOT_INSTALLED": "Install Syncthing (installers/*/packages.sh), then: suw work setup",
        "STOPPED": "Run: suw work setup   (starts the service)",
        "PAUSED": "Run: suw work resume",
    }.get(state)
    if hint:
        lines += ["", hint]
    return lines


def _need_running(cfg) -> worksync.Instance | None:
    inst = worksync.instance(cfg)
    if not inst.running():
        print(paint("error: ", "err") + "the work sync service is not running (suw work setup)", file=sys.stderr)
        return None
    return inst


def setup(cfg, quiet: bool = False) -> tuple[bool, str]:
    """Create identity, folder, ignore rules and service. Safe to repeat."""
    from ..core import bootstrap

    title, note = bootstrap.step_work(cfg)
    ok = not note.startswith(("FAILED", "Syncthing is not installed"))
    if not quiet:
        out(f"{paint('✓', 'ok') if ok else paint('✗', 'err')} {title}: {note}")
    return ok, note


def cmd_pair(cfg, args) -> int:
    inv = inventory.load()
    if args.id:
        name = args.target or "peer"
        if not inventory.valid_label(name):
            print(f"error: '{name}' is not a valid device name", file=sys.stderr)
            return 1
        device_id = args.id.strip().upper()
        if len(device_id.replace("-", "")) != 56:
            print("error: that does not look like a Syncthing device ID (suw work id on the other workstation)", file=sys.stderr)
            return 1
        if args.address and not inventory.valid_host(args.address):
            print(f"error: '{args.address}' is not a valid host name", file=sys.stderr)
            return 1
        if not args.dry_run:
            inventory.ensure_device(inv, name, role="workstation", syncthing_id=device_id, host=args.address or "")
            inventory.save(inv)
    waiting = dict(worksync.unpaired(cfg, inv)) if not (args.id and args.dry_run) else {args.target or "peer": args.id}
    if args.target and args.target not in waiting:
        already = [p.name for p in worksync.peers(cfg, inv)]
        out(f"'{args.target}' is already paired." if args.target in already else f"No workstation named '{args.target}' has published a sync identity yet.")
        return 0 if args.target in already else 1
    if not waiting:
        mine = worksync.instance(cfg).device_id()
        out("No workstation is waiting to be paired.")
        out("On the other workstation run `suw bootstrap`; its identity arrives here through the shared configuration.")
        out(f"Or pair by hand there:  suw work pair {cfg.device} --id {mine or '<run suw work setup first>'}")
        return 0
    name = args.target or next(iter(waiting))
    device_id = waiting[name]
    base = worksync.root(cfg)
    out(paint(f"PAIR THIS WORK FOLDER WITH '{name}'".upper(), "bold"))
    out()
    out(f"Device   {name}  {device_id[:7]}…{device_id[-7:]}")
    mine = worksync.publish_manifest(cfg) if base.is_dir() else {"entries": {}, "files": 0, "bytes": 0}
    theirs = worksync.peer_manifest(name)
    out(f"Here     {mine['files']} files, {size(mine['bytes'])}   ({base})")
    risky = False
    if theirs is None:
        out("There    unknown — that workstation has not published a listing of its folder yet")
        out("         Joining never deletes anything: both folders are merged, and a file that")
        out("         exists on both sides with different content is kept twice (conflict copy).")
        risky = mine["files"] > 0
    else:
        result = worksync.compare(mine, theirs)
        out(f"There    {theirs['files']} files, {size(theirs['bytes'])}   (listing from {text.ago(theirs.get('generated'))})")
        out()
        out(f"  identical on both sides      {result['identical']}")
        out(f"  only here  → copied there    {result['only_here']}")
        out(f"  only there → copied here     {result['only_there']}")
        out(f"  different on both sides      {len(result['differing'])}   (both versions kept; nothing overwritten)")
        for path in result["differing"][:8]:
            out(f"      {path}")
        if len(result["differing"]) > 8:
            out(f"      … and {len(result['differing']) - 8} more")
        risky = bool(result["differing"]) or (mine["files"] > 0 and theirs["files"] > 0)
    out()
    if args.dry_run:
        out("Dry run: nothing was changed.")
        return 0
    if risky and not args.yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            out("Both sides already hold files. Re-run with --yes to join them, or --dry-run to look again.")
            return 2
        try:
            answer = input("Join the two folders? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            out("Nothing was changed.")
            return 1
    worksync.approve(device_id)
    worksync.refresh(cfg)
    events.emit("work.paired", f"work folder paired with {name}", device=name)
    out(f"{paint('✓', 'ok')} paired with {name}. It must approve this workstation too: there, run  suw work pair {cfg.device}")
    return 0


def cmd_work(args) -> int:
    cfg = config.load()
    action = args.action or "status"
    if action == "status":
        report = worksync.status(cfg)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            out(paint("WORK FOLDER", "bold"))
            out()
            for line in render(report):
                out(line)
        return 0 if report["state"] in GOOD else 1
    if action == "setup":
        if args.dry_run:
            out(f"Would create {worksync.root(cfg)} if missing, a private Syncthing identity in {worksync.home_dir()},")
            out("the managed block in .stignore and the user service. Nothing was changed.")
            return 0
        ok, _note = setup(cfg)
        return 0 if ok else 1
    if action == "id":
        device_id = worksync.instance(cfg).device_id()
        out(device_id or "not set up yet (suw work setup)")
        return 0 if device_id else 1
    if action == "pair":
        return cmd_pair(cfg, args)
    if action == "unpair":
        inv = inventory.load()
        device = inv["devices"].get(args.target or "", {})
        if not device.get("syncthing_id"):
            print(f"error: no paired workstation named '{args.target}'", file=sys.stderr)
            return 1
        worksync.revoke(device["syncthing_id"])
        worksync.refresh(cfg)
        events.emit("work.unpaired", f"work folder no longer shared with {args.target}", device=args.target)
        out(f"No longer sharing with {args.target}. Files already here stay here.")
        return 0
    if action == "doctor":
        from ..core import doctor

        checks = doctor.check_work(cfg)
        if args.json:
            print(json.dumps([c.as_dict() for c in checks], ensure_ascii=False, indent=1))
        else:
            for check in checks:
                out(f"{doctor.MARK[check.level]} {doctor.WORD[check.level]:<15} {check.title}" + (f"   {paint(check.detail, 'dim')}" if check.detail else ""))
                if check.fix and check.level != doctor.PASS:
                    out(f"    → {check.fix}")
        return 1 if any(c.level == doctor.FAIL for c in checks) else 0
    if action in ("conflicts", "ignored"):
        found = worksync.scan(cfg)
        rows = found.conflicts if action == "conflicts" else found.ignored + [{"path": i["path"], "reason": f"{i['mb']} MB, above work.large_file_mb"} for i in found.large if i["path"] in worksync.held_large(cfg, found)] + found.unsupported
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if action == "conflicts":
            if not rows:
                out(f"{paint('✓', 'ok')} No conflicts.")
                return 0
            out("The same file changed on two workstations before they could talk. Both versions exist:")
            out()
            for row in rows:
                out(f"  {row['original']}")
                out(paint(f"    other version: {row['path']}   ({time.strftime('%Y-%m-%d %H:%M', time.localtime(row['mtime']))})", "dim"))
            out()
            out("Compare them, then keep one:   suw work resolve '<other version>' --keep current|conflict")
            out("The version you do not keep is moved to the SUW trash, never deleted.")
            return 1
        if not rows:
            out("Everything in the folder is shared.")
            return 0
        width = min(60, max(len(r["path"]) for r in rows))
        for row in rows:
            out(f"  {row['path']:<{width}}  {paint(row['reason'], 'dim')}")
        out()
        out("Share one of these names anyway: suw config set work.sync_anyway '[\"venv\"]' --shared && suw work setup")
        return 0
    if action == "resolve":
        if not args.target or args.keep not in ("current", "conflict"):
            print("usage: suw work resolve <conflict-copy> --keep current|conflict", file=sys.stderr)
            return 1
        try:
            saved = worksync.resolve_conflict(cfg, args.target, args.keep)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        events.emit("work.conflict.resolved", f"conflict resolved: kept the {args.keep} version of {worksync.original_of(args.target)}")
        out(f"{paint('✓', 'ok')} kept the {args.keep} version. The other one is in {saved}")
        return 0
    if action == "claude":
        from ..integrations import claudestate

        words = (args.target or "").split(":", 1)  # `suw work claude` · `suw work claude share` · `suw work claude share:<folder>`
        try:
            if words[0] == "share":
                chosen = [claudestate.locate(cfg, words[1])] if len(words) > 1 else claudestate.projects(cfg)
                for project in chosen:
                    done = claudestate.share(project, dry=args.dry_run)
                    verb = "would keep" if args.dry_run else "keeps"
                    out(f"{paint('✓', 'ok')} {done['project']:<24} {verb} its Claude memory in {done['setting']}" + (f"   ({len(done['copied'])} note(s) copied in from this machine)" if done["copied"] else ""))
                if not args.dry_run:
                    events.emit("work.claude", f"Claude memory kept inside the work folder for {len(chosen)} project(s)")
                    out(paint("Claude Code asks once per machine whether to trust a project's settings; answer yes there.", "dim"))
                return 0
            rows = claudestate.status(cfg)
        except claudestate.ClaudeStateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        out("Follows you already: CLAUDE.md, .claude/ (settings, commands, agents, skills), .mcp.json")
        out("Claude's own notes (auto memory), per project:")
        for item in rows:
            if item["error"]:
                out(f"  {paint('⚠', 'warn')} {item['project']:<24} {item['error']}")
            elif item["shared"]:
                out(f"  {paint('●', 'ok')} {item['project']:<24} shared, {item['shared_files']} note(s)")
            else:
                out(f"  {paint('○', 'dim')} {item['project']:<24} this machine only" + (f", {item['local_files']} note(s)" if item["local_files"] else ""))
        if any(not item["shared"] and not item["error"] for item in rows):
            out()
            out("Make them follow you:  suw work claude share")
        out(paint("Stays on each machine by design: sign-in, and the session list of `claude --resume`.", "dim"))
        return 0
    if action in ("versions", "restore") and args.home:
        from ..integrations import replica

        try:
            if action == "restore":
                target = replica.restore(cfg, args.target or "")
                events.emit("work.restored", f"restored {args.target} from the home server")
                out(f"{paint('✓', 'ok')} restored to {target}")
                return 0
            rows = replica.versions(cfg, args.target or "")
        except replica.ReplicaError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if not rows:
            out("The home server holds no older versions yet.")
            return 0
        for row in rows[:200]:
            out(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['mtime']))}  {size(row['bytes']):>9}  {row['path']}")
        out()
        out(f"Bring one back: suw work restore --home '<path>'   (kept for {cfg.get('work.replica_days', 365)} days)")
        return 0
    if action == "versions":
        rows = worksync.versions(cfg, args.target or "")
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if not rows:
            out("No saved versions on this workstation.")
            out(paint("A file deleted HERE is kept by the OTHER workstation (run this there) and by Home: suw work versions --home", "dim"))
            return 0
        for row in rows[:200]:
            out(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['mtime']))}  {size(row['bytes']):>9}  {row['path']}")
        out()
        out(f"Bring one back: suw work restore <path>   (kept for {cfg.get('work.trash_days', 30)} days)")
        return 0
    if action == "restore":
        try:
            target = worksync.restore(cfg, args.target or "")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        events.emit("work.restored", f"restored {args.target} from the trash")
        out(f"{paint('✓', 'ok')} restored to {target}")
        return 0
    if action == "allow":
        if not args.target:
            print("usage: suw work allow <path-inside-the-work-folder>", file=sys.stderr)
            return 1
        allowed = sorted({*map(str, cfg.get("work.large_allow", [])), args.target})
        from ..core import tomlw

        local = config.read_toml(config.local_file())
        local.setdefault("work", {})["large_allow"] = allowed
        tomlw.dump(config.local_file(), local, "SUW configuration for this machine only.")
        worksync.refresh(config.load())
        out(f"{paint('✓', 'ok')} {args.target} will be synchronised despite its size.")
        return 0
    if action == "repos":
        found = worksync.scan(cfg)
        todo = worksync.detached_repos(cfg)
        if args.json:
            print(json.dumps({"repositories": found.repos, "detached": todo}, ensure_ascii=False, indent=1))
            return 0
        for rel in found.repos:
            out(f"{paint('✓', 'ok')} {rel}")
        for item in todo:
            if args.attach and not args.dry_run:
                ok, note = worksync.attach_repo(cfg, item["path"], item["url"], item["branch"])
                out(f"{paint('✓', 'ok') if ok else paint('✗', 'err')} {item['path']}: {note}")
            else:
                out(f"{paint('○', 'dim')} {item['path']}   files are here; Git history is not attached yet ({item['url']})")
        if todo and not args.attach:
            out()
            out("Attach the history (downloads it; no file is touched): suw work repos --attach")
        if not found.repos and not todo:
            out("No Git repositories inside the work folder.")
        return 0
    inst = _need_running(cfg)
    if inst is None:
        return 1
    if action == "rescan":
        worksync.refresh(cfg, rescan=False)
        ok = inst.rescan()
        out("Rescan requested." if ok else "Rescan could not be requested.")
        return 0 if ok else 1
    ok = inst.set_paused(action == "pause")
    if ok:
        events.emit(f"work.{'paused' if action == 'pause' else 'resumed'}", f"work folder sync {'paused' if action == 'pause' else 'resumed'}")
        out("Paused. Files stay where they are; nothing is sent or received until `suw work resume`." if action == "pause" else "Resumed.")
    return 0 if ok else 1


def add_parser(sub, flags) -> None:
    p = flags(sub.add_parser("work", help="the shared work folder: status, conflicts, pause, pair"), dry=True, yes=True)
    p.add_argument("action", nargs="?", choices=ACTIONS)
    p.add_argument("target", nargs="?")
    p.add_argument("--keep", choices=["current", "conflict"], help="resolve: which version stays under the original name")
    p.add_argument("--id", help="pair: Syncthing device ID of the other workstation (manual pairing)")
    p.add_argument("--address", help="pair --id: host name of the other workstation")
    p.add_argument("--home", action="store_true", help="versions/restore: use the copies kept on the home server")
    p.add_argument("--attach", action="store_true", help="repos: attach Git history to folders that arrived without it")
    p.set_defaults(func=cmd_work)
