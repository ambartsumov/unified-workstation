"""The small, deterministic set of applications Workstation Mode opens.

Each launcher returns a `placement` rule (window class / title -> workspace) that the optional
GNOME helper extension uses once, right after launch. Without the extension everything still
opens; it just lands on the current workspace.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import time
from pathlib import Path

from ..core import inventory, paths
from ..core.config import Config
from ..core.proc import have, run, spawn

TERMINAL_TITLE = "Workstation · terminal"
SERVERS_TITLE = "Workstation · servers"


def editor(cfg: Config) -> str:
    wanted = str(cfg.get("workstation.editor", "auto"))
    if wanted != "auto":
        return wanted if have(wanted) else ""
    for exe in ("cursor", "code", "codium", "zed"):
        if have(exe):
            return exe
    return ""


def terminal(cfg: Config) -> str:
    wanted = str(cfg.get("workstation.terminal", "auto"))
    candidates = [wanted] if wanted != "auto" else ["wezterm", "ghostty", "kitty", "gnome-terminal", "ptyxis"]
    for exe in candidates:
        if have(exe):
            return exe
    if paths.platform() == "macos":
        return "Terminal.app"
    return ""


def suw_cmd() -> str:
    """Absolute path of the `suw` launcher (tmux panes may not have ~/.local/bin on PATH)."""
    return str(paths.launcher())


# ── tmux sessions ───────────────────────────────────────────────────────────


def tmux_has(session: str) -> bool:
    return run(["tmux", "has-session", "-t", f"={session}"], timeout=5).ok


def tmux_attached(session: str) -> bool:
    res = run(["tmux", "list-clients", "-t", f"={session}"], timeout=5)
    return res.ok and bool(res.out.strip())


_IDLE = {"sh", "bash", "zsh", "fish", "dash", "ksh"}  # a pane showing only a prompt


def linked(cfg: Config, inv: dict) -> list[str]:
    """Machines Workstation Mode keeps a connection to: the home server and every other
    workstation that has an address. Cloud is rented by the hour and is connected by hand."""
    names = ["home"] if inventory.device_host(inv["devices"].get("home", {})) else []
    for name, device in sorted(inv["devices"].items()):
        if device.get("role") == "workstation" and name != cfg.device and inventory.device_host(device):
            names.append(name)
    return names


def server_windows(cfg: Config, inv: dict) -> list[tuple[str, str]]:
    shell = os.environ.get("SHELL", "/bin/sh")
    keep = f"; exec {shlex.quote(shell)}"
    suw = shlex.quote(suw_cmd())
    return [("status", f"{suw} status --watch{keep}")] + [(name, f"{suw} link {name}{keep}") for name in linked(cfg, inv)]


def ensure_servers_session(cfg: Config, session: str = "servers") -> bool:
    """Detached tmux session: the live status panel plus one connected window per machine.

    Idempotent and self-repairing: a missing window is added, a window that fell back to a bare
    prompt is started again. A pane where something is running is never touched.
    Never blocks on the network."""
    if not have("tmux"):
        return False
    wanted = server_windows(cfg, inventory.load())
    fresh = not tmux_has(session)
    if fresh and not run(["tmux", "new-session", "-d", "-s", session, "-n", wanted[0][0], wanted[0][1]], timeout=10).ok:
        return False
    panes = {}
    for line in run(["tmux", "list-panes", "-s", "-t", f"={session}", "-F", "#{window_name}\t#{pane_current_command}\t#{pane_id}"], timeout=5).out.splitlines():
        name, _, rest = line.partition("\t")
        panes.setdefault(name, tuple(rest.split("\t")))
    for name, command in wanted:
        if name not in panes:
            run(["tmux", "new-window", "-d", "-t", f"={session}:", "-n", name, command], timeout=10)
        elif len(panes[name]) == 2 and panes[name][0] in _IDLE and not fresh:
            run(["tmux", "respawn-pane", "-k", "-t", panes[name][1], command], timeout=10)
    monitor = next((m for m in ("btop", "htop", "top") if have(m)), "")
    if monitor and fresh:
        run(["tmux", "new-window", "-d", "-t", f"={session}:", "-n", "monitor", monitor], timeout=10)
    return True


# ── who am I connected to? ──────────────────────────────────────────────────

_SSH_TAKES_VALUE = set("BbcDEeFIiJLlmOopQRSWw")
_DEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:\[\]-]*$")
_resolved: dict[str, tuple[float, str]] = {}


def ssh_destinations(listing: str) -> list[str]:
    """Destinations of the interactive `ssh` sessions in a `ps -axo tty=,args=` listing.
    Probes and file transfers have no terminal and are not what "connected" means."""
    out = []
    for line in listing.splitlines():
        words = line.split()
        if len(words) < 3 or words[0] in ("?", "??", "-") or os.path.basename(words[1]) != "ssh":
            continue
        i = 2
        while i < len(words):
            word = words[i]
            if word.startswith("-"):
                i += 2 if len(word) == 2 and word[1] in _SSH_TAKES_VALUE else 1
                continue
            out.append(word)
            break
    return out


def _address(host: str) -> str:
    """The IP address behind a name, so that two aliases of one machine compare equal (one may
    be written as a MagicDNS name, the other as an address). Gives up after a second."""
    import socket
    import threading

    found: list[str] = []

    def lookup() -> None:
        try:
            found.append(socket.gethostbyname(host))
        except OSError:
            pass

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(1.0)
    return found[0] if found else host


def ssh_host(dest: str) -> str:
    """Where `ssh <dest>` really goes (aliases of the user's own config included)."""
    if not _DEST.match(dest):
        return ""
    cached = _resolved.get(dest)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    res = run(["ssh", "-G", dest], timeout=5)
    host = next((line.split(None, 1)[1].strip().lower() for line in res.out.splitlines() if line.startswith("hostname ") and len(line.split()) > 1), "")
    if host and host != dest.split("@")[-1]:  # a bare unknown name is not worth a DNS question
        host = _address(host)
    _resolved[dest] = (time.monotonic(), host)
    return host


def ssh_sessions(names: list[str]) -> dict[str, int]:
    """How many terminal SSH sessions are open from this machine to each named machine,
    whatever alias they were started with."""
    counts = dict.fromkeys(names, 0)
    listing = run(["ps", "-axo", "tty=,args="], timeout=5).out
    targets = {name: ssh_host(name) for name in names}
    for dest in ssh_destinations(listing):
        host = ssh_host(dest)
        for name in names:
            if dest == name or dest.endswith("@" + name) or (host and host == targets[name] and host != name):
                counts[name] += 1
                break
    return counts


# ── launchers ───────────────────────────────────────────────────────────────


def open_terminal(cfg: Config, session: str, title: str, workspace: int, cwd: Path | None = None) -> dict | None:
    """Open a terminal window attached to a tmux session, unless one already is."""
    if have("tmux") and tmux_attached(session):
        return None
    exe = terminal(cfg)
    inner = ["tmux", "new-session", "-A", "-s", session] if have("tmux") else [os.environ.get("SHELL", "/bin/sh")]
    where = str(cwd or paths.home())
    klass = f"suw-{session}"
    if exe == "wezterm":
        ok = spawn(["wezterm", "start", "--class", klass, "--cwd", where, "--", *inner])
        rule = {"class": f"^{klass}$"}
    elif exe == "kitty":
        ok = spawn(["kitty", "--class", klass, "--title", title, "--directory", where, *inner])
        rule = {"class": f"^{klass}$"}
    elif exe == "ghostty":
        ok = spawn(["ghostty", f"--class=io.github.ambartsumov.{session}", f"--working-directory={where}", "-e", *inner])
        rule = {"class": f"dev\\.suw\\.{session}"}
    elif exe in ("gnome-terminal", "ptyxis"):
        flag = ["--title", title] if exe == "gnome-terminal" else ["--title", title, "--new-window"]
        ok = spawn([exe, *flag, f"--working-directory={where}", "--", *inner])
        rule = {"title": f"^{title}"}
    elif exe == "Terminal.app":
        script = f'tell application "Terminal" to do script "{" ".join(inner)}"'
        ok = run(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'], timeout=10).ok
        rule = {}
    else:
        return None
    return {**rule, "workspace": workspace} if ok and rule else ({} if ok else None)


def open_editor(cfg: Config, project: Path | None, workspace: int) -> dict | None:
    exe = editor(cfg)
    if not exe:
        return None
    if not spawn([exe, str(project)] if project else [exe]):
        return None
    klass = {"cursor": "^[Cc]ursor$", "code": "^[Cc]ode$", "codium": "codium", "zed": "zed"}.get(exe, exe)
    return {"class": klass, "workspace": workspace}


WORK_SESSIONS = ("main", "servers")


def detach_terminals() -> int:
    """Close the workstation terminal windows by detaching their tmux clients.
    The sessions (and whatever runs in them) stay alive for the next `suw mode on`."""
    if not have("tmux"):
        return 0
    count = 0
    for session in WORK_SESSIONS:
        if tmux_attached(session) and run(["tmux", "detach-client", "-s", f"={session}"], timeout=5).ok:
            count += 1
    return count


def close_rules(cfg: Config) -> list[dict]:
    """Windows the desktop helper should ask to close (a normal close request: every
    application still gets to prompt about unsaved documents)."""
    rules: list[dict] = []
    exe = editor(cfg)
    if exe:
        # `owned`: only the editor windows Workstation Mode itself opened (the desktop helper
        # remembers them); an editor window the user opened on their own is left alone.
        rules.append({"class": {"cursor": "^[Cc]ursor$", "code": "^[Cc]ode$", "codium": "codium", "zed": "zed"}.get(exe, exe), "owned": True})
    rules += [{"class": f"^suw-{session}$"} for session in WORK_SESSIONS]
    rules += [{"title": f"^{TERMINAL_TITLE}"}, {"title": f"^{SERVERS_TITLE}"}]
    if cfg.get("workstation.close_browser", False):
        from . import browser

        kind, _exe = browser.detect(cfg)
        if kind:
            rules.append({"class": {"chrome": "google-chrome"}.get(kind, kind), "owned": True})
    return rules


def workdir(cfg: Config) -> Path | None:
    """Where the editor and the terminal start: the shared work folder, when there is one."""
    if str(cfg.get("workstation.open", "work")) != "work":
        return None
    from . import worksync

    base = worksync.root(cfg)
    return base if base.is_dir() else None


def running(pattern: str) -> int:
    """How many processes of this user match (exact command name)."""
    res = run(["pgrep", "-u", str(os.getuid()), "-x", pattern], timeout=5)
    return len(res.out.split()) if res.ok else 0


def open_files(workspace: int) -> dict | None:
    if sys.platform == "darwin":
        return {} if spawn(["open", str(paths.home())]) else None
    if have("nautilus") and spawn(["nautilus", "--new-window", str(paths.home())]):
        return {"class": "org.gnome.Nautilus", "workspace": workspace}
    return None
