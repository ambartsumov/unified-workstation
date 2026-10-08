"""`suw cloud …` — paste the address once; afterwards it is just `ssh cloud`."""

from __future__ import annotations

import getpass
import json
import os

from .. import cloud
from ..cloud import CloudError
from ..core import config, events, health, inventory, paths, secrets
from ..core.proc import have, run
from ..ui import text
from ..ui.text import paint

AUTHKEY = "tailscale_authkey"
ACTIONS = ["status", "add", "replace", "set-endpoint", "remove", "bootstrap", "shell", "ssh", "backup", "save", "history", "verify", "credentials", "project"]


def add_parser(sub) -> None:
    p = sub.add_parser("cloud", help="the disposable compute role")
    p.add_argument("action", nargs="?", choices=ACTIONS)
    p.add_argument("words", nargs="*", help="replace/set-endpoint: the new address · project: list|push|status|pull|remove [name]")
    p.add_argument("--host", help="public IP or hostname of the new machine")
    p.add_argument("--with-git", action="store_true", help="project push: also send the .git directory")
    p.add_argument("--with-claude", action="store_true", help="project push: also send the project's .claude directory")
    p.add_argument("--mirror", action="store_true", help="project push: also delete remote files that no longer exist here")
    p.add_argument("--forget", action="store_true", help="credentials: remove the stored password")
    p.add_argument("--user", help="SSH username")
    p.add_argument("--port", type=int)
    p.add_argument("--provider", help="provider label (free text, e.g. vast, lambda, hetzner)")
    p.add_argument("--gpu", help="GPU you rented (optional; the real hardware is detected)")
    p.add_argument("--role", default="compute")
    p.add_argument("--fingerprint", help="expected SHA256 host key fingerprint (from the provider console)")
    p.add_argument("--profile", choices=[*cloud.PROFILES, "none"], help="bootstrap profile")
    p.add_argument("--docker", action="store_true", help="also install Docker during bootstrap")
    p.add_argument("--no-tailscale", action="store_true", help="skip tailnet enrollment; use the public endpoint")
    p.add_argument("--force", action="store_true", help="override the data-loss guard on removal")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    p.add_argument("--json", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="assume yes for non-destructive prompts")
    p.set_defaults(func=run_cloud)


def _main():
    from . import main

    return main


def run_cloud(args) -> int:
    action = args.action or "status"
    if action in ("add", "replace", "set-endpoint") and args.words and not args.host:
        args.host = args.words[0]  # `suw cloud set-endpoint 203.0.113.7`
    try:
        return {
            "status": cmd_status,
            "add": cmd_add,
            "replace": cmd_replace,
            "set-endpoint": cmd_replace,
            "credentials": cmd_credentials,
            "project": cmd_project,
            "remove": cmd_remove,
            "bootstrap": cmd_bootstrap,
            "shell": cmd_shell,
            "ssh": cmd_shell,
            "backup": cmd_backup,
            "save": cmd_backup,
            "history": cmd_history,
            "verify": cmd_verify,
        }[action](args)
    except CloudError as exc:
        events.emit("cloud.error", str(exc), "error", status="error")
        return _main().err(str(exc))


def _current() -> tuple[str, dict]:
    label, node = inventory.cloud_current(inventory.load())
    if node is None:
        raise CloudError("no cloud machine is enrolled. Run: suw cloud add")
    return label, node


def _hardware_block(hw: dict) -> list[str]:
    lines = []
    if hw.get("gpu"):
        lines.append(f"GPU    {hw['gpu']} {hw.get('gpu_vram_gb', '?')}GB")
    lines.append(f"CPU    {hw.get('cores', '?')} vCPU")
    lines.append(f"RAM    {hw.get('ram_gb', '?')}GB")
    if hw.get("disk_total_gb"):
        lines.append(f"Disk   {hw['disk_total_gb']}GB")
    if hw.get("gpu"):
        lines.append(f"CUDA   {'available (' + str(hw['cuda']) + ')' if hw.get('cuda') else 'not detected'}")
    lines.append("")
    lines.append("Suitable for:")
    lines += [f"✓ {item}" for item in inventory.capabilities(hw)]
    return lines


