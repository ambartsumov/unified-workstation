"""GNOME integration: shortcuts, workspaces, dock and wallpaper — native settings only.

Two kinds of change, kept apart on purpose:
  * install-time (shortcut registration)  -> recorded in the installer journal, undone by
    `suw rollback`;
  * mode-time (workspaces, dock, wallpaper) -> previous values are captured when Workstation
    Mode starts and put back when it ends.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from ..core import journal, paths
from ..core.config import Config
from ..core.proc import have, run

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
CUSTOM_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SCAN_SCHEMAS = [
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
    MEDIA_KEYS,
]
EXTENSION_UUID = "helper@ambartsumov.github.io"


def available() -> bool:
    return have("gsettings") and "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def _list(text: str | None) -> list:
    """Parse a GVariant string list such as "['a', 'b']" or "@as []"."""
    if not text:
        return []
    text = re.sub(r"^@a\w+\s+", "", text.strip())
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _fmt(items: list) -> str:
    return "[" + ", ".join("'" + str(i).replace("'", "\\'") + "'" for i in items) + "]"


def norm(binding: str) -> str:
    """Canonical form so '<Super>d' == '<super>D' and <Primary> == <Control>."""
    mods = sorted(m.lower().replace("primary", "control") for m in re.findall(r"<([^>]+)>", binding))
    key = re.sub(r"<[^>]+>", "", binding).lower()
    return "".join(f"<{m}>" for m in mods) + key


# ── shortcuts ───────────────────────────────────────────────────────────────


def find_conflicts(binding: str, ignore_names: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Every (schema, key) currently using `binding`."""
    wanted = norm(binding)
    hits: list[tuple[str, str]] = []
    for schema in SCAN_SCHEMAS:
        res = run(["gsettings", "list-recursively", schema], timeout=8)
        for line in res.out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and any(norm(str(b)) == wanted for b in _list(parts[2])):
                hits.append((parts[0], parts[1]))
    for path in _list(journal.gsettings_get(MEDIA_KEYS, "custom-keybindings")):
        if any(path.rstrip("/").endswith(name) for name in ignore_names):
            continue
        current = (journal.gsettings_get(CUSTOM, "binding", path) or "").strip("'")
        if current and norm(current) == wanted:
            hits.append((CUSTOM, path))
    return hits


def register_shortcut(slug: str, name: str, command: str, binding: str) -> tuple[bool, str]:
    """Create/refresh one custom shortcut. Refuses to steal a binding that is the only way
    to reach an existing action; shares nothing silently."""
    path = f"{CUSTOM_PATH}{slug}/"
    for schema, key in find_conflicts(binding, ignore_names=(slug,)):
        if schema == CUSTOM:
            return False, f"{binding} is used by another custom shortcut ({key})"
        current = _list(journal.gsettings_get(schema, key))
        remaining = [b for b in current if norm(str(b)) != norm(binding)]
        if not remaining:
            return False, f"{binding} is the only binding of {schema} {key}; not taking it"
        journal.gsettings_set(schema, key, _fmt(remaining))
    paths_now = _list(journal.gsettings_get(MEDIA_KEYS, "custom-keybindings"))
    if path not in paths_now:
        journal.gsettings_set(MEDIA_KEYS, "custom-keybindings", _fmt(paths_now + [path]))
        journal.record("gsettings-reset", path, schema=f"{CUSTOM}:{path}")
    ok = True
    for key, value in (("name", name), ("command", command), ("binding", binding)):
        ok &= journal.gsettings_set(CUSTOM, key, "'" + value.replace("'", "\\'") + "'", path, journal=False)
    return ok, binding


# ── mode appearance ─────────────────────────────────────────────────────────

MODE_KEYS = [
    ("org.gnome.mutter", "dynamic-workspaces"),
    ("org.gnome.desktop.wm.preferences", "num-workspaces"),
    ("org.gnome.desktop.wm.preferences", "workspace-names"),
    ("org.gnome.shell", "favorite-apps"),
    ("org.gnome.desktop.background", "picture-uri"),
    ("org.gnome.desktop.background", "picture-uri-dark"),
    ("org.gnome.desktop.background", "picture-options"),
    ("org.gnome.desktop.background", "primary-color"),
    ("org.gnome.shell.extensions.dash-to-dock", "hot-keys"),
] + [("org.gnome.shell.keybindings", f"switch-to-application-{n}") for n in range(1, 10)] + [
    ("org.gnome.desktop.wm.keybindings", f"switch-to-workspace-{n}") for n in range(1, 10)
]


def capture() -> dict:
    """Current values of everything Workstation Mode may change."""
    saved = {}
    for schema, key in MODE_KEYS:
        value = journal.gsettings_get(schema, key)
        if value is not None:
            saved[f"{schema} {key}"] = value
    return saved


def restore(saved: dict) -> None:
    for ident, value in saved.items():
        schema, key = ident.split(" ", 1)
        run(["gsettings", "set", schema, key, value], timeout=5)


def _set(schema: str, key: str, value: str) -> None:
    journal.gsettings_set(schema, key, value, journal=False)


def desktop_id(*candidates: str) -> str:
    dirs = [
        paths.home() / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    ]
    for name in candidates:
        if any((d / name).exists() for d in dirs):
            return name
    return ""


