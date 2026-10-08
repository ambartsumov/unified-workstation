"""suwd — the single background daemon.

Responsibilities: git reconciliation and checkpoints, device/server health, cloud inventory
refresh, SSH config re-rendering when the shared inventory changes, notifications, and a local
status cache for the CLI/UI. It stays up in both modes and never depends on the cloud node,
the home server, GitHub or Claude Code being available.

Control API: a Unix socket (mode 0600) in the runtime directory. Never a network port.
The API accepts three fixed verbs (ping / status / sync); it cannot run commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import time
from pathlib import Path

from .. import __version__
from ..core import config, events, gitsync, health, inventory, ipc, notify, paths, projects, state, syncer, watch
from ..core.lock import try_lock
from ..integrations import ssh as sshcfg
from ..integrations import peripherals, tailscale, worksync

WORK_SCAN_SECONDS = 600  # full walk of the shared work folder (conflicts, large files, new repositories)
RESCAN_SECONDS = 300  # safety net: a watched repository is still re-read this often
WAKE_DELAY = 2.0  # coalesce bursts of file events into one pass


def backoff(failures: int, cap: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter: 30 s, 60 s, 120 s … capped, ±20 %."""
    base = min(cap, 30 * 2 ** max(0, min(failures, 10) - 1))
    return base * (rng or random).uniform(0.8, 1.2)