def _identity_warning(label: str) -> None:
    main = _main()
    main.out(paint("SECURITY WARNING", "err"))
    main.out()
    main.out("cloud identity changed unexpectedly.")
    main.out("Connection blocked.")
    main.out()
    main.out("Actions:")
    main.out("  1. Inspect      suw cloud verify")
    main.out("  2. Re-verify    compare the fingerprint with your provider's console")
    main.out("  3. Replace cloud intentionally    suw cloud replace")


def status_payload() -> dict:
    inv = inventory.load()
    label, node = inventory.cloud_current(inv)
    if node is None:
        return {"status": health.NOT_CONFIGURED, "label": "", "configured": False}
    report = health.probe_remote("cloud")
    hw = health.hardware(report) if report.get("online") else node.get("hardware", {})
    status = health.server_status(True, report, direct=not node.get("tailscale_name"))
    return {
        "status": status,
        "configured": True,
        "label": label,
        "logical_name": "cloud",
        "tailnet_name": node.get("tailscale_name", ""),
        "provider": node.get("provider", ""),
        "fingerprint": node.get("fingerprint", ""),
        "enrolled_at": node.get("enrolled_at", ""),
        "hardware": hw,
        "identity_changed": health.identity_changed(report),
        "error": report.get("error", ""),
        "load": {k: report.get(k) for k in ("cpu_pct", "ram_used_pct", "disk_used_pct")} if report.get("online") else {},
        "gpus": report.get("gpus", []),
    }


def cmd_status(args) -> int:
    main = _main()
    data = status_payload()
    if args.json:
        main.out(json.dumps(data, ensure_ascii=False, indent=1))
        return 0 if data["status"] in (health.ONLINE, health.DEGRADED, health.NOT_CONFIGURED) else 1
    main.heading("cloud")
    if not data["configured"]:
        main.out(paint("○ NOT CONFIGURED", "dim") + "   no cloud machine right now")
        main.out("Rent one, then: suw cloud add")
        return 0
    online = data["status"] in (health.ONLINE, health.DEGRADED)
    main.out(f"{text.dot(online)} {data['status']}   {paint(data['label'], 'dim')}")
    main.out()
    if data["identity_changed"]:
        _identity_warning(data["label"])
        return 1
    for line in _hardware_block(data["hardware"]):
        main.out(line)
    main.out()
    if online:
        load = data["load"]
        line = f"CPU {text.pct(load.get('cpu_pct'))}   RAM {text.pct(load.get('ram_used_pct'))}   Disk {text.pct(load.get('disk_used_pct'))}"
        gpus = data["gpus"]
        if gpus:
            line += f"   GPU {text.pct(gpus[0]['util_pct'])}   VRAM {round(gpus[0]['used_mb'] / 1024, 1)}/{round(gpus[0]['vram_mb'] / 1024)}GB"
        main.out(line)
        if data["status"] == health.DEGRADED:
            main.out(paint("Reached over the public endpoint, not the tailnet.", "warn"))
        return 0
    main.out(paint("CLOUD " + data["status"].replace("_", " "), "warn") + paint(f"   {data['error']}", "dim"))
    main.out()
    main.out("Your local environment is unaffected.")
    main.out()
    if data["status"] == health.AUTH_REQUIRED:
        main.out("Your SSH key is not accepted any more:  ssh cloud   (to see the prompt)")
    main.out("[Reconnect]      suw cloud status")
    main.out("[Replace Cloud]  suw cloud replace")
    main.out("[Ignore]         nothing to do; SUW keeps working without it")
    return 1


def cmd_history(args) -> int:
    inv = inventory.load()
    if args.json:
        _main().out(json.dumps(inv["cloud"], ensure_ascii=False, indent=1))
        return 0
    for label, node in sorted(inv["cloud"]["nodes"].items()):
        hw = node.get("hardware", {})
        when = node.get("enrolled_at", "")[:10]
        gone = f" → {node['retired_at'][:10]}" if node.get("retired_at") else ""
        _main().out(f"{label:<20} {node.get('state', '?'):<8} {when}{gone}  {hw.get('gpu') or str(hw.get('cores', '?')) + ' vCPU'}  {node.get('provider', '')}")
    return 0


