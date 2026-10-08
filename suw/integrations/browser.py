"""Named browser sessions. Uses the system default browser unless configured otherwise.

Workstation Mode must end in *one* logical browser workspace however often it is entered:
the session is opened once per login session of the machine and is not opened again while
the browser it was opened in is still running.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from ..core import paths, state
from ..core.config import Config
from ..core.proc import have, run, spawn

# kind -> candidate executables (Linux) / application names (macOS)
LINUX = {
    "chromium": ["chromium", "chromium-browser"],
    "chrome": ["google-chrome", "google-chrome-stable"],
    "brave": ["brave-browser", "brave"],
    "firefox": ["firefox"],
}
MACOS = {"chrome": "Google Chrome", "firefox": "Firefox", "safari": "Safari", "brave": "Brave Browser", "chromium": "Chromium"}
PROCESS = {
    "chromium": "chromium",
    "chrome": "google-chrome|/opt/google/chrome|Google Chrome",
    "brave": "brave|Brave Browser",
    "firefox": "firefox|Firefox",
    "safari": "Safari.app",
}


def _kind_from_desktop(desktop_id: str) -> str:
    ident = desktop_id.lower()
    for kind in ("chromium", "chrome", "brave", "firefox"):
        if kind in ident:
            return kind
    return ""


def detect(cfg: Config) -> tuple[str, str]:
    """(kind, executable-or-app). kind '' means 'let the OS decide'."""
    wanted = str(cfg.get("workstation.browser.app", "default")).lower()
    if paths.platform() == "macos":
        return (wanted, MACOS[wanted]) if wanted in MACOS else ("", "")
    kind = wanted if wanted in LINUX else _kind_from_desktop(run(["xdg-settings", "get", "default-web-browser"], timeout=5).out)
    for exe in LINUX.get(kind, []):
        if have(exe):
            return kind, exe
    return "", ""


def session_urls(cfg: Config, session: str = "workstation") -> tuple[list[str], list[str]]:
    """Resolve the configured entries to URLs. Returns (urls, unresolved-names).

    An entry without a configured URL is reported, never replaced by a guess.
    """
    if session != "workstation":
        return [], []
    urls, missing = [], []
    for entry in cfg.get("workstation.browser.open", []):
        url = cfg.url(str(entry))
        if url and url not in urls:
            urls.append(url)
        elif not url:
            missing.append(str(entry))
    return urls, missing


def boot_id() -> str:
    """Identifies the current uptime of the machine (changes on reboot)."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        kernel_query = "sys" + "ctl"  # macOS: read-only query of the boot time
        return run([kernel_query, "-n", "kern.boottime"], timeout=5).out.strip()


def profile_dir(cfg: Config, kind: str, exe: str) -> str:
    """Directory of the dedicated Workstation profile, or '' when the user's own is used."""
    if str(cfg.get("workstation.browser.profile", "existing")) != "dedicated" or kind not in LINUX:
        return ""
    if kind == "chromium" and "/snap/" in (shutil.which(exe) or ""):
        return str(paths.home() / "snap" / "chromium" / "common" / "suw-workstation")  # snap confinement
    return str(paths.home() / ".local" / "share" / "suw" / f"browser-{kind}")


def running(kind: str, profile: str = "") -> bool:
    pattern = profile or PROCESS.get(kind, "")
    if not pattern:
        return False
    return run(["pgrep", "-u", str(os.getuid()), "-f", pattern], timeout=5).ok


def command(kind: str, exe: str, urls: list[str], profile: str = "") -> list[str]:
    if kind == "firefox":
        cmd = [exe, *(["--profile", profile, "--new-instance"] if profile else []), "--new-window", urls[0]]
        for url in urls[1:]:
            cmd += ["--new-tab", url]
        return cmd
    if kind:
        return [exe, *([f"--user-data-dir={profile}"] if profile else []), "--new-window", *urls]
    return ["xdg-open", urls[0]]


def already_open(urls: list[str]) -> bool:
    """Was this exact session opened since boot, and is that browser still running?"""
    memo = state.load("browser")
    if not memo or memo.get("boot") != boot_id() or memo.get("urls") != urls:
        return False
    kind = memo.get("kind", "")
    return running(kind, memo.get("profile", "")) if kind else True


def open_session(cfg: Config, session: str = "workstation", force: bool = False) -> tuple[bool, str]:
    urls, missing = session_urls(cfg, session)
    unresolved = ""
    if missing:
        unresolved = f"no address is set for: {', '.join(missing)} (Settings → Workspace → Browser)"
    if not urls:
        return False, unresolved or "no URLs configured"
    if not force and already_open(urls):
        return True, "already open for this session" + (f"; {unresolved}" if unresolved else "")
    kind, exe = detect(cfg)
    profile = profile_dir(cfg, kind, exe)
    if paths.platform() == "macos":
        ok = spawn(["open", *(["-a", exe] if exe else []), *urls])
    elif kind:
        if profile:
            paths.ensure(Path(profile))
        ok = spawn(command(kind, exe, urls, profile))
    else:
        ok = all(spawn(["xdg-open", url]) for url in urls)
    if ok:
        state.save("browser", {"boot": boot_id(), "urls": urls, "kind": kind, "profile": profile, "opened": time.time()})
    note = f"{len(urls)} tab(s) in {exe or 'default browser'}" + (" (workstation profile)" if profile else "")
    if unresolved:
        note += f"; {unresolved}"
    return ok, note
