"""Git reconciliation engine.

Design rules (see docs/sync.md):
  * the worktree is sacred — nothing here ever discards, resets or overwrites local work;
  * no forced push, no branch deletion, no amend, no history rewrite, by any code path;
  * diverged history is never merged behind the user's back unless they opted in;
  * `classify()` and `decide()` are pure functions of the collected state, so the policy
    is unit-tested;
  * every automated operation leaves an audit event.
"""

from __future__ import annotations

import hashlib
import re
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from . import events, policy as filepolicy
from .proc import Result, run

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
    "GCM_INTERACTIVE": "never",
    "GIT_OPTIONAL_LOCKS": "0",  # `git status` must not rewrite the index (would wake the file watcher)
    "LC_ALL": "C",
}
AUTOSYNC_PREFIX = "chore(suw-autosync): checkpoint"
LEGACY_PREFIX = "chore(autosync): checkpoint"
RECOVERY_PREFIX = "suw/recovery"
_AUTH = re.compile(
    r"authentication failed|could not read username|could not read password|permission denied \(publickey|"
    r"terminal prompts disabled|invalid credentials|returned error: 40[13]|http 40[13]|"
    r"invalid username or password|bad credentials",
    re.I,
)
_OFFLINE = re.compile(
    r"could not resolve host|unable to access|connection timed out|network is unreachable|"
    r"could not read from remote|connection refused|failed to connect|temporary failure|"
    r"operation timed out|no route to host|timed out after|connection reset|does not appear to be a git repository",
    re.I,
)
_REJECTED = re.compile(r"non-fast-forward|fetch first|\[rejected\]|\[remote rejected\]|failed to push some refs", re.I)

DEFAULT_DENY = filepolicy.DENY
DEFAULT_ALLOW = filepolicy.ALLOW


class Status(str, Enum):
    """The explicit per-repository state machine (docs/sync.md)."""

    CLEAN = "CLEAN"  # nothing to commit; the remote has not been checked
    LOCAL_CHANGES = "LOCAL_CHANGES"  # uncommitted work, automatic checkpoints are off
    CHECKPOINT_PENDING = "CHECKPOINT_PENDING"  # uncommitted work, waiting for the quiet period
    LOCAL_COMMITTED = "LOCAL_COMMITTED"  # committed, with nowhere to push (no remote / push off)
    PUSH_PENDING = "PUSH_PENDING"  # commits queued for the remote
    PUSH_FAILED = "PUSH_FAILED"  # the remote refused the push
    REMOTE_AHEAD = "REMOTE_AHEAD"  # the remote has commits this machine has not taken yet
    SYNCED = "SYNCED"
    DIVERGED = "DIVERGED"  # both sides have commits; waiting for a human decision
    CONFLICT = "CONFLICT"  # a merge/rebase is in progress or the automatic merge hit conflicts
    OFFLINE = "OFFLINE"  # the remote is unreachable; work continues locally
    AUTH_REQUIRED = "AUTH_REQUIRED"  # the remote rejected our credentials
    POLICY_BLOCKED = "POLICY_BLOCKED"  # a secret / oversized file was kept out of the checkpoint
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"  # the repository itself needs attention


class Action(str, Enum):
    NONE = "none"
    FAST_FORWARD = "fast-forward"
    PUSH = "push"
    RECONCILE = "reconcile"
    HOLD_DIRTY = "hold-dirty"
    HOLD_CONFLICT = "hold-conflict"
    HOLD_MANUAL = "hold-manual"


LABELS = {
    Status.CLEAN: ("●", "Clean"),
    Status.LOCAL_CHANGES: ("✎", "Local changes"),
    Status.CHECKPOINT_PENDING: ("…", "Checkpoint pending"),
    Status.LOCAL_COMMITTED: ("◌", "Committed locally"),
    Status.PUSH_PENDING: ("↑", "Push pending"),
    Status.PUSH_FAILED: ("✕", "Push failed"),
    Status.REMOTE_AHEAD: ("↓", "Remote ahead"),
    Status.SYNCED: ("●", "Synced"),
    Status.DIVERGED: ("⚠", "Diverged"),
    Status.CONFLICT: ("✕", "Conflict"),
    Status.OFFLINE: ("○", "Offline"),
    Status.AUTH_REQUIRED: ("🔑", "Sign-in required"),
    Status.POLICY_BLOCKED: ("⛔", "Blocked by policy"),
    Status.RECOVERY_REQUIRED: ("✕", "Recovery required"),
}
# States that need a human; everything else resolves on its own.
ATTENTION = {Status.DIVERGED, Status.CONFLICT, Status.AUTH_REQUIRED, Status.POLICY_BLOCKED, Status.RECOVERY_REQUIRED, Status.PUSH_FAILED}
# States that mean "work is waiting to leave this machine".
PENDING = {Status.CHECKPOINT_PENDING, Status.LOCAL_COMMITTED, Status.PUSH_PENDING, Status.PUSH_FAILED, Status.OFFLINE, Status.AUTH_REQUIRED}