def cmd_verify(args) -> int:
    """Re-read the host key and compare it with the one trusted at enrollment."""
    main = _main()
    label, node = _current()
    host = node.get("endpoint", "")
    port = int(node.get("port", 22))
    main.out(f"Trusted at enrollment:  {paint(node.get('fingerprint', '?'), 'bold')}")
    key, fingerprint = cloud.scan_host_key(node.get("tailscale_name") or host, 22 if node.get("tailscale_name") else port)
    main.out(f"Presented right now:    {paint(fingerprint, 'bold')}")
    if cloud.fingerprints_match(node.get("fingerprint", ""), fingerprint):
        main.out(paint("✓ identity unchanged", "ok"))
        return 0
    main.out(paint("✗ identity differs - SUW keeps the connection blocked", "err"))
    main.out("If you re-installed or re-rented the machine on purpose:  suw cloud replace")
    events.emit("cloud.identity", f"{label}: host key differs from the trusted one", "warn", label=label, status="untrusted")
    return 1


def _install_key(host: str, user: str, port: int, target: cloud.Target) -> bool:
    """Offer to install the user's public key (asks for the server password once)."""
    main = _main()
    if not main.interactive() or not have("ssh-copy-id"):
        return False
    main.out(paint("Your SSH key is not authorised on this server yet.", "warn"))
    if not main.confirm("Install your public key now? (asks for the server password once)", default=True):
        return False
    rc = os.spawnvp(
        os.P_WAIT,
        "ssh-copy-id",
        [
            "ssh-copy-id",
            "-o", f"UserKnownHostsFile={target.known_hosts()}",
            "-o", f"HostKeyAlias=suw-{target.label}",
            "-o", "StrictHostKeyChecking=yes",
            "-p", str(port),
            f"{user}@{host}",
        ],
    )
    return rc == 0


def _authkey(args) -> str:
    """Tailnet auth key from the keychain, or asked once (hidden). Empty string = skip."""
    main = _main()
    if args.no_tailscale:
        return ""
    key = secrets.get(AUTHKEY) or ""
    if key or not main.interactive():
        return key
    main.out()
    main.out("Tailscale auth key (admin console → Settings → Keys; reusable + tagged is ideal).")
    key = getpass.getpass("Paste the key, or press Enter to skip the tailnet for this node: ").strip()
    if key and main.confirm("Remember it in the system keychain for future nodes?", default=True):
        try:
            secrets.put(AUTHKEY, key)
        except secrets.SecretError as exc:
            main.out(paint(f"could not store the key: {exc}", "warn"))
    return key


def _step(index: int, title: str) -> None:
    _main().out(paint(f"[{index}/8] ", "dim") + title)


def _gather(args) -> tuple[str, str, int, str, str]:
    """Ask only what cannot be detected. Everything else is discovered."""
    main = _main()
    ask = main.interactive() and not args.yes
    host = args.host or (main.ask("Address / IP") if main.interactive() else "")
    if not host:
        raise CloudError("no address given (use --host)")
    host = host.strip()
    user, port = args.user, args.port
    if "@" in host:  # accept a pasted `user@host`
        user, host = host.split("@", 1)[0] or user, host.split("@", 1)[1]
    if host.count(":") == 1:  # accept a pasted `host:port`
        host, pasted = host.split(":")
        port = port or (int(pasted) if pasted.isdigit() else None)
    user = user or (main.ask("SSH user", "root") if ask else "root")
    if not port:
        answer = main.ask("SSH port", "22") if ask else "22"
        if not answer.isdigit():
            raise CloudError(f"'{answer}' is not a port number")
        port = int(answer)
    provider = args.provider or (main.ask("Provider label", "generic") if ask else "generic")
    gpu = args.gpu if args.gpu is not None else (main.ask("GPU (optional)") if ask else "")
    if not inventory.valid_label(provider.lower()):
        raise CloudError(f"'{provider}' is not a valid provider label (letters, digits, dash)")
    if gpu and not all(c.isalnum() or c in " -_." for c in gpu):
        raise CloudError("the GPU label may only contain letters, digits, spaces, dash, dot")
    return host, user, port, provider.lower(), gpu.strip()


