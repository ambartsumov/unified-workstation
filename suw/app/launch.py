"""Open the application window.

Preference order: an embedded native web view (pywebview, bundled in the packaged builds) →
a browser in application mode (its own window, no tabs or address bar) → the default browser.
The choice never changes what the window can do.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import platform as osplatform
from .. import product
from ..core import paths
from .backend import Backend
from .server import App

_APP_MODE_BROWSERS = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser", "microsoft-edge", "msedge", "chrome"]


def make_backend(demo: bool) -> Backend:
    if demo:
        from .demo import DemoBackend

        return DemoBackend()
    return Backend()


def _profile_dir(exe: str) -> Path:
    """Where the window keeps its browser profile. A snap-packaged browser is confined and can
    not write to hidden folders in the home directory, so it gets a folder inside its own area."""
    real = os.path.realpath(exe)
    if "/snap/" in exe or "/snap/" in real or real.endswith("/snap"):
        return paths.ensure(Path.home() / "snap" / Path(exe).name / "common" / product.SLUG)
    return paths.ensure(paths.cache_dir() / "window")


def _open_and_stay(command: list[str], grace: float = 2.5) -> bool:
    """Start the window process and make sure it did not give up immediately."""
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    try:
        return child.wait(timeout=grace) == 0  # a launcher that hands over to a running browser exits 0
    except subprocess.TimeoutExpired:
        return True


def _app_mode_command(url: str) -> list[str]:
    for name in _APP_MODE_BROWSERS:
        exe = shutil.which(name) or osplatform.current().which(name)
        if exe:
            profile = _profile_dir(exe)
            return [exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", f"--class={product.APP_ID}", "--window-size=1180,800"]
    profile = paths.ensure(paths.cache_dir() / "window")
    if paths.platform() == "macos":
        for bundle in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if os.path.isdir(f"/Applications/{bundle}.app"):
                return ["open", "-na", bundle, "--args", f"--app={url}", f"--user-data-dir={profile}", "--no-first-run"]
    return []


def run(demo: bool = False, port: int = 0, window: str = "auto", once: bool = False) -> int:
    """window: auto | webview | browser | none (print the address; for tests and remote debugging)."""
    app = App(make_backend(demo), port)
    if window == "none":
        print(app.url, flush=True)
        if once:
            return 0
        try:
            app.serve()
        except KeyboardInterrupt:
            pass
        return 0
    if window in ("auto", "webview"):
        try:
            import webview  # type: ignore[import-not-found]

            app.start()
            webview.create_window(product.NAME, app.url, width=1180, height=800, min_size=(880, 600))
            webview.start()
            app.stop()
            return 0
        except ImportError:
            if window == "webview":
                print("The embedded window component is not available in this build; opening in the browser instead.", file=sys.stderr)
        except Exception as exc:  # a broken web view must not make the product unusable
            print(f"The embedded window could not start ({exc}); opening in the browser instead.", file=sys.stderr)
    app.start()
    command = _app_mode_command(app.url)
    opened = bool(command) and _open_and_stay(command)
    if not opened:
        opened = osplatform.current().open_url(app.url)
    # The address is always printed: if no window appeared, it is the way in.
    print(f"{product.NAME} is running. If no window opened, open this address in a browser on this computer:\n  {app.url}\nKeep this process running; press Ctrl+C to close it.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app.stop()
    return 0
