"""Sync orchestration on top of the git engine: which repositories, when to checkpoint,
and remembering unresolved situations so they are handled (and announced) exactly once."""

from __future__ import annotations

import time
from pathlib import Path

from . import events, gitsync, notify, paths, policy as filepolicy, projects, state
from .config import Config
from .gitsync import Net, Outcome, Status

PUSH_FAILURES_BEFORE_NOTIFY = 3


class Debounce:
    """Checkpoint timing: commit after edits have gone quiet, not too often — and, when
    the user never stops typing, at the latest after `max_wait`."""

    def __init__(self) -> None:
        self.fingerprint: str | None = None
        self.changed_at = 0.0
        self.dirty_since: float | None = None
        self.last_commit = float("-inf")

    def due(self, fingerprint: str | None, now: float, idle: float, min_interval: float, max_wait: float = 0) -> bool:
        if fingerprint is None:  # clean tree
            self.fingerprint = None
            self.dirty_since = None
            return False
        if self.dirty_since is None:
            self.dirty_since = now
        if now - self.last_commit < min_interval:
            if fingerprint != self.fingerprint:
                self.fingerprint, self.changed_at = fingerprint, now
            return False
        overdue = bool(max_wait) and now - self.dirty_since >= max_wait
        if fingerprint != self.fingerprint:
            self.fingerprint, self.changed_at = fingerprint, now
            return overdue
        return overdue or now - self.changed_at >= idle

    def committed(self, now: float) -> None:
        self.last_commit = now
        self.fingerprint = None
        self.dirty_since = None


def managed(cfg: Config) -> list[tuple[Path, dict]]:
    """Every repository the daemon looks after, with its effective policy."""
    out: list[tuple[Path, dict]] = []
    shared = paths.shared_dir()
    if (shared / ".git").exists():
        policy = gitsync.project_policy(shared, cfg.data)
        # The shared configuration repo is SUW's own data, not user work: always
        # checkpointed, short idle window, and non-conflicting divergence is merged.
        policy.update(enabled=True, idle_seconds=5, min_interval_seconds=0, max_wait_seconds=60, sync=True, policy="merge")
        out.append((shared, policy))
    from ..integrations import worksync

    shared_folder = worksync.enabled(cfg)
    for path in projects.discover(cfg):
        policy = gitsync.project_policy(path, cfg.data)
        if shared_folder and worksync.inside(cfg, path):
            # File sync keeps this working tree identical on every workstation, each with its
            # own Git database. Automatic commits on two machines would write the same change
            # twice and fork the history, so here commits are the user's (or "Save" on leaving).
            policy["enabled"] = bool(policy["enabled"] and cfg.get("work.git_autosync", False))
            policy["adopt"] = True
        out.append((path, policy))
    return out


def _pair(path: Path) -> str:
    head = gitsync.git(path, "rev-parse", "HEAD").out.strip()
    upstream = gitsync.git(path, "rev-parse", "@{upstream}").out.strip()
    return f"{head}:{upstream}"


def _diverged(cfg: Config, path: Path, memo: dict, outcome: Outcome, pair: str) -> dict:
    """First sight of a diverged/conflicted pair: pin the local side, notify once."""
    name = path.name
    if memo.get("pair") == pair:
        return memo
    branch = gitsync.create_recovery_branch(path, cfg.device)
    conflict = outcome.status is Status.CONFLICT
    detail = ("the automatic merge hit conflicts" if conflict else "both sides have new commits") + "; nothing was changed"
    if branch:
        detail += f" (your side is pinned at {branch})"
    events.emit("sync.diverged", f"{name}: {detail}", "warn", project=name, status="conflict" if conflict else "diverged", recovery_branch=branch)
    notify.send(
        cfg,
        f"conflict:{path}",
        f"{name}: sync {'conflict' if conflict else 'needs a decision'}",
        f"No data was deleted. Run: suw project recovery {name}",
        urgent=True,
    )
    return {"pair": pair, "detail": detail, "kind": Status.CONFLICT.value if conflict else Status.DIVERGED.value, "branch": branch}