def cmd_add(args, replacing: str | None = None) -> int:
    main = _main()
    cfg = config.load()
    prov = cloud.provider(args.provider or "custom")
    main.heading("cloud enrollment" if not replacing else "cloud replacement")

    host, user, port, provider_label, gpu = _gather(args)
    _step(1, "Validate input")
    prov.validate_endpoint(host, user, port)
    if not args.yes and not args.dry_run and main.interactive() and not main.confirm("Proceed?", default=True):
        main.out("Cancelled; nothing was changed.")
        return 1

    _step(2, "Establish SSH")
    key, fingerprint = cloud.scan_host_key(host, port)
    _step(3, "Read host identity")
    _step(4, "Show fingerprint")
    main.out()
    main.out(paint("UNKNOWN HOST", "warn"))
    main.out()
    main.out("Fingerprint:")
    main.out(paint(fingerprint, "bold") + paint(f"   ({key.split()[0]})", "dim"))
    main.out()
    main.out("Verify this fingerprint using the provider console.")
    main.out()
    if args.dry_run:
        main.out(paint("Dry run: stopping before the trust decision. Nothing was changed.", "dim"))
        main.out(f"Would enroll {user}@{host}:{port} as the `cloud` role" + (f", retiring {replacing}" if replacing else "") + ".")
        return 0

    _step(5, "Require explicit trust confirmation")
    if args.fingerprint:
        if not cloud.fingerprints_match(args.fingerprint, fingerprint):
            raise CloudError("host key does NOT match the expected fingerprint - refusing to connect")
        main.out(paint("matches the fingerprint you supplied", "ok"))
    elif not main.interactive():
        raise CloudError("host identity not confirmed; nothing was changed (non-interactive: pass --fingerprint)")
    elif main.ask("[Trust] / [Cancel] - type 'trust' to continue", "cancel").lower() != "trust":
        raise CloudError("host identity not confirmed; nothing was changed")

    inv = inventory.load()
    label = inventory.new_cloud_label(inv)
    target = cloud.Target(host, user, port, label, key)
    node = {
        "provider": provider_label,
        "role": args.role,
        "endpoint": host,
        "port": port,
        "user": user,
        "host_key": key,
        "fingerprint": fingerprint,
    }
    if gpu:
        node["declared_gpu"] = gpu
    try:
        try:
            prov.connect(target)
        except CloudError as exc:
            if "authentication failed" in str(exc) and _install_key(host, user, port, target):
                prov.connect(target)
            else:
                raise CloudError(f"{exc}\n       fix: ssh-copy-id -p {port} {user}@{host}   then re-run `suw cloud {'replace' if replacing else 'add'}`")

        _step(6, "Install Tailscale/bootstrap")
        profile = args.profile or ""
        if not profile and main.interactive() and not args.yes:
            report = prov.discover(target)
            recommended = inventory.recommend_profile(health.hardware(report))
            profile = main.ask(f"Bootstrap profile ({'/'.join(cloud.PROFILES)}, or 'none')", recommended)

        def say(message: str) -> None:
            if "health check passed" in message:
                _step(7, 'Register logical role "cloud"')
            mark = paint("!", "warn") if message.startswith("!") else paint("✓", "ok")
            main.out(f"      {mark} {message.lstrip('! ')}")

        result = cloud.enroll(cfg, prov, target, node, authkey=_authkey(args), profile=profile, docker=args.docker, say=say)
        for step in result["steps"]:
            main.out(paint(f"        {step}", "dim"))
        _step(8, "Health check")
        main.out(f"      {paint('✓', 'ok')} `ssh cloud` answers")
    finally:
        target.close()

    final = result["node"]
    hw = final.get("hardware", {})
    main.out()
    main.out(paint("CLOUD READY", "ok"))
    main.out()
    main.out("Logical name: cloud")
    main.out(f"Tailnet name: {final.get('tailscale_name') or paint('not on the tailnet (public endpoint)', 'warn')}")
    main.out(f"GPU: {hw.get('gpu') or final.get('declared_gpu') or 'none detected'}")
    main.out(f"CPU: {hw.get('cores', '?')} vCPU")
    main.out(f"RAM: {hw.get('ram_gb', '?')} GB")
    if result["retired"]:
        main.out()
        main.out(paint(f"Previous node {result['retired']} is retired. Destroy it at the provider and remove it from the Tailscale admin console.", "dim"))
    if not final.get("bootstrap_profile"):
        main.out(paint(f"Not bootstrapped. Recommended: suw cloud bootstrap --profile {inventory.recommend_profile(hw)}", "dim"))
    main.out()
    main.out("Use:")
    main.out("  ssh cloud")
    main.out("  suw cloud status")
    return 0


