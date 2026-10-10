"""Cloud project sessions: the cloud machine receives one selected project, never the work folder.

The work folder is shared between the workstations and replicated to Home. Cloud is
different: it is rented compute. A project is sent there on request and only that project:

    suw cloud project push  <name>     preview → confirm → copy → manifest
    suw cloud project status           what is there, from which revision, when
    suw cloud project pull  <name>     bring results back; replaced local files are kept
    suw cloud project remove <name>    delete the remote copy only

Never sent: other projects, anything outside the project directory, Git internals and
project-local Claude state (unless asked for), dependency caches. Never deleted by any of
these commands: the local project, the Home replica, the GitHub repository.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core import health, inventory, paths
from ..core.config import Config
from ..core.proc import Result, have, run
from ..integrations import worksync

REMOTE_ROOT = "suw-projects"  # below the remote home directory
MANIFEST = ".suw-manifest.json"
ALIAS = "cloud"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALWAYS_LOCAL = [".git", ".claude"]  # history and assistant state stay on the workstations by default
SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.*", "*.keystore", ".netrc", ".npmrc", ".pypirc")
# Deletes one project directory below ~/suw-projects, and only if SUW's manifest is in it.
REMOVE_SCRIPT = 'd="$HOME/$1/$2"; [ -d "$d" ] || { echo absent; exit 0; }; [ -f "$d/$3" ] || { echo foreign; exit 3; }; rm -r -- "$d" && echo removed'


class ProjectError(RuntimeError):
    pass


@dataclass
class Plan:
    name: str
    source: str
    files: int = 0
    bytes: int = 0
    digest: str = ""
    revision: str = ""
    dirty: bool = False
    excluded: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)


def state_dir() -> Path:
    return paths.ensure(paths.state_dir() / "cloud-projects")


def candidates(cfg: Config) -> list[str]:
    """Top-level folders of the work folder: what can be sent."""
    base = worksync.root(cfg)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir() if entry.is_dir() and not entry.name.startswith(".") and _NAME.match(entry.name))


def locate(cfg: Config, name: str) -> Path:
    """The project directory. Only a real directory strictly inside the work folder qualifies."""
    base = worksync.root(cfg).resolve()
    raw = Path(name).expanduser()
    target = (raw if raw.is_absolute() else base / raw).resolve()
    if target == base or base not in target.parents:
        raise ProjectError(f"'{name}' is not a project inside {base} — the whole work folder is never sent to the cloud")
    if not target.is_dir():
        raise ProjectError(f"no such project folder: {target}")
    if not _NAME.match(target.name):
        raise ProjectError(f"'{target.name}' cannot be used as a remote folder name (letters, digits, dot, dash, underscore)")
    return target


def excludes(cfg: Config, with_git: bool = False, with_claude: bool = False) -> list[str]:
    names = [n for n in worksync.ignore_rules(cfg) if n != ".git"]
    names += [n for n in ALWAYS_LOCAL if not (n == ".git" and with_git) and not (n == ".claude" and with_claude)]
    names += [str(n) for n in cfg.get("cloud.project_exclude", [])]
    names += [MANIFEST, ".stfolder", ".stversions", ".stignore"]
    return sorted({n for n in names if n and "/" not in n and not n.startswith("-")})


def is_sensitive(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE)


def plan(cfg: Config, name: str, *, with_git: bool = False, with_claude: bool = False) -> Plan:
    """Exactly what a push would send: counted from the same exclusion list rsync is given."""
    source = locate(cfg, name)
    skip = excludes(cfg, with_git, with_claude)

    def skipped(entry: str) -> bool:
        return any(fnmatch.fnmatch(entry, pattern) for pattern in skip)

    out = Plan(source.name, str(source))
    digest = hashlib.sha256()
    hidden: set[str] = set()
    for current, dirs, names in os.walk(source):
        hidden.update(entry for entry in [*dirs, *names] if skipped(entry))
        dirs[:] = sorted(d for d in dirs if not skipped(d) and not os.path.islink(os.path.join(current, d)))
        for entry in sorted(names):
            if skipped(entry):
                continue
            full = os.path.join(current, entry)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, source)
            out.files += 1
            out.bytes += info.st_size
            digest.update(f"{rel}\0{info.st_size}\0{int(info.st_mtime)}\n".encode("utf-8", "surrogateescape"))
            if is_sensitive(entry):
                out.sensitive.append(rel)
    out.digest = digest.hexdigest()
    out.excluded = sorted(hidden)
    if (source / ".git").exists():
        out.revision = run(["git", "-C", str(source), "rev-parse", "HEAD"], timeout=10).out.strip()
        out.dirty = bool(run(["git", "-C", str(source), "status", "--porcelain"], timeout=20).out.strip())
    return out


def needs_confirmation(cfg: Config, wanted: Plan, previous: dict | None) -> list[str]:
    """Reasons a person must look before bytes leave the machine (empty = routine re-sync)."""
    reasons = []
    if not previous or previous.get("state") != "ok":
        reasons.append("first transfer of this project")
    limit = int(cfg.get("cloud.project_confirm_mb", 500))
    if wanted.bytes > limit * 1024 * 1024:
        reasons.append(f"larger than {limit} MB")
    if wanted.sensitive:
        reasons.append(f"{len(wanted.sensitive)} file(s) that look like secrets")
    return reasons


# ── transport (three small seams; tests replace them with local equivalents) ─────────


def destination(name: str) -> str:
    return f"{ALIAS}:{REMOTE_ROOT}/{name}/"


def transport() -> list[str]:
    config = os.environ.get("SUW_SSH_CONFIG")
    # A fresh, verified handshake per transfer: a multiplexed connection would keep talking
    # to a machine whose identity has changed since.
    # One argument (`--rsh=…`), not `-e` followed by its value: the rsync that ships with macOS
    # took the next option for the remote shell ("Failed to exec --exclude=…") when they were apart.
    return ["--rsh=ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o ControlPath=none" + (f" -F {shlex.quote(config)}" if config else "")]


def reason(stderr: str, fallback: str) -> str:
    """The line that says why, not rsync's closing 'unexplained error' boilerplate."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith(("rsync error:", "rsync: connection unexpectedly closed"))]
    return ((useful or lines or [fallback])[-1])[:300]