@dataclass
class RepoState:
    path: str
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: int = 0
    untracked: int = 0
    unmerged: int = 0
    detached: bool = False
    in_progress: str = ""  # merge | rebase | cherry-pick | ""
    files: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)  # the untracked subset of `files`
    origins: list[str] = field(default_factory=list)  # old names of renamed files
    error: str = ""

    @property
    def clean(self) -> bool:
        return self.dirty == 0 and self.untracked == 0 and self.unmerged == 0

    @property
    def conflicted(self) -> bool:
        return self.unmerged > 0 or bool(self.in_progress)


@dataclass
class Net:
    """Result of talking to the remote."""

    state: str = "ok"  # ok | offline | auth | error | skipped
    detail: str = ""

    @property
    def online(self) -> bool:
        return self.state in ("ok", "skipped")


def git(path: str | Path, *args: str, timeout: float = 30) -> Result:
    return run(["git", "-C", str(path), *args], timeout=timeout, env=GIT_ENV)


def net_failure(res: Result) -> Net:
    """Classify a failed fetch/push. Auth is checked first: ssh reports it together with
    the generic "could not read from remote" line."""
    message = (res.err or res.out).strip()
    if _AUTH.search(message):
        return Net("auth", "the remote rejected the credentials")
    if res.rc == 124 or _OFFLINE.search(message):
        return Net("offline", "remote unreachable")
    return Net("error", events.redact(message)[:300])


# ── state collection ────────────────────────────────────────────────────────


def parse_status_v2(text: str, path: str = "") -> RepoState:
    """Parse `git status --porcelain=v2 --branch` (pure)."""
    state = RepoState(path=path)
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line.split(" ", 2)[2]
            state.detached = head == "(detached)"
            state.branch = "" if state.detached else head
        elif line.startswith("# branch.upstream "):
            state.upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            match = re.search(r"\+(\d+) -(\d+)", line)
            if match:
                state.ahead, state.behind = int(match.group(1)), int(match.group(2))
        elif line.startswith("1 "):
            state.dirty += 1
            state.files.append(line.split(" ", 8)[8])
        elif line.startswith("2 "):
            state.dirty += 1
            new, _, old = line.split(" ", 9)[9].partition("\t")
            state.files.append(new)
            if old:
                state.origins.append(old)
        elif line.startswith("u "):
            state.unmerged += 1
            state.files.append(line.split(" ", 10)[10])
        elif line.startswith("? "):
            state.untracked += 1
            state.files.append(line[2:])
            state.new.append(line[2:])
    return state


def _git_dir(path: str | Path) -> Path | None:
    res = git(path, "rev-parse", "--git-dir")
    if not res.ok:
        return None
    git_dir = Path(res.out.strip())
    return git_dir if git_dir.is_absolute() else Path(path) / git_dir


def _in_progress(path: str | Path) -> str:
    git_dir = _git_dir(path)
    if git_dir is None:
        return ""
    for marker, name in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ):
        if (git_dir / marker).exists():
            return name
    return ""


def collect(path: str | Path) -> RepoState:
    res = git(path, "status", "--porcelain=v2", "--branch", "--untracked-files=all")
    if not res.ok:
        return RepoState(path=str(path), error=res.err.strip() or "not a git repository")
    state = parse_status_v2(res.out, str(path))
    state.in_progress = _in_progress(path)
    return state