def cmd_replace(args) -> int:
    main = _main()
    inv = inventory.load()
    label, node = inventory.cloud_current(inv)
    if node is None:
        main.out("No current cloud machine; enrolling a new one.")
        return cmd_add(args)
    report = health.probe_remote("cloud")
    hw = node.get("hardware", {})
    main.out(f"Current cloud: {health.server_status(True, report)}   {paint(label, 'dim')}")
    if hw.get("gpu"):
        main.out(f"GPU: {hw['gpu']}")
    main.out()
    if report.get("online"):
        target = cloud.target_for(label, node)
        try:
            findings = cloud.teardown_report(target, list(config.load().get("cloud.outputs", [])))
        finally:
            target.close()
        if cloud.is_risky(findings):
            _print_report(findings)
            main.out(paint("The current node still holds unsaved work:", "warn"))
            main.out("  suw cloud backup        (copy outputs)      git push   (on the node)")
            if not args.force and not args.dry_run:
                raise CloudError("refusing to replace while work is at risk (override with --force)")
    main.out(f"The role moves only after the new machine passes its health check; until then {label} stays `cloud`.")
    if not args.yes and not args.dry_run and not main.confirm("Replace cloud node?"):
        main.out("Cancelled.")
        return 1
    return cmd_add(args, replacing=label)


def _print_report(report: dict[str, list[str]]) -> None:
    main = _main()
    titles = {
        "unpushed": "Unpushed commits",
        "dirty": "Uncommitted changes",
        "noremote": "Repositories with no remote (exist only here)",
        "job": "Active jobs",
        "output": "Output paths with data",
        "mount": "Mounted volumes",
    }
    counts = cloud.report_counts(report)
    main.out(f"Uncommitted cloud data:  {counts['uncommitted']}")
    main.out(f"Unuploaded artifacts:    {counts['unexported']}")
    main.out(f"Running jobs:            {counts['jobs']}")
    main.out()
    for key, title in titles.items():
        items = report.get(key, [])
        if not items:
            continue
        main.out(paint(f"{'·' if key == 'mount' else '!'} {title}", "dim" if key == "mount" else "warn"))
        for item in items[:12]:
            main.out(f"    {item}")
    if not cloud.is_risky(report):
        main.out(paint("✓ No unsaved work found.", "ok"))
    main.out()