def remote_sh(script: str, *args: str, stdin: str | None = None, timeout: float = 60) -> Result:
    return health.ssh(ALIAS, ["sh", "-c", shlex.quote(script), "suw", *[shlex.quote(a) for a in args]], stdin=stdin, timeout=timeout)


def rsync_push(source: str, name: str, skip: list[str], *, mirror: bool = False, dry: bool = False) -> list[str]:
    cmd = ["rsync", "-a", "--partial", *transport()]
    cmd += [f"--exclude={pattern}" for pattern in skip]
    if mirror:
        cmd.append("--delete")  # excluded names (the manifest among them) are never deleted on the far side
    if dry:
        cmd += ["--dry-run", "--itemize-changes"]
    return [*cmd, source.rstrip("/") + "/", destination(name)]


def rsync_pull(name: str, target: str, backup: str, skip: list[str], *, dry: bool = False) -> list[str]:
    cmd = ["rsync", "-a", "--partial", "--itemize-changes", "--backup", f"--backup-dir={backup}", *transport()]
    cmd += [f"--exclude={pattern}" for pattern in skip]
    if dry:
        cmd.append("--dry-run")
    return [*cmd, destination(name), target.rstrip("/") + "/"]


# ── manifest ─────────────────────────────────────────────────────────────────────────


def manifest_file(name: str) -> Path:
    return state_dir() / f"{name}.json"


def load(name: str) -> dict | None:
    try:
        return json.loads(manifest_file(name).read_text())
    except (OSError, ValueError):
        return None


def save(record: dict) -> None:
    file = manifest_file(record["project"])
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    os.replace(tmp, file)


def records() -> list[dict]:
    out = []
    for file in sorted(state_dir().glob("*.json")):
        try:
            out.append(json.loads(file.read_text()))
        except (OSError, ValueError):
            continue
    return out


def current_label() -> str:
    label, node = inventory.cloud_current(inventory.load())
    if node is None:
        raise ProjectError("no cloud machine is enrolled (suw cloud add)")
    return label or ""


# ── actions ──────────────────────────────────────────────────────────────────────────


def push(cfg: Config, name: str, *, with_git: bool = False, with_claude: bool = False, mirror: bool = False) -> dict:
    if not have("rsync"):
        raise ProjectError("rsync is required on this machine")
    wanted = plan(cfg, name, with_git=with_git, with_claude=with_claude)
    label = current_label()
    skip = excludes(cfg, with_git, with_claude)
    previous = load(wanted.name) or {}
    record = {
        "project": wanted.name,
        "source": wanted.source,
        "destination": f"~/{REMOTE_ROOT}/{wanted.name}",
        "cloud": label,
        "revision": wanted.revision,
        "uncommitted_changes": wanted.dirty,
        "files": wanted.files,
        "bytes": wanted.bytes,
        "digest": wanted.digest,
        "excluded": skip,
        "with_git": with_git,
        "with_claude": with_claude,
        "started": time.time(),
        "state": "transferring",
        "last_ok": previous.get("last_ok", 0) if previous.get("cloud") == label else 0,
        "error": "",
    }
    save(record)
    made = remote_sh('umask 077; mkdir -p "$HOME/$1/$2"', REMOTE_ROOT, wanted.name, timeout=30)
    res = run(rsync_push(wanted.source, wanted.name, skip, mirror=mirror), timeout=12 * 3600) if made.ok else made
    if not res.ok:
        record.update(state="failed", error=reason(res.err, "transfer failed"))
        save(record)
        raise ProjectError(record["error"])
    record.update(state="ok", last_ok=time.time(), error="")
    public = {k: v for k, v in record.items() if k != "source"} | {"source_device": cfg.device}
    remote_sh('umask 077; cat > "$HOME/$1/$2/$3"', REMOTE_ROOT, wanted.name, MANIFEST, stdin=json.dumps(public, ensure_ascii=False, indent=1), timeout=30)
    save(record)
    return record


