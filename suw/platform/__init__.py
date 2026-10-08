"""Operating system adapters. `current()` is the only entry point core code uses."""

from __future__ import annotations

import sys

from .base import NEEDS_PERMISSION, NOT_INSTALLED, PARTIAL, SUPPORTED, UNAVAILABLE, Autostart, DesktopInfo, Permission, Platform

__all__ = ["current", "name", "Platform", "Permission", "Autostart", "DesktopInfo", "SUPPORTED", "PARTIAL", "NEEDS_PERMISSION", "NOT_INSTALLED", "UNAVAILABLE"]

_cache: dict[str, Platform] = {}


def name() -> str:
    """'linux', 'macos' or 'windows'."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return "linux"


def get(which: str) -> Platform:
    if which not in _cache:
        if which == "macos":
            from .macos import MacOS

            _cache[which] = MacOS()
        elif which == "windows":
            from .windows import Windows

            _cache[which] = Windows()
        else:
            from .linux import Linux

            _cache[which] = Linux()
    return _cache[which]


def current() -> Platform:
    from ..core import paths

    return get(paths.platform())