def cmd_remove(args) -> int:
    main = _main()
    cfg = config.load()
    label, node = _current()
    main.out("This removes the current cloud role.")
    main.out()
    target = cloud.target_for(label, node)
    try:
        try:
            report = cloud.teardown_report(target, list(cfg.get("cloud.outputs", [])))
        except CloudError as exc:
            main.out(paint(f"Cannot inspect {label}: {exc}", "warn"))
            main.out("If the machine is already destroyed there is nothing left to save.")
            if args.dry_run:
                return 0
            if not main.confirm("Release the cloud role without a safety report?"):
                raise CloudError("cancelled; the cloud role is unchanged")
            report = None
        if report is not None:
            _print_report(report)
            if cloud.is_risky(report) and not args.force:
                main.out(paint("BLOCKED", "err"))
                main.out()
                main.out("You have unexported data.")
                main.out()
                main.out("Use:")
                main.out("  suw cloud backup")
                raise CloudError("refusing teardown: data would be lost (override with --force)")
            if args.dry_run:
                main.out(paint("Dry run: the role would be released. Nothing was changed.", "dim"))
                return 0
            if not main.interactive():
                raise CloudError("teardown needs an interactive confirmation")
            if main.ask(f"Proceed? Type the node label to confirm ({label})") != label:
                raise CloudError("label did not match; nothing was changed")
            if node.get("tailscale_name") and main.confirm("Log the node out of the tailnet as well?", default=True):
                cloud.provider(node.get("provider", "custom")).retire(target)
    finally:
        target.close()
    cloud.release(cfg)
    main.out(f"{label} retired; the cloud role is empty. Your workstation is unaffected.")
    main.out(paint("SUW does not delete rented machines: destroy it in the provider console.", "dim"))
    return 0


def cmd_bootstrap(args) -> int:
    main = _main()
    label, node = _current()
    profile = args.profile or inventory.recommend_profile(node.get("hardware", {}))
    if profile == "none":
        return 0
    if args.dry_run:
        main.out(f"Would apply bootstrap profile '{profile}' (version {cloud.BOOTSTRAP_VERSION}) to {label}. Nothing was changed.")
        return 0
    target = cloud.target_for(label, node)
    try:
        main.out(f"Bootstrapping {label} with profile '{profile}'…")
        for step in cloud.provider(node.get("provider", "custom")).bootstrap(target, profile, args.docker):
            main.out(f"  {step}")
    finally:
        target.close()
    events.emit("cloud.bootstrap", f"{label}: profile {profile} applied", label=label, status="ok")
    return 0


def cmd_shell(args) -> int:
    """Open (or re-attach to) the persistent tmux session on the cloud node."""
    _current()
    os.execvp("ssh", ["ssh", "-t", "cloud", "command -v tmux >/dev/null 2>&1 && exec tmux new-session -A -s main || exec $SHELL -l"])


def cmd_backup(args) -> int:
    """Safety report, then copy the configured output paths off the node (checksum-verified)."""
    main = _main()
    cfg = config.load()
    label, node = _current()
    outputs = [str(o) for o in cfg.get("cloud.outputs", [])]
    target = cloud.target_for(label, node)
    try:
        report = cloud.teardown_report(target, outputs)
    finally:
        target.close()
    counts = cloud.report_counts(report)
    if args.json and args.dry_run:
        main.out(json.dumps({"label": label, **counts, "report": report}, ensure_ascii=False, indent=1))
        return 0
    main.out(f"Running jobs: {counts['jobs']}")
    main.out(f"Untracked files: {counts['uncommitted']}")
    main.out(f"Unexported artifacts: {counts['unexported']}")
    main.out()
    for key, title in (("unpushed", "push these on the node"), ("dirty", "commit these on the node"), ("noremote", "these repositories exist only on the node"), ("job", "still running")):
        for item in report.get(key, [])[:12]:
            main.out(paint(f"! {title}: {item}", "warn"))
    if args.dry_run:
        return 0
    if not outputs:
        main.out("No output paths configured, so nothing is copied. Example:")
        main.out('  suw config set cloud.outputs \'["~/outputs", "/workspace/results"]\' --shared')
        return 0 if not cloud.is_risky(report) else 1
    if not have("rsync"):
        raise CloudError("rsync is required locally")
    dest_root = paths.expand(str(cfg.get("cloud.save_to", "~/Projects/archive/cloud-outputs"))) / label
    failed = False
    for remote in outputs:
        if remote.startswith("-") or any(c in remote for c in " \n;&|`$"):
            main.out(paint(f"skipped unsafe path: {remote}", "warn"))
            continue
        dest = dest_root / remote.strip("~/").replace("/", "_")
        dest.mkdir(parents=True, exist_ok=True)
        base = ["rsync", "-a", "--partial", "-e", "ssh -o BatchMode=yes"]
        res = run([*base, f"cloud:{remote.rstrip('/')}/", f"{dest}/"], timeout=6 * 3600)
        if not res.ok:
            main.out(f"{paint('✕', 'err')} {remote}: {(res.err.strip().splitlines() or ['failed'])[-1]}")
            events.emit("cloud.backup", f"{label}: backup of {remote} failed", "error", label=label, status="error")
            failed = True
            continue
        verify = run([*base, "--checksum", "--dry-run", "--itemize-changes", f"cloud:{remote.rstrip('/')}/", f"{dest}/"], timeout=6 * 3600)
        differing = [ln for ln in verify.out.splitlines() if ln.startswith((">", "<", "c"))]
        if verify.ok and not differing:
            main.out(f"{paint('✓', 'ok')} {remote} → {dest}  (verified)")
            events.emit("cloud.backup", f"{label}: {remote} saved and verified", label=label, status="ok")
        else:
            main.out(f"{paint('✕', 'err')} {remote}: verification found {len(differing)} differing file(s)")
            failed = True
    return 1 if failed else 0