def classify(state: RepoState, online: bool | None = True, *, autosync: bool = False, push: bool = True) -> Status:
    """Pure: the state implied by the repository alone. `online=None` = remote not checked."""
    if state.error:
        return Status.RECOVERY_REQUIRED
    if state.conflicted:
        return Status.CONFLICT
    local = Status.CHECKPOINT_PENDING if autosync else Status.LOCAL_CHANGES
    if state.detached:
        return Status.CLEAN if state.clean else Status.LOCAL_CHANGES
    if not state.upstream:
        return Status.LOCAL_COMMITTED if state.clean else local
    if state.ahead and state.behind:
        return Status.DIVERGED
    if online is False:
        return Status.OFFLINE
    if state.behind:
        return Status.REMOTE_AHEAD
    if not state.clean:
        return local
    if state.ahead:
        return Status.PUSH_PENDING if push else Status.LOCAL_COMMITTED
    return Status.CLEAN if online is None else Status.SYNCED


def decide(state: RepoState, policy: str = "manual", push: bool = True) -> Action:
    """The whole reconciliation policy (pure, deterministic)."""
    if state.error or state.detached or not state.upstream:
        return Action.NONE
    if state.conflicted:
        return Action.HOLD_CONFLICT
    if state.ahead and state.behind:
        if not state.clean:
            return Action.HOLD_DIRTY
        return Action.RECONCILE if policy in ("merge", "rebase") else Action.HOLD_MANUAL
    if state.behind:
        # Also attempted on a dirty tree: git refuses, without touching anything, when an
        # incoming commit would overwrite an uncommitted file.
        return Action.FAST_FORWARD
    if state.ahead:
        return Action.PUSH if push else Action.NONE
    return Action.NONE


# ── sensitive-file preflight (kept for callers that only need name checks) ───


def sensitive_files(files: list[str], deny: list[str], allow: list[str]) -> list[str]:
    return [name for name in files if filepolicy.sensitive_name(name, deny, allow)]


def oversized_files(path: str | Path, files: list[str], max_mb: float) -> list[str]:
    return filepolicy.warn_sized(Path(path), files, max_mb)


# ── project policy (.suw.toml) ──────────────────────────────────────────────


def project_policy(path: str | Path, defaults: dict) -> dict:
    """global [autosync]/[sync] defaults + the project's `.suw.toml`. Nothing else decides."""
    auto_d, sync_d = defaults.get("autosync", {}), defaults.get("sync", {})
    out = {
        "enabled": bool(auto_d.get("enabled", True)),
        "idle_seconds": int(auto_d.get("idle_seconds", 20)),
        "min_interval_seconds": int(auto_d.get("min_interval_seconds", 120)),
        "max_wait_seconds": int(auto_d.get("max_wait_seconds", 900)),
        "max_file_mb": float(auto_d.get("max_file_mb", 50)),
        "warn_file_mb": float(auto_d.get("warn_file_mb", 5)),
        "deny": list(auto_d.get("deny", DEFAULT_DENY)),
        "allow": list(auto_d.get("allow", DEFAULT_ALLOW)),
        "artifact_globs": list(auto_d.get("artifact_globs", filepolicy.ARTIFACT_GLOBS)),
        "artifact_paths": [],
        "secret_ignore": [],
        "sync": True,
        "policy": str(sync_d.get("policy", "manual")),
        "push": bool(sync_d.get("push", True)),
    }
    file = Path(path) / ".suw.toml"
    if file.exists():
        try:
            with file.open("rb") as handle:
                local = tomllib.load(handle)
        except (tomllib.TOMLDecodeError, OSError):
            local = {}
        auto = local.get("autosync", {})
        if "debounce_seconds" in auto:  # documented alias
            auto = {"idle_seconds": auto["debounce_seconds"], **auto}
        for key in ("enabled", "idle_seconds", "min_interval_seconds", "max_wait_seconds", "max_file_mb", "warn_file_mb"):
            if key in auto:
                try:
                    out[key] = type(out[key])(auto[key])
                except (TypeError, ValueError):
                    pass
        out["deny"] += [str(p) for p in auto.get("deny", [])]
        out["allow"] += [str(p) for p in auto.get("allow", [])]
        out["secret_ignore"] += [str(p) for p in auto.get("secret_ignore", [])]
        out["artifact_paths"] += [str(p) for p in local.get("artifacts", {}).get("paths", [])]
        sync = local.get("sync", {})
        if "enabled" in sync:
            out["sync"] = bool(sync["enabled"])
        if sync.get("policy") in ("merge", "rebase", "manual"):
            out["policy"] = sync["policy"]
        if "push" in sync:
            out["push"] = bool(sync["push"])
    return out


