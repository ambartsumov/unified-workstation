"""Clipboard transport.

Three separate paths, deliberately not merged into one daemon:
  * workstation ↔ workstation: the keyboard/mouse sharing tool (Deskflow);
  * workstation ↔ SSH session: OSC 52 through the terminal, tmux and any number of hops;
  * server ↔ server: nothing. Clipboard stays attached to an interactive terminal.

`self_test()` is a real end-to-end check: it emits an OSC 52 sequence carrying a random
token on the controlling terminal (optionally from the far side of an SSH connection) and
then reads the system clipboard back. It proves the path instead of assuming it.
"""

from __future__ import annotations

import base64
import os
import re
import secrets as pysecrets
import subprocess
import time
from abc import ABC, abstractmethod

from ..core import paths
from ..core.config import Config
from ..core.proc import have, run

OSC52_TERMINALS = {"wezterm", "kitty", "ghostty", "alacritty", "foot"}


class ClipboardTransport(ABC):
    name = "clipboard"

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def copy(self, text: str) -> bool: ...

    @abstractmethod
    def paste(self) -> str | None: ...


class LocalClipboard(ClipboardTransport):
    """The clipboard of the machine we run on."""

    name = "local"

    def tool(self) -> list[list[str]]:
        if paths.platform() == "macos":
            return [["pbcopy"], ["pbpaste"]] if have("pbcopy") else []
        if os.environ.get("WAYLAND_DISPLAY") and have("wl-copy"):
            return [["wl-copy"], ["wl-paste", "--no-newline"]]
        if os.environ.get("DISPLAY") and have("xclip"):
            return [["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]]
        return []

    def available(self) -> bool:
        return bool(self.tool())

    def copy(self, text: str) -> bool:
        tool = self.tool()
        if not tool:
            return False
        # wl-copy keeps a child alive that owns the selection; waiting on captured pipes
        # would block until the next copy, so its output is discarded instead.
        try:
            done = subprocess.run(tool[0], input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    def paste(self) -> str | None:
        tool = self.tool()
        if not tool:
            return None
        res = run(tool[1], timeout=5)
        return res.out if res.ok else None


def osc52(text: str) -> str:
    """The escape sequence that asks the terminal to put `text` on the clipboard."""
    return "\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a"


class Osc52(ClipboardTransport):
    """Write-only transport through the controlling terminal."""

    name = "osc52"

    def __init__(self, tty: str = "/dev/tty"):
        self.tty = tty

    def available(self) -> bool:
        try:
            fd = os.open(self.tty, os.O_WRONLY | os.O_NOCTTY)
        except OSError:
            return False
        os.close(fd)
        return True

    def copy(self, text: str) -> bool:
        try:
            with open(self.tty, "w") as handle:
                handle.write(osc52(text))
            return True
        except OSError:
            return False

    def paste(self) -> str | None:
        return None  # pasting into a terminal is a terminal feature, not a transport


def terminal_name() -> str:
    """Best-effort name of the terminal emulator this process is attached to."""
    env = os.environ
    if env.get("WEZTERM_PANE") or env.get("TERM_PROGRAM") == "WezTerm":
        return "wezterm"
    if env.get("KITTY_WINDOW_ID"):
        return "kitty"
    if env.get("GHOSTTY_RESOURCES_DIR"):
        return "ghostty"
    if env.get("ALACRITTY_WINDOW_ID"):
        return "alacritty"
    if env.get("TERM_PROGRAM") == "Apple_Terminal":
        return "Terminal.app"
    if env.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm2"
    if env.get("GNOME_TERMINAL_SCREEN") or env.get("VTE_VERSION"):
        return "gnome-terminal"
    return ""


def vte_version() -> tuple[int, int]:
    raw = os.environ.get("VTE_VERSION", "")
    if raw.isdigit() and len(raw) >= 4:
        return int(raw[:-4] or 0), int(raw[-4:-2])
    res = run(["gnome-terminal", "--version"], timeout=5)
    match = re.search(r"VTE (\d+)\.(\d+)", res.out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def terminal_supports_osc52(name: str) -> bool | None:
    """True / False / None (unknown)."""
    if name in OSC52_TERMINALS or name == "iterm2":
        return True
    if name == "gnome-terminal":
        return vte_version() >= (0, 78)
    if name == "Terminal.app":
        return False
    return None


def tmux_configured() -> bool:
    conf = paths.home() / ".tmux.conf"
    try:
        return "suw managed" in conf.read_text(errors="replace")
    except OSError:
        return False


def capabilities(cfg: Config) -> dict:
    from . import apps

    configured = apps.terminal(cfg)
    local = LocalClipboard()
    supports = terminal_supports_osc52(configured)
    ssh = "ONLINE" if supports and tmux_configured() else ("DEGRADED" if supports is False else "UNKNOWN")
    return {
        "local": "ONLINE" if local.available() else "ERROR",
        "ssh": ssh,
        "terminal": configured,
        "terminal_osc52": supports,
        "tmux": tmux_configured() and have("tmux"),
        "peer": "NOT_CONFIGURED" if not (have("deskflow") or have("deskflow-core")) else "UNKNOWN",
    }


def self_test(via: str = "", wait: float = 1.5) -> dict:
    """Emit a token via OSC 52 and read it back from the system clipboard.

    `via` = an SSH alias (home / cloud): the sequence is then printed on the remote side,
    which is exactly the server -> workstation copy path.
    Returns {"ok", "stage", "detail", ...}; never raises.
    """
    local = LocalClipboard()
    result = {"via": via or "local", "terminal": terminal_name(), "tmux": bool(os.environ.get("TMUX")), "ok": False}
    if not local.available():
        return {**result, "stage": "local-clipboard", "detail": "no local clipboard tool (wl-clipboard / pbcopy / xclip)"}
    # The clipboard is deliberately neither read nor written before the terminal had its
    # turn: on GNOME/Wayland wl-copy / wl-paste take keyboard focus, and a terminal without
    # focus may not set the clipboard. The test token is therefore left on the clipboard.
    previous = None
    # Two lines, Cyrillic, accents, CJK and an emoji: what real copied text looks like.
    token = "suw-clip-" + pysecrets.token_hex(6) + "\nстрока два — ñ 日本語 🚀"
    # No baseline copy here on purpose: on GNOME/Wayland `wl-copy` and `wl-paste` briefly take
    # keyboard focus, and a terminal that has just lost focus is not allowed to set the
    # clipboard. Touch the clipboard as little as possible until the terminal has had its turn.
    result["local_clipboard"] = True
    transport = Osc52()
    if not transport.available():
        _restore(local, previous)
        return {**result, "stage": "terminal", "detail": "no controlling terminal (run this inside a terminal window)"}
    if via:
        if not re.match(r"^[A-Za-z0-9._-]+$", via):
            return {**result, "stage": "input", "detail": "invalid host alias"}
        # The remote side prints the sequence on its tty; ssh -t carries it back to ours.
        remote = "printf '\\033]52;c;%s\\a' " + base64.b64encode(token.encode()).decode()
        rc = os.spawnvp(os.P_WAIT, "ssh", ["ssh", "-t", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", via, remote])
        if rc != 0:
            _restore(local, previous)
            return {**result, "stage": "ssh", "detail": f"could not run on {via} (ssh exit {rc})"}
    elif not transport.copy(token):
        _restore(local, previous)
        return {**result, "stage": "terminal", "detail": "could not write to the terminal"}
    seen = ""
    for pause in (0.6, wait):  # two looks, well apart: each one steals focus for a moment
        time.sleep(pause)
        seen = (local.paste() or "").strip()
        if seen == token:
            break
    _restore(local, previous)
    if seen == token:
        return {**result, "ok": True, "stage": "done", "detail": "OSC 52 reached the system clipboard (multi-line Unicode text arrived intact)"}
    hint = "the terminal ignored the OSC 52 sequence"
    if result["terminal"] == "gnome-terminal":
        hint += " (GNOME Terminal needs VTE 0.78+; use WezTerm)"
    elif result["tmux"]:
        hint += " or tmux did not pass it on (check `set -g set-clipboard on`)"
    return {**result, "stage": "osc52", "detail": hint}


def repeated(via: str = "", runs: int = 1) -> dict:
    """`self_test` several times in a row; passes only when every run did."""
    runs = max(1, min(int(runs), 50))
    last, passed = {}, 0
    for _ in range(runs):
        last = self_test(via)
        passed += bool(last.get("ok"))
    return {**last, "ok": passed == runs, "runs": runs, "passed": passed}


def window_test(suw: str, via: str = "", runs: int = 3, timeout: float = 300) -> dict:
    """Run the test in a terminal window of its own, so it can be started from anywhere
    (a script, the Control Center) and gives the same answer every time.

    On Wayland a terminal may only set the clipboard while it has keyboard focus, and a
    window opened by a background process does not get focus. The test window therefore
    uses WezTerm's X11 backend, where setting the clipboard does not depend on focus. What
    is proven: OSC 52 through ssh, tmux and WezTerm into the system clipboard."""
    import json
    import tempfile

    result = {"via": via or "local", "terminal": "wezterm", "ok": False, "window": True}
    if not have("wezterm"):
        return {**result, "stage": "terminal", "detail": "WezTerm is not installed"}
    if via and not re.match(r"^[A-Za-z0-9._-]+$", via):
        return {**result, "stage": "input", "detail": "invalid host alias"}
    with tempfile.TemporaryDirectory(prefix="suw-clip-") as tmp:
        report = os.path.join(tmp, "report.json")
        inner = [suw, "clipboard", "test", "--repeat", str(runs), "--report", report, *(["--via", via] if via else [])]
        run(["wezterm", "--config", "enable_wayland=false", "start", "--always-new-process", "--class", "suw-cliptest", "--", *inner], timeout=timeout)
        try:
            with open(report) as handle:
                return {**json.load(handle), "window": True}
        except (OSError, ValueError):
            return {**result, "stage": "terminal", "detail": "the test window did not report back"}


def _restore(local: LocalClipboard, previous: str | None) -> None:
    if previous is not None:
        local.copy(previous)