def cmd_credentials(args) -> int:
    """Store the cloud machine's password in the OS keychain and use it to install this
    machine's SSH key there. Nothing is written to any file."""
    _current()
    return credentials("cloud", args.forget)


def credentials(role: str, forget: bool = False, identity: str = "") -> int:
    from ..core import askpass

    main = _main()
    if forget:
        gone = secrets.delete(askpass.ref(role))
        main.out("The stored password was removed." if gone else "No password was stored.")
        return 0
    if not main.interactive():
        return main.err(f"run `suw {role} credentials` in a terminal: the password is typed, never passed as an argument")
    main.out(f"Password sign-in for `{role}`. Typed once, kept in {secrets.backend()}, never written to a file.")
    main.out(paint("Preferred: it is used right now to install this machine's SSH key; afterwards the key signs in.", "dim"))
    password = getpass.getpass(f"Password for {role} (hidden): ")
    if not password:
        main.out("Nothing was changed.")
        return 1
    try:
        secrets.put(askpass.ref(role), password)
    except secrets.SecretError as exc:
        return main.err(f"could not store the password: {exc}")
    del password
    key = askpass.public_key(identity)
    if key is None:
        main.out(paint("No SSH public key on this machine yet (ssh-keygen -t ed25519). The password is stored; run this again afterwards.", "warn"))
        return 1
    res = askpass.install_key(role, role, key.read_text())
    if res.ok and "installed" in res.out:
        events.emit(f"{role}.credentials", f"{role}: SSH key installed using the stored password")
        main.out(f"{paint('✓', 'ok')} {key.name} is authorised on {role}. From now on: ssh {role}")
        return 0
    state_word = health.failure_status(res.err)
    if state_word == health.UNTRUSTED:
        return main.err(f"the identity of {role} is not trusted yet — verify it first: suw {role} " + ("trust" if role == "home" else "verify"))
    reason = (res.err.strip().splitlines() or ["no answer"])[-1][:160]
    return main.err(f"sign-in with the stored password failed: {reason}")


