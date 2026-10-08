"""Advisory file locks that work on every platform (flock on POSIX, msvcrt on Windows)."""

from __future__ import annotations

from typing import IO


def try_lock(handle: IO) -> bool:
    """Take an exclusive lock without waiting. The lock lives as long as `handle` stays open."""
    try:
        import fcntl
    except ImportError:
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