def dock_for(cfg: Config, editor: str, browser_kind: str) -> list[str]:
    ids = {
        "editor": desktop_id(*{"cursor": ["cursor.desktop"], "code": ["code_code.desktop", "code.desktop"]}.get(editor, [f"{editor}.desktop"])),
        "terminal": desktop_id("org.wezfurlong.wezterm.desktop", "org.gnome.Terminal.desktop", "org.gnome.Ptyxis.desktop"),
        "browser": desktop_id(
            *{
                "chromium": ["chromium_chromium.desktop", "chromium.desktop"],
                "chrome": ["google-chrome.desktop"],
                "firefox": ["firefox_firefox.desktop", "firefox.desktop"],
                "brave": ["brave-browser.desktop"],
            }.get(browser_kind, [])
        ),
        "files": desktop_id("org.gnome.Nautilus.desktop"),
        "control": desktop_id("io.github.ambartsumov.ControlCenter.desktop"),
    }
    out = []
    for role in cfg.get("workstation.dock", ["editor", "terminal", "browser", "files", "control"]):
        ident = ids.get(role, role if str(role).endswith(".desktop") else "")
        if ident and ident not in out:
            out.append(ident)
    return out


def write_wallpaper(cfg: Config) -> Path:
    """Render the restrained workstation wallpaper from theme tokens (SVG, no binaries)."""
    bg = cfg.get("theme.background", "#0e1116")
    surface = cfg.get("theme.surface", "#161b22")
    accent = cfg.get("theme.accent", "#5b9cf5")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 3840 2160" preserveAspectRatio="xMidYMid slice">
  <defs>
    <radialGradient id="g" cx="72%" cy="18%" r="85%">
      <stop offset="0" stop-color="{surface}"/>
      <stop offset="1" stop-color="{bg}"/>
    </radialGradient>
    <radialGradient id="a" cx="78%" cy="12%" r="42%">
      <stop offset="0" stop-color="{accent}" stop-opacity="0.16"/>
      <stop offset="1" stop-color="{accent}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="3840" height="2160" fill="{bg}"/>
  <rect width="3840" height="2160" fill="url(#g)"/>
  <rect width="3840" height="2160" fill="url(#a)"/>
  <rect x="320" y="1876" width="96" height="6" rx="3" fill="{accent}" opacity="0.85"/>
</svg>
"""
    path = paths.ensure(paths.state_dir()) / "wallpaper.svg"
    if not path.exists() or path.read_text() != svg:
        path.write_text(svg)
    return path


def apply_workstation(cfg: Config, dock: list[str]) -> None:
    names = [str(n) for n in cfg.get("workstation.workspaces", [])]
    if names:
        _set("org.gnome.mutter", "dynamic-workspaces", "false")
        _set("org.gnome.desktop.wm.preferences", "num-workspaces", str(len(names)))
        _set("org.gnome.desktop.wm.preferences", "workspace-names", _fmt(names))
        if cfg.get("workstation.workspace_shortcuts", True):
            _set("org.gnome.shell.extensions.dash-to-dock", "hot-keys", "false")
            for n in range(1, min(len(names), 9) + 1):
                _set("org.gnome.shell.keybindings", f"switch-to-application-{n}", "@as []")
                key = f"switch-to-workspace-{n}"
                keep = [b for b in _list(journal.gsettings_get("org.gnome.desktop.wm.keybindings", key)) if norm(str(b)) != norm(f"<Super>{n}")]
                _set("org.gnome.desktop.wm.keybindings", key, _fmt(keep + [f"<Super>{n}"]))
    if dock and cfg.get("workstation.dock_enabled", True):
        _set("org.gnome.shell", "favorite-apps", _fmt(dock))
    if cfg.get("workstation.wallpaper", True):
        uri = "'" + write_wallpaper(cfg).as_uri() + "'"
        _set("org.gnome.desktop.background", "picture-uri", uri)
        _set("org.gnome.desktop.background", "picture-uri-dark", uri)
        _set("org.gnome.desktop.background", "picture-options", "'zoom'")
        _set("org.gnome.desktop.background", "primary-color", "'" + cfg.get("theme.background", "#0e1116") + "'")


# ── helper extension ────────────────────────────────────────────────────────


def shell_started() -> float:
    """When the running GNOME Shell started (0 when unknown)."""
    import os
    import time

    res = run(["pgrep", "-u", str(os.getuid()), "-o", "-x", "gnome-shell"], timeout=5)
    pid = res.out.split()[0] if res.ok and res.out.split() else ""
    age = run(["ps", "-o", "etimes=", "-p", pid], timeout=5).out.strip() if pid else ""
    return time.time() - int(age) if age.isdigit() else 0.0


def extension_state() -> str:
    """'active', 'stale' (running, but a newer version is on disk and loads at the next
    login), 'installed' (needs re-login or enable), or 'missing'."""
    target = paths.home() / ".local/share/gnome-shell/extensions" / EXTENSION_UUID
    res = run(["gnome-extensions", "info", EXTENSION_UUID], timeout=5)
    if not res.ok:
        return "installed" if target.exists() else "missing"
    if not re.search(r"State:\s*(ACTIVE|ENABLED)", res.out):
        return "installed"
    try:  # a Wayland session cannot reload extension code: what runs is what was there at login
        started = shell_started()
        if started and (target / "extension.js").stat().st_mtime > started:
            return "stale"
    except OSError:
        pass
    return "active"