def cmd_project(args) -> int:
    """Cloud project sessions: one project from the work folder, nothing else."""
    from ..cloud import project

    main = _main()
    cfg = config.load()
    words = list(args.words or [])
    sub = words[0] if words else "status"
    name = words[1] if len(words) > 1 else ""
    try:
        if sub == "list":
            known = {r["project"]: r for r in project.status(cfg)}
            if args.json:
                main.out(json.dumps({"projects": project.candidates(cfg), "sent": list(known.values())}, ensure_ascii=False, indent=1))
                return 0
            for item in project.candidates(cfg):
                row = known.get(item)
                main.out(f"{paint('●', 'ok') if row and row.get('state') == 'ok' else paint('○', 'dim')} {item:<28} " + paint(row["note"] if row else "only here", "dim"))
            if not project.candidates(cfg):
                main.out("No project folders in the work folder yet.")
            main.out()
            main.out("Send one:  suw cloud project push <name>")
            return 0
        if sub == "status":
            rows = project.status(cfg)
            if args.json:
                main.out(json.dumps(rows, ensure_ascii=False, indent=1))
                return 0
            if not rows:
                main.out("Nothing has been sent to the cloud from this machine.   suw cloud project list")
                return 0
            for row in rows:
                when = text.ago(row["last_ok"]) if row.get("last_ok") else "never"
                main.out(f"{paint('●', 'ok') if row.get('state') == 'ok' else paint('○', 'dim')} {row['project']:<24} {row.get('files', 0):>7} files  {row.get('bytes', 0) / 1048576:>9.1f} MB  sent {when}")
                main.out(paint(f"    {row['note']}" + (f"   revision {row['revision'][:12]}" if row.get("revision") else "") + f"   → {row.get('cloud', '')}:{row.get('destination', '')}", "dim"))
            return 0
        if not name:
            return main.err(f"usage: suw cloud project {sub} <name>   (suw cloud project list)")
        if sub == "push":
            wanted = project.plan(cfg, name, with_git=args.with_git, with_claude=args.with_claude)
            try:
                label = project.current_label()
            except project.ProjectError:
                if not args.dry_run:
                    raise
                label = "no cloud machine enrolled yet"
            for line in project.summary(wanted, label):
                main.out(line)
            if args.dry_run:
                main.out()
                main.out("Dry run: nothing was sent.")
                return 0
            reasons = project.needs_confirmation(cfg, wanted, project.load(wanted.name))
            if reasons and not args.yes:
                main.out()
                main.out(paint("Confirmation needed: " + "; ".join(reasons) + ".", "warn"))
                if not main.interactive() or not main.confirm("Send this project to the cloud?"):
                    main.out("Nothing was sent.")
                    return 1
            record = project.push(cfg, name, with_git=args.with_git, with_claude=args.with_claude, mirror=args.mirror)
            events.emit("cloud.project.push", f"{record['project']}: sent to the cloud ({record['files']} files)", project=record["project"], label=label)
            main.out()
            main.out(f"{paint('✓', 'ok')} {record['project']} is on the cloud: ssh cloud, then cd ~/{project.REMOTE_ROOT}/{record['project']}")
            return 0
        if sub == "pull":
            changes, target, backup = project.pull_preview(cfg, name)
            if not changes:
                main.out("Nothing new on the cloud for this project.")
                return 0
            main.out(f"{len(changes)} file(s) would be copied into {target}:")
            for line in changes[:20]:
                main.out(paint("  " + line.split(" ", 1)[-1].strip(), "dim"))
            if len(changes) > 20:
                main.out(paint(f"  … and {len(changes) - 20} more", "dim"))
            main.out("No local file is deleted. A local file that gets replaced is first moved to:")
            main.out(paint(f"  {backup}", "dim"))
            if args.dry_run:
                return 0
            if not args.yes and not (main.interactive() and main.confirm("Copy them?")):
                main.out("Nothing was changed.")
                return 1
            done = project.pull(cfg, name)
            events.emit("cloud.project.pull", f"{name}: {done['changed']} file(s) brought back from the cloud", project=name)
            main.out(f"{paint('✓', 'ok')} {done['changed']} file(s) copied." + (f" Replaced local versions: {done['backup']}" if done["backup"] else ""))
            return 0
        if sub == "remove":
            main.out(f"This deletes ~/{project.REMOTE_ROOT}/{name} on the cloud machine only.")
            main.out("The project here, the Home replica and the GitHub repository are not touched.")
            if args.dry_run:
                return 0
            if not args.yes and not (main.interactive() and main.confirm("Delete the cloud copy?")):
                main.out("Nothing was changed.")
                return 1
            gone = project.remove(cfg, name)
            events.emit("cloud.project.remove", f"{name}: cloud copy removed", project=name)
            main.out(f"{paint('✓', 'ok')} cloud copy removed." if gone else "There was no cloud copy.")
            return 0
    except project.ProjectError as exc:
        return main.err(str(exc))
    return main.err("usage: suw cloud project list|push|status|pull|remove [name]")
