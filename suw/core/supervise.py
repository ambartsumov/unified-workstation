"""Background components where no session service manager exists.

systemd (Linux) and launchd (macOS) own the background services where they are available.
Everywhere else — Windows, or a Linux session without systemd — the product starts its
components itself: one detached process each, a pid file, and a record of what should be
running so the application restores it at the next sign-in.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from . import paths, state


def _pidfile(name: str) -> Path:
    return paths.ensure(paths.runtime_dir()) / f"{name}.pid"


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a finished child that nobody waited for is not running
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (OSError, IndexError):
        return True


def pid(name: str) -> int:
    try:
        value = int(_pidfile(name).read_text().strip())
    except (OSError, ValueError):
        return 0
    return value if alive(value) else 0


def running(name: str) -> bool:
    return pid(name) > 0


def start(name: str, command: list[str], env: dict[str, str] | None = None, remember: bool = True) -> bool:
    """Start `command` detached unless it is already running. Idempotent."""
    if remember:
        wanted = state.load("supervised", {})
        wanted[name] = {"command": command, "env": env or {}}
        state.save("supervised", wanted)
    if running(name):
        return True
    log = paths.ensure(paths.state_dir()) / f"{name}.log"
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        with open(log, "ab") as sink:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, env={**os.environ, **(env or {})}, **kwargs)
    except OSError:
        return False
    _pidfile(name).write_text(str(child.pid))
    return True


def stop(name: str, forget: bool = True, wait: float = 8.0) -> bool:
    if forget:
        wanted = state.load("supervised", {})
        if wanted.pop(name, None) is not None:
            state.save("supervised", wanted)
    current = pid(name)
    if not current:
        _pidfile(name).unlink(missing_ok=True)
        return True
    try:
        os.kill(current, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + wait
    while time.time() < deadline and alive(current):
        try:
            os.waitpid(current, os.WNOHANG)  # reap it when it is our own child
        except (ChildProcessError, OSError):
            pass
        time.sleep(0.1)
    if alive(current) and os.name != "nt":
        try:
            os.kill(current, signal.SIGKILL)
        except OSError:
            pass
    _pidfile(name).unlink(missing_ok=True)
    return not alive(current)


def restore() -> list[str]:
    """Start everything that should be running and is not (called at application start)."""
    started = []
    for name, spec in state.load("supervised", {}).items():
        if not running(name) and start(name, list(spec.get("command", [])), dict(spec.get("env", {})), remember=False):
            started.append(name)
    return started


def needed() -> bool:
    """True where the session has no service manager this product integrates with."""
    from .. import platform as osplatform

    return osplatform.current().service_manager not in ("systemd", "launchd")
