"""Client for the daemon's control channel. Returns None when the daemon is not running —
callers then do the work in-process, so the CLI never depends on the daemon."""

from __future__ import annotations

from ..core import ipc


def call(command: str, timeout: float = 5, **params) -> dict | None:
    return ipc.request({"cmd": command, **params}, timeout=timeout)


def running() -> bool:
    reply = call("ping", timeout=2)
    return bool(reply and reply.get("ok"))