# ── checkpoint ──────────────────────────────────────────────────────────────


def fingerprint(path: str | Path, state: RepoState) -> str:
    """Changes whenever the set of modified files or any of their mtimes changes."""
    digest = hashlib.sha1()
    for name in sorted(state.files)[:2000]:
        digest.update(name.encode("utf-8", "replace"))
        try:
            digest.update(str((Path(path) / name).stat().st_mtime_ns).encode())
        except OSError:
            digest.update(b"-")
    return digest.hexdigest()


def checkpoint_message(device: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).astimezone().isoformat(timespec="seconds")
    return f"{AUTOSYNC_PREFIX} {device} {stamp}"


def is_checkpoint(subject: str) -> bool:
    return subject.startswith((AUTOSYNC_PREFIX, LEGACY_PREFIX))


@dataclass
class Outcome:
    status: Status
    action: str = "none"
    detail: str = ""
    changed: bool = False
    blocked: list[str] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    net: str = "ok"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


def _stage(path: str | Path, files: list[str]) -> Result:
    res = Result(0)
    for start in range(0, len(files), 200):
        res = git(path, "add", "--all", "--", *files[start : start + 200])
        if not res.ok:
            return res
    return res


def checkpoint(path: str | Path, device: str, policy: dict) -> Outcome:
    """Create one safe checkpoint commit.

    inspect -> ignore rules (git) -> secret scan -> size/artifact policy -> stage what is
    allowed -> commit. Files that are not allowed stay in the worktree untouched.
    """
    started = time.monotonic()
    state = collect(path)
    name = Path(path).name
    auto = bool(policy.get("enabled"))
    if state.error:
        return Outcome(Status.RECOVERY_REQUIRED, "checkpoint", state.error)
    if state.conflicted:
        return Outcome(Status.CONFLICT, "checkpoint", f"{state.in_progress or 'merge'} in progress")
    if state.detached:
        return Outcome(classify(state, None), "checkpoint", "detached HEAD - skipped")
    if state.clean:
        return Outcome(classify(state, None, autosync=auto), "checkpoint", "nothing to commit")

    stage, held = filepolicy.plan(Path(path), state.files, set(state.new), policy, consume=True)
    held_rows = [h.as_dict() for h in held]
    blocked = [h.path for h in held]
    if held:
        events.emit("sync.blocked", f"{name}: {filepolicy.describe(held)}", "warn", project=name, files=blocked[:10], status="blocked")
    if not stage:
        return Outcome(Status.POLICY_BLOCKED, "checkpoint", filepolicy.describe(held), blocked=blocked, held=held_rows)

    for big in filepolicy.warn_sized(Path(path), [f for f in stage if f in set(state.new)], float(policy.get("warn_file_mb", 5))):
        events.emit("sync.large", f"{name}: committing a large file ({big})", "warn", project=name)

    res = _stage(path, [*stage, *state.origins] if held else ["."])
    if not res.ok:
        return Outcome(Status.RECOVERY_REQUIRED, "checkpoint", res.err.strip()[:300], blocked=blocked, held=held_rows)
    if git(path, "diff", "--cached", "--quiet").ok:  # nothing actually staged (e.g. mode-only noise)
        status = Status.POLICY_BLOCKED if held else classify(collect(path), None, autosync=auto)
        return Outcome(status, "checkpoint", filepolicy.describe(held) or "nothing to commit", blocked=blocked, held=held_rows)
    message = checkpoint_message(device)
    res = git(path, "commit", "--quiet", "-m", message, timeout=60)
    if not res.ok:
        detail = "commit refused (hook or signing): " + events.redact(res.text)[:200]
        events.emit("sync.error", f"{name}: checkpoint commit failed", "error", project=name, error=res.text[:300], status="error")
        return Outcome(Status.POLICY_BLOCKED, "checkpoint", detail, blocked=blocked, held=held_rows)
    events.emit(
        "sync.checkpoint",
        f"{name}: checkpoint committed ({len(stage)} files)",
        project=name,
        status="ok",
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    status = Status.POLICY_BLOCKED if held else classify(collect(path), None, autosync=auto, push=policy.get("push", True))
    return Outcome(status, "checkpoint", filepolicy.describe(held) or message, changed=True, blocked=blocked, held=held_rows)


# ── remote ──────────────────────────────────────────────────────────────────


def fetch(path: str | Path) -> Net:
    """No remote configured counts as reachable with nothing to do."""
    if not git(path, "remote").out.strip():
        return Net("skipped", "no remote")
    res = git(path, "fetch", "--quiet", "--prune", "--no-tags", timeout=90)
    return Net() if res.ok else net_failure(res)


def remote_url(path: str | Path) -> str:
    return git(path, "config", "--get", "remote.origin.url").out.strip()


def url_has_credentials(url: str) -> bool:
    return bool(re.match(r"^[a-z+]+://[^/@\s]+:[^/@\s]+@", url)) or bool(re.match(r"^https?://(gh[pousr]_|github_pat_)[^/@]+@", url))


# ── recovery information ────────────────────────────────────────────────────


def _backup_ref(path: str | Path, branch: str) -> str:
    ref = f"refs/suw/backup/{branch}/{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    git(path, "update-ref", ref, "HEAD")
    return ref


def recovery_branches(path: str | Path) -> list[str]:
    res = git(path, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{RECOVERY_PREFIX}/")
    return [line for line in res.out.splitlines() if line.strip()]


def create_recovery_branch(path: str | Path, device: str, when: datetime | None = None) -> str:
    """Pin the current local HEAD under `suw/recovery/<device>/<timestamp>`.

    Reuses an existing recovery branch that already points at HEAD, so repeated calls for
    the same situation never pile up branches.
    """
    head = git(path, "rev-parse", "HEAD").out.strip()
    for branch in recovery_branches(path):
        if git(path, "rev-parse", branch).out.strip() == head:
            return branch
    stamp = (when or datetime.now()).strftime("%Y%m%dT%H%M%S")
    branch = f"{RECOVERY_PREFIX}/{device}/{stamp}"
    return branch if git(path, "branch", branch, "HEAD").ok else ""


def recovery_info(path: str | Path) -> dict:
    """Everything needed to recover both sides by hand. Read-only."""
    state = collect(path)

    def rev(name: str) -> str:
        res = git(path, "rev-parse", "--short", name)
        return res.out.strip() if res.ok else ""

    def log(spec: str) -> list[str]:
        res = git(path, "log", "--oneline", "-n", "20", spec)
        return res.out.splitlines() if res.ok else []

    base = git(path, "merge-base", "HEAD", "@{upstream}") if state.upstream else Result(1)
    return {
        "name": Path(path).name,
        "path": str(path),
        "branch": state.branch,
        "upstream": state.upstream,
        "local_head": rev("HEAD"),
        "remote_head": rev("@{upstream}") if state.upstream else "",
        "last_synced_head": base.out.strip()[:7] if base.ok else "",
        "unpushed": log("@{upstream}..HEAD") if state.upstream else [],
        "incoming": log("HEAD..@{upstream}") if state.upstream else [],
        "uncommitted": state.files[:50],
        "in_progress": state.in_progress,
        "recovery_branches": recovery_branches(path),
        "error": state.error,
    }


def manual_reconcile(path: str | Path, how: str) -> Outcome:
    """User-requested merge or rebase. Aborts cleanly on conflict; never forces anything."""
    state = collect(path)
    if state.error:
        return Outcome(Status.RECOVERY_REQUIRED, how, state.error)
    if state.conflicted:
        return Outcome(Status.CONFLICT, how, f"{state.in_progress or 'merge'} already in progress - finish or abort it first")
    if not state.clean:
        return Outcome(classify(state), how, "commit or stash your uncommitted work first; nothing was changed")
    if not (state.ahead and state.behind):
        return Outcome(classify(state), how, "not diverged; nothing to do")
    return _reconcile_diverged(path, state, how)


def _reconcile_diverged(path: str | Path, state: RepoState, how: str) -> Outcome:
    name = Path(path).name
    ref = _backup_ref(path, state.branch)
    if how == "rebase":
        res = git(path, "rebase", "--quiet", "@{upstream}", timeout=120)
        abort = ("rebase", "--abort")
    else:
        res = git(path, "merge", "--no-edit", "--quiet", "@{upstream}", timeout=120)
        abort = ("merge", "--abort")
    if res.ok:
        events.emit("sync.reconcile", f"{name}: diverged history reconciled ({how})", project=name, backup=ref, status="ok")
        return Outcome(Status.PUSH_PENDING, "reconcile", f"{how} ok", changed=True)
    # Conflict: put the worktree back exactly as it was and stop. Local commits are intact
    # (and additionally pinned by the backup ref); the user resolves when convenient.
    aborted = git(path, *abort)
    detail = f"{how} hit conflicts; both sides are intact"
    if not aborted.ok:
        detail += " (the abort failed - finish the merge by hand)"
    events.emit("sync.conflict", f"{name}: {detail}", "warn", project=name, backup=ref, status="conflict")
    return Outcome(Status.CONFLICT, "reconcile", detail)


# ── reconcile ───────────────────────────────────────────────────────────────


def reconcile(path: str | Path, policy: dict, net: Net | None = None, *, pull_only: bool = False) -> Outcome:
    """collect -> decide -> act. Safe to call at any time, any number of times.

    `net` is the result of a `fetch()` the caller already ran; without it we fetch here.
    """
    name = Path(path).name
    net = net if net is not None else fetch(path)
    state = collect(path)
    auto = bool(policy.get("enabled"))
    push = bool(policy.get("push", True))
    if state.error:
        return Outcome(Status.RECOVERY_REQUIRED, "none", state.error)
    if net.state == "auth":
        return Outcome(Status.AUTH_REQUIRED, "fetch", net.detail, net="auth")
    if net.state == "error":
        return Outcome(Status.PUSH_FAILED if state.ahead else classify(state, None, autosync=auto), "fetch", net.detail, net="error")
    if net.state == "offline":
        return Outcome(classify(state, False, autosync=auto, push=push), "none", "remote unreachable; will retry", net="offline")
    status = classify(state, True, autosync=auto, push=push)
    action = decide(state, policy.get("policy", "manual"), push)

    if action is Action.FAST_FORWARD:
        res = git(path, "merge", "--ff-only", "--quiet", "@{upstream}", timeout=120)
        if not res.ok and not state.clean and policy.get("adopt") and adopt_delivered(path):
            events.emit("sync.pull", f"{name}: history caught up with files that file sync already delivered ({state.behind} commit(s))", project=name, status="ok")
            return Outcome(classify(collect(path), True, autosync=auto, push=push), action.value, f"{state.behind} commit(s), files already present", changed=True)
        if not res.ok:
            if not state.clean:
                return Outcome(Status.REMOTE_AHEAD, Action.HOLD_DIRTY.value, "incoming changes touch your uncommitted files; left untouched")
            return Outcome(Status.RECOVERY_REQUIRED, action.value, res.text[:300])
        events.emit("sync.pull", f"{name}: fast-forwarded {state.behind} commit(s)", project=name, status="ok")
        return Outcome(classify(collect(path), True, autosync=auto, push=push), action.value, f"{state.behind} commit(s)", changed=True)

    if pull_only:
        return Outcome(status, "none", "")

    if action is Action.RECONCILE:
        outcome = _reconcile_diverged(path, state, policy.get("policy", "merge"))
        if outcome.status is not Status.PUSH_PENDING or not push:
            return outcome
        state = collect(path)
        action = Action.PUSH

    if action is Action.PUSH:
        started = time.monotonic()
        res = git(path, "push", "--quiet", timeout=120)
        if res.ok:
            events.emit(
                "sync.push", f"{name}: pushed {state.ahead} commit(s)", project=name, status="ok", duration_ms=int((time.monotonic() - started) * 1000)
            )
            return Outcome(classify(collect(path), True, autosync=auto, push=push), action.value, f"{state.ahead} commit(s)", changed=True)
        message = (res.err or res.out).strip()
        if _REJECTED.search(message) and not _AUTH.search(message):
            # The remote moved between fetch and push; the next cycle fetches and decides again.
            return Outcome(Status.PUSH_FAILED, action.value, "the remote moved; will fetch and re-evaluate", net="rejected")
        failure = net_failure(res)
        if failure.state == "auth":
            return Outcome(Status.AUTH_REQUIRED, action.value, failure.detail, net="auth")
        if failure.state == "offline":
            return Outcome(Status.OFFLINE, action.value, "push queued; remote unreachable", net="offline")
        return Outcome(Status.PUSH_FAILED, action.value, failure.detail, net="error")

    detail = {
        Action.HOLD_DIRTY: "diverged with uncommitted work present; left untouched",
        Action.HOLD_CONFLICT: f"{state.in_progress or 'merge'} needs manual resolution",
        Action.HOLD_MANUAL: "both sides have new commits; nothing was changed",
    }.get(action, "")
    return Outcome(status, action.value, detail)


def delivered(path: str | Path) -> bool:
    """True when every file the incoming commits change already holds, in the working tree,
    exactly the content those commits bring — the situation in a folder that a file-sync tool
    keeps identical across machines while each machine has its own Git database."""
    res = git(path, "diff", "--name-only", "-z", "HEAD", "@{upstream}")
    files = [name for name in res.out.split("\0") if name]
    if not res.ok or not files:
        return False
    if not git(path, "diff", "--cached", "--quiet").ok:  # something is staged: not ours to rearrange
        return False
    for start in range(0, len(files), 200):
        chunk = files[start : start + 200]
        tree = git(path, "ls-tree", "-z", "@{upstream}", "--", *[":(literal)" + name for name in chunk], timeout=60)
        if not tree.ok:
            return False
        wanted: dict[str, tuple[str, str]] = {}
        for record in tree.out.split("\0"):
            if record:
                meta, _, name = record.partition("\t")
                mode, _kind, blob = meta.split()
                wanted[name] = (mode, blob)
        present = []
        for name in chunk:
            file = Path(path) / name
            if name not in wanted:  # deleted upstream: must be gone here too
                if file.exists() or file.is_symlink():
                    return False
            elif wanted[name][0] not in ("100644", "100755") or not file.is_file() or file.is_symlink():
                return False
            else:
                present.append(name)
        if present:
            hashed = git(path, "hash-object", "--", *present, timeout=120)
            if not hashed.ok or hashed.out.split() != [wanted[name][1] for name in present]:
                return False
    return True


def adopt_delivered(path: str | Path) -> bool:
    """Move the branch and the index to the upstream commit without writing a single file.
    Only when `delivered()` holds, so the working tree already *is* that commit for every
    file it touches; other local edits stay exactly as they were."""
    if not delivered(path):
        return False
    target = git(path, "rev-parse", "--verify", "@{upstream}").out.strip()
    if not target or not git(path, "merge-base", "--is-ancestor", "HEAD", target).ok:
        return False
    if not git(path, "update-ref", "-m", "suw: adopt files delivered by file sync", "HEAD", target).ok:
        return False
    return git(path, "read-tree", "HEAD").ok


def summarize(path: str | Path, outcome: Outcome | None = None, online: bool | None = None, *, autosync: bool = False, push: bool = True) -> dict:
    """Snapshot row used by the CLI, the daemon cache and the UI."""
    state = collect(path)
    status = classify(state, online, autosync=autosync, push=push)
    detail = ""
    if outcome is not None:
        detail = outcome.detail
        # Verdicts that only the attempt itself can know override the repository-only view,
        # except that a repository in conflict always reads as CONFLICT.
        sticky = {Status.OFFLINE, Status.AUTH_REQUIRED, Status.PUSH_FAILED, Status.POLICY_BLOCKED, Status.CONFLICT, Status.RECOVERY_REQUIRED}
        if outcome.status in sticky and status is not Status.CONFLICT:
            status = outcome.status
    if state.detached and not detail:
        detail = "detached HEAD - not synchronised"
    elif not state.upstream and not state.error and not detail:
        detail = "no remote branch yet"
    return {
        "name": Path(path).name,
        "path": str(path),
        "branch": state.branch or ("detached" if state.detached else ""),
        "upstream": state.upstream,
        "status": status.value,
        "ahead": state.ahead,
        "behind": state.behind,
        "dirty": state.dirty + state.untracked,
        "autosync": autosync,
        "held": outcome.held if outcome else [],
        "detail": detail or state.error,
    }