def status(cfg: Config) -> list[dict]:
    """Local records, each marked: current · changed since the push · on a replaced machine."""
    try:
        label = current_label()
    except ProjectError:
        label = ""
    out = []
    for record in records():
        row = dict(record)
        if record.get("state") == "removed":
            row["note"] = "removed from the cloud"
        elif not label or record.get("cloud") != label:
            row["note"] = "was sent to a machine that is no longer the cloud — push again"
        elif record.get("state") != "ok":
            row["note"] = record.get("error") or record.get("state", "")
        else:
            try:
                now = plan(cfg, record["source"], with_git=record.get("with_git", False), with_claude=record.get("with_claude", False))
                row["note"] = "up to date" if now.digest == record.get("digest") else "changed here since the last push"
            except ProjectError:
                row["note"] = "the local folder is gone"
        out.append(row)
    return out


def _sent(name: str) -> dict:
    record = load(Path(name).name) or {}
    if record.get("state") != "ok":
        raise ProjectError(f"'{name}' has not been sent to the cloud from this machine")
    if record.get("cloud") != current_label():
        raise ProjectError(f"'{name}' was sent to a machine that is no longer the cloud")
    return record


def pull_preview(cfg: Config, name: str) -> tuple[list[str], str, str]:
    record = _sent(name)
    target = locate(cfg, record["source"])
    backup = str(paths.ensure(paths.state_dir() / "cloud-pull-backup") / f"{record['project']}-{time.strftime('%Y%m%d-%H%M%S')}")
    skip = excludes(cfg, record.get("with_git", False), record.get("with_claude", False))
    res = run(rsync_pull(record["project"], str(target), backup, skip, dry=True), timeout=3600)
    if not res.ok:
        raise ProjectError(reason(res.err, "the cloud did not answer"))
    changes = [line for line in res.out.splitlines() if line[:1] in (">", "c") and not line.rstrip().endswith("/")]
    return changes, str(target), backup


def pull(cfg: Config, name: str) -> dict:
    """Copy new and changed files back. Nothing local is deleted; a local file that would be
    replaced is first moved to the backup folder."""
    changes, target, backup = pull_preview(cfg, name)
    record = _sent(name)
    skip = excludes(cfg, record.get("with_git", False), record.get("with_claude", False))
    res = run(rsync_pull(record["project"], target, backup, skip), timeout=12 * 3600)
    if not res.ok:
        raise ProjectError(reason(res.err, "transfer failed"))
    return {"changed": len(changes), "target": target, "backup": backup if Path(backup).exists() else ""}


def remove(cfg: Config, name: str) -> bool:
    """Delete the remote copy of one project. Refuses anything SUW did not put there."""
    plain = name.strip().rstrip("/")
    if not _NAME.match(plain):  # a bare name only: a path is never reduced to its last part here
        raise ProjectError(f"'{name}' is not a project name (suw cloud project status lists them)")
    current_label()
    res = remote_sh(REMOVE_SCRIPT, REMOTE_ROOT, plain, MANIFEST, timeout=600)
    if "foreign" in res.out:
        raise ProjectError(f"~/{REMOTE_ROOT}/{plain} on the cloud was not created by `suw cloud project push`; it was left alone")
    if not res.ok:
        raise ProjectError(reason(res.err, "the cloud did not answer"))
    record = load(plain)
    if record:
        record.update(state="removed", removed=time.time())
        save(record)
    return "removed" in res.out


def summary(wanted: Plan, label: str) -> list[str]:
    """The preview shown before bytes leave the machine."""
    lines = [
        f"PROJECT   {wanted.source}",
        f"FILES     {wanted.files:,}",
        f"SIZE      {wanted.bytes / 1024 / 1024:,.1f} MB",
        f"TARGET    cloud ({label}) → ~/{REMOTE_ROOT}/{wanted.name}",
    ]
    if wanted.revision:
        lines.append(f"REVISION  {wanted.revision[:12]}" + (" + uncommitted changes" if wanted.dirty else ""))
    lines.append("EXCLUDED  every other project in the work folder · machine credentials · system identity")
    if wanted.excluded:
        lines.append("          inside the project: " + ", ".join(wanted.excluded[:12]))
    if wanted.sensitive:
        lines.append(f"SECRETS   {len(wanted.sensitive)} file(s) that look like secrets WILL be sent:")
        lines += [f"          {rel}" for rel in wanted.sensitive[:8]]
        if len(wanted.sensitive) > 8:
            lines.append(f"          … and {len(wanted.sensitive) - 8} more")
    return lines


def as_dict(wanted: Plan) -> dict:
    return asdict(wanted)
