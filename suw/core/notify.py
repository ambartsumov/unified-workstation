"""High-signal desktop notifications. One per problem, not one per retry."""

from __future__ import annotations

import time

from .. import platform as osplatform
from . import events, state
from .config import Config

REPEAT_AFTER = 6 * 3600  # the same unresolved problem may remind once per 6h at most


def send(cfg: Config, key: str, title: str, body: str, urgent: bool = False) -> bool:
    """Notify unless the same `key` was already raised and not cleared since."""
    memo = state.load("notified")
    last = memo.get(key, 0)
    if last and time.time() - last < REPEAT_AFTER:
        return False
    memo[key] = time.time()
    state.save("notified", memo)
    events.emit("notify", f"{title}: {body}", "warn", key=key)
    if not cfg.get("notify.enabled", True):
        return False
    brand = cfg.get("brand.short", "SUW")
    return bool(_deliver(brand, title, body, urgent))


def _deliver(brand: str, title: str, body: str, urgent: bool) -> bool:
    return osplatform.current().notify(brand, title, body, urgent)


def clear(key: str) -> None:
    """Problem resolved: allow a fresh notification if it ever comes back."""
    memo = state.load("notified")
    if key in memo:
        del memo[key]
        state.save("notified", memo)