class Daemon:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.runtime: dict = {"started": clock(), "projects": []}
        self.wake = asyncio.Event()
        self.cycle_done = asyncio.Event()
        self.stop = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.debounce: dict[str, syncer.Debounce] = {}
        self.next_fetch: dict[str, float] = {}
        self.next_scan: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self.tried_head: dict[str, str] = {}
        self.blocked: dict[str, str] = {}
        self.rows: dict[str, dict] = {}
        self.dirty: set[str] = set()
        self.pending: set[str] = set()
        self.offline_count: dict[str, int] = {}
        self.inventory_mtime = 0.0
        self.catalog_key = ""
        self.force = False
        self.wake_scheduled = False
        self.watcher = watch.Watcher(self._on_change)
        self.work_seen: dict = {}
        self.next_work_scan = 0.0
        self.route: str | None = None
        self.health_seen = 0.0

    # ── persistence ─────────────────────────────────────────────────────────

    def publish(self) -> None:
        self.runtime["ts"] = self.clock()
        self.runtime["version"] = __version__
        self.runtime["pid"] = os.getpid()
        self.runtime["watcher"] = self.watcher.stats()
        state.save("runtime", self.runtime)

    # ── file events ─────────────────────────────────────────────────────────

    def _on_change(self, root: str) -> None:
        """Called from the watcher thread."""
        self.dirty.add(root)
        loop = self.loop
        if loop is not None and not self.wake_scheduled:
            self.wake_scheduled = True
            loop.call_soon_threadsafe(loop.call_later, WAKE_DELAY, self._wake_now)

    def _wake_now(self) -> None:
        self.wake_scheduled = False
        self.wake.set()

    def _watch(self, managed: list[tuple[Path, dict]]) -> None:
        wanted = {str(path): policy for path, policy in managed}
        for root in [r for r in list(self.watcher.roots) if r not in wanted]:
            self.watcher.unwatch(root)
        for root, policy in wanted.items():
            if not self.watcher.watching(root):
                self.watcher.watch(root, policy.get("artifact_paths", []))

    # ── sync ────────────────────────────────────────────────────────────────

    def _sync_one(self, cfg: config.Config, path: Path, policy: dict, force: bool) -> dict:
        key = str(path)
        now = self.clock()
        watched = self.watcher.watching(key)
        fetch_due = now >= self.next_fetch.get(key, 0)
        if not (force or fetch_due or not watched or key in self.dirty or key in self.pending or key not in self.rows or now >= self.next_scan.get(key, 0)):
            return self.rows[key]  # nothing happened in this repository: no git call at all
        self.dirty.discard(key)
        self.next_scan[key] = now + RESCAN_SECONDS

        current = gitsync.collect(path)
        deb = self.debounce.setdefault(key, syncer.Debounce())
        checkpoint = False
        if policy["enabled"] and not current.error and not current.conflicted and not current.detached and not current.clean:
            fp = gitsync.fingerprint(path, current)
            if self.blocked.get(key) == fp and not force:
                self.pending.discard(key)  # same held-back files as last time: wait for a change
            else:
                self.blocked.pop(key, None)
                self.pending.add(key)
                checkpoint = deb.due(fp, now, policy["idle_seconds"], policy["min_interval_seconds"], policy.get("max_wait_seconds", 0)) or force
        else:
            deb.due(None, now, 0, 0)
            self.pending.discard(key)
            self.blocked.pop(key, None)

        head = gitsync.git(path, "rev-parse", "HEAD").out.strip() if current.ahead else ""
        fresh_commit = bool(head) and self.tried_head.get(key) != head
        urgent = (checkpoint or fresh_commit) and self.failures.get(key, 0) == 0
        previous = self.rows.get(key)
        held_before = previous.get("held", []) if previous else []
        if not (force or fetch_due or urgent):
            if checkpoint:  # the remote is backing off; the work is still saved locally, now
                cp = syncer.checkpoint_only(cfg, path, policy)
                deb.committed(now)
                self.pending.discard(key)
                if cp.held:
                    self.blocked[key] = gitsync.fingerprint(path, gitsync.collect(path))
                    held_before = cp.held
                    previous = {**(previous or {}), "held": cp.held}
            row = syncer.local_row(path, policy)
            # Keep what only a network attempt can know until the next attempt says otherwise.
            before = (previous or {}).get("status", "")
            if before in ("OFFLINE", "AUTH_REQUIRED", "PUSH_FAILED", "POLICY_BLOCKED") and row["status"] not in ("CONFLICT", "DIVERGED", "RECOVERY_REQUIRED"):
                if before != "POLICY_BLOCKED" or row["dirty"]:
                    row.update(status=before, detail=previous.get("detail", ""), held=previous.get("held", []))
            elif key in self.blocked and held_before and row["status"] in ("CHECKPOINT_PENDING", "LOCAL_CHANGES"):
                row.update(status="POLICY_BLOCKED", held=held_before)
            if row["status"] == "CLEAN" and key in self.next_fetch and self.failures.get(key, 0) == 0:
                row["status"] = "SYNCED"  # the last look at the remote succeeded and nothing changed since
            self.rows[key] = row
            return row

        row = syncer.sync_project(cfg, path, policy, do_fetch=True, force_checkpoint=checkpoint)
        if checkpoint:
            deb.committed(now)
            self.pending.discard(key)
            if row.get("held"):
                after = gitsync.collect(path)
                self.blocked[key] = gitsync.fingerprint(path, after)
        elif key in self.blocked and held_before and row["status"] in ("CHECKPOINT_PENDING", "LOCAL_CHANGES"):
            # Same held-back files as before: the verdict stands until they change.
            row.update(status="POLICY_BLOCKED", held=held_before, detail=(previous or {}).get("detail", ""))
        if head:
            self.tried_head[key] = gitsync.git(path, "rev-parse", "HEAD").out.strip()
        if row["status"] in ("OFFLINE", "AUTH_REQUIRED", "PUSH_FAILED", "RECOVERY_REQUIRED"):
            self.failures[key] = self.failures.get(key, 0) + 1
            cap = float(cfg.get("sync.max_backoff_seconds", 1800))
            if row["status"] == "OFFLINE":  # an unreachable remote costs nothing to ask again
                cap = min(cap, float(cfg.get("sync.offline_retry_seconds", 300)))
            delay = backoff(self.failures[key], cap)
        else:
            self.failures[key] = 0
            delay = float(cfg.get("sync.fetch_seconds", 300)) * random.uniform(0.9, 1.1)
        self.next_fetch[key] = now + delay
        self.rows[key] = row
        return row

    def network_back(self, why: str) -> bool:
        """The network looks different (new route, woke up, a server answers again): repositories
        that were waiting for the remote try again now instead of sitting out their backoff."""
        waiting = [key for key, row in self.rows.items() if row.get("status") == "OFFLINE"]
        if not waiting:
            return False
        for key in waiting:
            self.failures.pop(key, None)
            self.next_fetch.pop(key, None)
        events.emit("sync.retry", f"{why}; retrying {len(waiting)} project(s) now")
        loop = self.loop
        if loop is not None:
            loop.call_soon_threadsafe(self.wake.set)
        return True

    def watch_network(self, interval: float) -> None:
        """Cheap signals that connectivity changed: the default route, and time that passed
        while the machine was asleep. Neither sends a packet."""
        now = self.clock()
        route = health.route()
        slept = self.health_seen and now - self.health_seen > max(120.0, 3 * interval)
        changed = self.route is not None and route != self.route and bool(route)
        self.route, self.health_seen = route, now
        if slept:
            self.network_back("back from sleep")
        elif changed:
            self.network_back("network changed")

    def sync_cycle(self, force: bool) -> None:
        cfg = config.load()
        managed = syncer.managed(cfg)
        self._watch(managed)
        rows = []
        for path, policy in managed:
            try:
                rows.append(self._sync_one(cfg, path, policy, force))
            except Exception as exc:  # isolate: one repository never blocks the rest
                events.emit("sync.error", f"{path.name}: {exc}", "error", project=path.name)
                rows.append(syncer.error_row(path, str(exc)))
        for gone in set(self.rows) - {str(p) for p, _ in managed}:
            self.rows.pop(gone, None)
        self._publish_catalog(cfg, managed)
        self.runtime["projects"] = rows
        self.runtime["synced_at"] = self.clock()
        self.publish()

    def _publish_catalog(self, cfg: config.Config, managed: list[tuple[Path, dict]]) -> None:
        key = "|".join(sorted(str(p) for p, _ in managed))
        if key == self.catalog_key:
            return
        self.catalog_key = key
        try:
            if projects.publish(cfg):
                events.emit("config", "shared project list updated")
        except Exception as exc:
            events.emit("daemon.error", f"project catalog: {exc}", "error")

    async def sync_loop(self) -> None:
        while not self.stop.is_set():
            force, self.force = self.force, False
            try:
                await asyncio.to_thread(self.sync_cycle, force)
            except Exception as exc:
                events.emit("daemon.error", f"sync loop: {exc}", "error")
            self.cycle_done.set()
            self.wake.clear()
            poll = int(config.load().get("sync.poll_seconds", 10))
            if not self.watcher.available:
                poll = max(poll, 30)  # without file events every pass costs a `git status` per repository
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(5, poll))
            except asyncio.TimeoutError:
                pass

    # ── health ──────────────────────────────────────────────────────────────

    def _track(self, cfg: config.Config, name: str, report: dict) -> None:
        """Notify once when a server stays unreachable; stay silent on blips and recovery."""
        key = f"offline:{name}"
        threshold = int(cfg.get("health.offline_after_failures", 3))
        if health.identity_changed(report):
            if not self.runtime.get(f"{name}_identity_alerted"):
                self.runtime[f"{name}_identity_alerted"] = True
                events.emit(f"{name}.identity", f"{name} identity changed unexpectedly; connection blocked", "error", status="untrusted")
                notify.send(cfg, f"identity:{name}", f"{name.capitalize()} identity changed", f"Connection blocked. Run: suw {name} " + ("verify" if name == "cloud" else "status"), urgent=True)
            return
        self.runtime.pop(f"{name}_identity_alerted", None)
        if report.get("online"):
            if self.offline_count.get(name, 0) >= threshold:
                events.emit(f"{name}.reachable", f"{name} reachable again", status="ok")
            if self.offline_count.get(name, 0):
                self.network_back(f"{name} answers again")
            self.offline_count[name] = 0
            notify.clear(key)
            notify.clear(f"identity:{name}")
            return
        self.offline_count[name] = self.offline_count.get(name, 0) + 1
        if self.offline_count[name] == threshold:
            events.emit(f"{name}.unreachable", f"{name} unreachable", "warn", error=report.get("error", ""), status=str(report.get("status", "OFFLINE")).lower())
            notify.send(cfg, key, f"{name.capitalize()} server unreachable", "Your local environment is unaffected; see `suw doctor`.")

    def _refresh_ssh(self, cfg: config.Config) -> None:
        """The shared inventory may have changed on another workstation (e.g. cloud replaced
        there): re-render the local SSH aliases so `ssh cloud` follows without any action."""
        try:
            mtime = inventory.file().stat().st_mtime
        except OSError:
            return
        if mtime != self.inventory_mtime:
            self.inventory_mtime = mtime
            changed = sshcfg.apply(inventory.load(), cfg.device)
            if changed:
                events.emit("ssh", "SSH aliases re-rendered from inventory")

    def health_remote(self) -> None:
        cfg = config.load()
        self._refresh_ssh(cfg)
        before = self.runtime.get("tailscale") or {}
        self.runtime["tailscale"] = tailscale.status()
        if tailscale.cut_off(before) and not tailscale.cut_off(self.runtime["tailscale"]):
            self.network_back("tailnet is back")
        inv = inventory.load()
        if inventory.device_host(inv["devices"].get("home", {})):
            report = health.probe_remote("home", list(cfg.get("home.services", [])))
            self._track(cfg, "home", report)
            self.runtime["home"] = report
        else:
            self.runtime.pop("home", None)
        label, node = inventory.cloud_current(inv)
        if node:
            report = health.probe_remote("cloud")
            self._track(cfg, "cloud", report)
            self.runtime["cloud"] = report
            if report.get("online"):
                hardware = health.hardware(report)
                if hardware and hardware != node.get("hardware"):
                    node["hardware"] = hardware
                    inventory.save(inv)
                    events.emit("cloud.hardware", f"cloud hardware refreshed ({label})", label=label)
        else:
            self.runtime.pop("cloud", None)
            self.offline_count.pop("cloud", None)
        self.publish()

    def health_work(self, cfg: config.Config) -> None:
        """Watch the shared work folder: record transitions, announce only what needs a person
        (a new conflict copy, an error, a workstation waiting to be paired)."""
        if not worksync.enabled(cfg) or not worksync.binary():
            self.runtime.pop("work", None)
            return
        deep = self.clock() >= self.next_work_scan
        if deep:
            self.next_work_scan = self.clock() + WORK_SCAN_SECONDS
            try:
                done = worksync.refresh(cfg) if worksync.instance(cfg).configured() else None
                if done and worksync.publish_repos(cfg, done["scan"].repos):
                    events.emit("work.repos", "list of Git repositories in the work folder updated")
            except Exception as exc:
                events.emit("work.error", f"work folder housekeeping: {exc}", "error")
        report = worksync.status(cfg, deep=deep)
        if not deep:  # keep what only the full walk knows
            for key in ("conflicts", "large", "unsupported", "ignored", "files", "bytes", "repos"):
                if key in self.runtime.get("work", {}):
                    report[key] = self.runtime["work"][key]
            if report.get("conflicts") and report["state"] in ("IN_SYNC", "PEER_OFFLINE", "NO_PEER", "SYNCING"):
                report["state"] = "CONFLICTS"
        seen = self.work_seen
        state_word = report["state"]
        if seen.get("state") != state_word:
            if state_word in ("ERROR", "STOPPED") and seen.get("state") is not None:
                detail = (report.get("errors") or [{}])[0].get("error", "") if state_word == "ERROR" else "the sync service is not running"
                events.emit("work.failed", f"work folder sync needs attention: {detail}", "error", status=state_word.lower())
                notify.send(cfg, "work:state", "Work folder sync needs attention", "See: suw work status", urgent=True)
            elif state_word == "SYNCING":
                events.emit("work.sync.started", "work folder: synchronising", status="syncing")
            elif state_word == "IN_SYNC":
                events.emit("work.sync.completed", "work folder: in sync", status="ok")
                notify.clear("work:state")
            elif state_word == "PAUSED":
                events.emit("work.paused", "work folder sync is paused", "warn")
            seen["state"] = state_word
        for peer in report.get("peers", []):
            key = f"peer:{peer['id']}"
            if seen.get(key) is not None and seen[key] != peer["connected"]:
                events.emit("work.peer", f"workstation {peer['name']} " + ("connected" if peer["connected"] else "went offline; changes are queued"), device=peer["name"])
            seen[key] = peer["connected"]
        conflicts = len(report.get("conflicts", []))
        if conflicts > seen.get("conflicts", 0):
            first = report["conflicts"][0]["original"]
            events.emit("work.conflict", f"work folder: {conflicts} conflict cop{'y' if conflicts == 1 else 'ies'} (both versions kept), e.g. {first}", "warn")
            notify.send(cfg, "work:conflict", "Work folder: a file changed on both workstations", f"Both versions are kept. Review: suw work conflicts ({first})")
        elif not conflicts:
            notify.clear("work:conflict")
        seen["conflicts"] = conflicts
        waiting = report.get("unpaired", [])
        if waiting and not seen.get("unpaired"):
            events.emit("work.pairing", f"workstation {waiting[0]} is waiting to be paired", "warn")
            notify.send(cfg, "work:pair", f"Workstation '{waiting[0]}' wants to share the work folder", f"Review and confirm: suw work pair {waiting[0]}")
        seen["unpaired"] = bool(waiting)
        self.runtime["work"] = {k: v for k, v in report.items() if k != "ignored"} | {"ignored_count": len(report.get("ignored", []))}

    def health_peripherals(self, cfg: config.Config) -> None:
        if not peripherals.enabled(cfg):
            self.runtime.pop("peripherals", None)
            return
        report = peripherals.status(cfg)
        before = self.work_seen.get("peripherals")
        if before is not None and before != report["state"] and "CONNECTED" in (before, report["state"]):
            events.emit("peripherals", "keyboard/mouse sharing " + ("connected" if report["state"] == "CONNECTED" else "disconnected; each machine keeps its own keyboard"))
        self.work_seen["peripherals"] = report["state"]
        self.runtime["peripherals"] = report

    async def health_loop(self) -> None:
        next_remote = 0.0
        while not self.stop.is_set():
            cfg = config.load()
            try:
                self.watch_network(max(10, int(cfg.get("health.local_seconds", 30))))
                self.runtime["local"] = await asyncio.to_thread(health.probe_local)
                for probe in (self.health_work, self.health_peripherals):
                    try:
                        await asyncio.to_thread(probe, cfg)
                    except Exception as exc:  # one integration never takes the others down
                        events.emit("daemon.error", f"{probe.__name__}: {exc}", "error")
                if self.clock() >= next_remote:
                    next_remote = self.clock() + int(cfg.get("health.remote_seconds", 120)) * random.uniform(0.9, 1.1)
                    await asyncio.to_thread(self.health_remote)
                self.publish()
            except Exception as exc:
                events.emit("daemon.error", f"health loop: {exc}", "error")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(10, int(cfg.get("health.local_seconds", 30))))
            except asyncio.TimeoutError:
                pass

    # ── control socket ──────────────────────────────────────────────────────

    def _request_cycle(self, retry: bool) -> None:
        if retry:  # forget backoff: the user asked for another attempt right now
            self.failures.clear()
            self.next_fetch.clear()
            self.blocked.clear()
        self.force = True
        self.cycle_done.clear()
        self.wake.set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, authorised: bool = True) -> None:
        try:
            if not authorised:
                raise ValueError("unauthorised")
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(line or b"{}")
            command = request.get("cmd")
            if command == "ping":
                reply = {"ok": True, "version": __version__, "pid": os.getpid(), "uptime": self.clock() - self.runtime["started"]}
            elif command in ("sync", "retry"):
                self._request_cycle(command == "retry")
                try:
                    await asyncio.wait_for(self.cycle_done.wait(), timeout=min(600.0, float(request.get("timeout", 180))))
                    reply = {"ok": True, "projects": self.runtime.get("projects", [])}
                except asyncio.TimeoutError:
                    reply = {"ok": False, "error": "sync still running"}
            elif command == "status":
                reply = {"ok": True, "runtime": self.runtime}
            else:
                reply = {"ok": False, "error": "unknown command"}
        except (ValueError, TypeError, asyncio.TimeoutError):
            reply = {"ok": False, "error": "bad request"}
        try:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        for line in state.recover():
            events.emit("state.recovered", line, "warn")
        server, cleanup = await ipc.start_server(self.handle)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:  # Windows event loops: Ctrl+C / process termination end the daemon
                signal.signal(sig, lambda *_: self.loop.call_soon_threadsafe(self.stop.set))
        self.watcher.start()
        events.emit("daemon.started", f"suwd {__version__} started" + ("" if self.watcher.available else " (no native file watching; periodic scan)"))
        tasks = [asyncio.create_task(self.sync_loop()), asyncio.create_task(self.health_loop())]
        await self.stop.wait()
        self.wake.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.watcher.stop()
        server.close()
        cleanup()
        events.emit("daemon.stopped", "suwd stopped")


def main() -> int:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("suwd refuses to run as root: it is a per-user daemon")
        return 1
    paths.ensure(paths.runtime_dir())
    paths.ensure(paths.state_dir())
    lock = open(paths.runtime_dir() / "suwd.lock", "w")
    if not try_lock(lock):
        print("suwd is already running")
        return 0
    asyncio.run(Daemon().serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
