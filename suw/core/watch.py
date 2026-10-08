"""OS-native file watching (Linux inotify through ctypes; no third-party packages).

The watcher only answers "something changed under this repository"; what changed is
always re-read from `git status`. Where native watching is unavailable (macOS, or a
repository too large for the watch budget) the daemon falls back to its periodic scan,
so correctness never depends on this module.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import struct
import sys
import threading
from pathlib import Path
from typing import Callable

IGNORED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", ".cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".gradle", ".next", ".turbo", "wandb", "lightning_logs",
}
GIT_NAMES = {"HEAD", "index", "ORIG_HEAD", "MERGE_HEAD", "packed-refs"}
MAX_WATCHES = 8192  # per repository; beyond this the repository is scanned periodically instead

IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE = 0x002, 0x004, 0x008
IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x040, 0x080, 0x100, 0x200
IN_DELETE_SELF, IN_Q_OVERFLOW, IN_IGNORED, IN_ISDIR = 0x400, 0x4000, 0x8000, 0x40000000
IN_NONBLOCK, IN_CLOEXEC = 0o4000, 0o2000000
MASK = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF
_EVENT = struct.Struct("iIII")


class Watcher:
    """Recursive directory watcher. `on_change(root)` is called from a background thread."""

    def __init__(self, on_change: Callable[[str], None]):
        self.on_change = on_change
        self.fd = -1
        self.libc = None
        self.lock = threading.Lock()
        self.wd: dict[int, tuple[str, str, bool]] = {}  # wd -> (root, directory, is_git_dir)
        self.roots: dict[str, set[int]] = {}
        self.ignored: dict[str, set[str]] = {}
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()
        if sys.platform.startswith("linux"):
            try:
                self.libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
                self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
            except (OSError, AttributeError):
                self.fd = -1

    @property
    def available(self) -> bool:
        return self.fd >= 0

    def watching(self, root: str | Path) -> bool:
        return str(root) in self.roots

    # ── registration ────────────────────────────────────────────────────────

    def _add(self, root: str, directory: str, git_dir: bool = False) -> bool:
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(directory), MASK)
        if wd < 0:
            return False
        self.wd[wd] = (root, directory, git_dir)
        self.roots[root].add(wd)
        return True

    def _add_tree(self, root: str, top: str) -> bool:
        ignored = IGNORED_DIRS | self.ignored.get(root, set())
        for directory, subdirs, _files in os.walk(top):
            subdirs[:] = [d for d in subdirs if d not in ignored and os.path.relpath(os.path.join(directory, d), root) not in ignored]
            if len(self.roots[root]) >= MAX_WATCHES:
                return False
            self._add(root, directory)
        return True

    def watch(self, root: str | Path, ignore: list[str] | None = None) -> bool:
        """Start watching a repository. False = not watched (caller keeps scanning)."""
        root = str(root)
        if not self.available:
            return False
        with self.lock:
            if root in self.roots:
                return True
            self.roots[root] = set()
            self.ignored[root] = {p.strip("/") for p in (ignore or []) if p.strip("/")}
            ok = self._add_tree(root, root)
            for sub in (".git", ".git/refs/heads"):
                path = os.path.join(root, sub)
                if ok and os.path.isdir(path):
                    self._add(root, path, git_dir=True)
            if not ok:
                self._drop(root)
            return ok

    def _drop(self, root: str) -> None:
        for wd in self.roots.pop(root, set()):
            self.libc.inotify_rm_watch(self.fd, wd)
            self.wd.pop(wd, None)
        self.ignored.pop(root, None)

    def unwatch(self, root: str | Path) -> None:
        with self.lock:
            self._drop(str(root))

    # ── event loop ──────────────────────────────────────────────────────────

    def _handle(self, wd: int, mask: int, name: str) -> str | None:
        if mask & IN_Q_OVERFLOW:
            return "*"
        entry = self.wd.get(wd)
        if entry is None:
            return None
        root, directory, git_dir = entry
        if mask & IN_IGNORED:
            self.wd.pop(wd, None)
            self.roots.get(root, set()).discard(wd)
            return None
        if git_dir:
            in_refs = directory.endswith(os.path.join("refs", "heads"))
            return root if (in_refs and not name.endswith(".lock")) or name in GIT_NAMES else None
        if name in IGNORED_DIRS or name.endswith((".swp", ".swx", "~")) or name.startswith(".#"):
            return None
        if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
            self._add_tree(root, os.path.join(directory, name))
        return root

    def _run(self) -> None:
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        while not self.stopping.is_set():
            if not poller.poll(500):
                continue
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return
            changed: set[str] = set()
            offset = 0
            with self.lock:
                while offset + _EVENT.size <= len(data):
                    wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
                    raw = data[offset + _EVENT.size : offset + _EVENT.size + length]
                    offset += _EVENT.size + length
                    root = self._handle(wd, mask, os.fsdecode(raw.rstrip(b"\0")))
                    if root == "*":
                        changed.update(self.roots)
                    elif root:
                        changed.add(root)
            for root in changed:
                try:
                    self.on_change(root)
                except Exception:
                    pass

    def start(self) -> None:
        if self.available and self.thread is None:
            self.thread = threading.Thread(target=self._run, name="suw-watch", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    def stats(self) -> dict:
        with self.lock:
            return {"available": self.available, "repositories": len(self.roots), "watches": len(self.wd)}