def sync_project(cfg: Config, path: Path, policy: dict, *, do_fetch: bool = True, force_checkpoint: bool = False) -> dict:
    """One full cycle for one repository. Returns the snapshot row.

    Order matters: take what the remote has first (fast-forward), then checkpoint on top
    of it, then push. That keeps history linear whenever it possibly can be.
    """
    name = path.name
    auto, push = bool(policy["enabled"]), bool(policy.get("push", True))
    memo_all = state.load("sync_memo")
    memo: dict = dict(memo_all.get(str(path), {}))
    net = Net("skipped")
    outcome: Outcome | None = None
    cp: Outcome | None = None

    if policy["sync"] and do_fetch:
        net = gitsync.fetch(path)
        if net.state == "ok":
            before = gitsync.collect(path)
            if before.behind and not before.ahead and not before.conflicted:
                gitsync.reconcile(path, policy, net, pull_only=True)

    if force_checkpoint and auto:
        cp = _checkpoint(cfg, path, policy, memo)

    if not policy["sync"]:
        _store(memo_all, path, memo)
        return gitsync.summarize(path, cp, None, autosync=auto, push=push)

    current = gitsync.collect(path)
    pair = _pair(path) if (current.ahead and current.behind) else ""
    if pair and memo.get("pair") == pair and not current.conflicted:
        # Already examined and announced: do not retry the same merge every cycle.
        row = gitsync.summarize(path, cp, True, autosync=auto, push=push)
        row.update(status=memo.get("kind", Status.DIVERGED.value), detail=memo.get("detail", ""), recovery_branch=memo.get("branch", ""))
        return row

    outcome = gitsync.reconcile(path, policy, net if do_fetch else Net("ok"))
    after = gitsync.collect(path)
    failures = int(memo.get("failures", 0))
    if after.ahead and after.behind and not after.conflicted:
        memo = {**_diverged(cfg, path, memo, outcome, _pair(path)), **({"held": memo["held"]} if memo.get("held") else {})}
    else:
        for key in ("pair", "detail", "kind", "branch"):
            memo.pop(key, None)
        if outcome.status in (Status.PUSH_FAILED, Status.AUTH_REQUIRED, Status.RECOVERY_REQUIRED):
            failures += 1
            memo["failures"] = failures
            if failures == PUSH_FAILURES_BEFORE_NOTIFY:
                title = {
                    Status.AUTH_REQUIRED: f"{name}: sign-in required",
                    Status.RECOVERY_REQUIRED: f"{name}: repository needs attention",
                }.get(outcome.status, f"{name}: push keeps failing")
                notify.send(cfg, f"error:{path}", title, (outcome.detail[:120] or "see `suw history --sync`") + ". Your work is safe locally.")
                events.emit("sync.failed", f"{name}: {outcome.detail}", "error", project=name, status=outcome.status.value.lower())
        elif outcome.net in ("ok",) and outcome.status not in (Status.OFFLINE,):
            if memo.pop("failures", None):
                notify.clear(f"error:{path}")
            notify.clear(f"conflict:{path}")
    _store(memo_all, path, memo)

    final = outcome
    if cp is not None and cp.held and outcome.status not in gitsync.ATTENTION and outcome.status is not Status.OFFLINE:
        final = cp  # the remote side is fine; what needs the user is the held-back file
    elif cp is not None and cp.held:
        final = Outcome(outcome.status, outcome.action, outcome.detail, held=cp.held)
    row = gitsync.summarize(path, final, net.online if do_fetch else None, autosync=auto, push=push)
    if memo.get("pair"):
        row.update(status=memo.get("kind", Status.DIVERGED.value), detail=memo.get("detail", ""), recovery_branch=memo.get("branch", ""))
    return row


def _checkpoint(cfg: Config, path: Path, policy: dict, memo: dict) -> Outcome:
    """Local checkpoint commit; a held-back file is announced once per distinct set."""
    name = path.name
    cp = gitsync.checkpoint(path, cfg.device, policy)
    if cp.held:
        key = filepolicy.held_key([filepolicy.Held(h["path"], h["reason"]) for h in cp.held])
        if memo.get("held") != key:
            memo["held"] = key
            notify.send(cfg, f"blocked:{path}", f"{name}: something was not committed", f"{cp.detail}. No data was deleted. See: suw project status {name}")
    elif memo.pop("held", None):
        notify.clear(f"blocked:{path}")
    return cp


def checkpoint_only(cfg: Config, path: Path, policy: dict) -> Outcome:
    """Commit locally without touching the network (used while the remote is backing off:
    saving work must never wait for connectivity)."""
    memo_all = state.load("sync_memo")
    memo: dict = dict(memo_all.get(str(path), {}))
    cp = _checkpoint(cfg, path, policy, memo)
    _store(memo_all, path, memo)
    return cp


def _store(memo_all: dict, path: Path, memo: dict) -> None:
    if memo:
        memo_all[str(path)] = memo
    else:
        memo_all.pop(str(path), None)
    state.save("sync_memo", memo_all)


def error_row(path: Path, detail: str) -> dict:
    return {
        "name": path.name, "path": str(path), "status": Status.RECOVERY_REQUIRED.value, "detail": detail[:200],
        "branch": "", "upstream": "", "ahead": 0, "behind": 0, "dirty": 0, "autosync": False, "held": [],
    }


def sync_all(cfg: Config, *, do_fetch: bool = True, force_checkpoint: bool = False) -> list[dict]:
    rows = []
    for path, policy in managed(cfg):
        try:
            rows.append(sync_project(cfg, path, policy, do_fetch=do_fetch, force_checkpoint=force_checkpoint))
        except Exception as exc:  # one broken repository must not stop the others
            events.emit("sync.error", f"{path.name}: {exc}", "error", project=path.name)
            rows.append(error_row(path, str(exc)))
    return rows


def local_row(path: Path, policy: dict, memo_all: dict | None = None) -> dict:
    """Cheap status without touching the network."""
    memo = (memo_all if memo_all is not None else state.load("sync_memo")).get(str(path), {})
    row = gitsync.summarize(path, None, None, autosync=bool(policy["enabled"]), push=bool(policy.get("push", True)))
    if memo.get("pair") and row["ahead"] and row["behind"]:
        row.update(status=memo.get("kind", Status.DIVERGED.value), detail=memo.get("detail", ""), recovery_branch=memo.get("branch", ""))
    return row


def local_rows(cfg: Config) -> list[dict]:
    memo_all = state.load("sync_memo")
    return [local_row(path, policy, memo_all) for path, policy in managed(cfg)]


def counts(rows: list[dict]) -> dict:
    attention = {s.value for s in gitsync.ATTENTION}
    pending = {s.value for s in gitsync.PENDING}
    conflicts = {Status.CONFLICT.value, Status.DIVERGED.value}
    return {
        "managed": len(rows),
        "pending": sum(1 for r in rows if r["status"] in pending or r.get("ahead")),
        "conflicts": sum(1 for r in rows if r["status"] in conflicts),
        "attention": sum(1 for r in rows if r["status"] in attention),
    }


def age(ts: float | None) -> str:
    if not ts:
        return "never"
    seconds = max(0, int(time.time() - ts))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 129600:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
