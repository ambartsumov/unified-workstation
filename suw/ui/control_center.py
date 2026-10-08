"""Control Center — one compact window (GTK 4 / libadwaita).

Answers in three seconds: am I in Workstation Mode, is Git synced, are Home and Cloud online,
which project am I in, is anything broken. No charts, no dashboard. Every state is shown as a
symbol plus a word, never colour alone. Keyboard: Esc closes, Ctrl+M toggles mode, Ctrl+R syncs.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core import config, modes, status  # noqa: E402
from ..core.gitsync import LABELS, Status  # noqa: E402
from ..core.health import gpu_label  # noqa: E402
from .text import ago  # noqa: E402

APP_ID = "io.github.ambartsumov.ControlCenter"
REFRESH_SECONDS = 5


def css(cfg: config.Config) -> str:
    t = {k: cfg.get(f"theme.{k}") for k in ("background", "surface", "text", "muted", "accent", "success", "warning", "error")}
    return f"""
window.suw {{ background-color: {t['background']}; color: {t['text']}; }}
.suw-brand {{ font-size: 11pt; font-weight: 700; letter-spacing: 1.5px; }}
.suw-mode {{ font-size: 9.5pt; font-weight: 600; color: {t['muted']}; }}
.suw-mode.work {{ color: {t['accent']}; }}
.suw-section {{ font-size: 8pt; font-weight: 700; letter-spacing: 1.2px; color: {t['muted']}; margin-top: 2px; }}
.suw-card {{ background-color: {t['surface']}; border-radius: 10px; padding: 12px 14px; }}
.suw-name {{ font-weight: 600; }}
.suw-note {{ color: {t['muted']}; }}
.suw-project {{ font-size: 11pt; font-weight: 700; }}
.suw-ok {{ color: {t['success']}; }}
.suw-warn {{ color: {t['warning']}; }}
.suw-err {{ color: {t['error']}; }}
.suw-idle {{ color: {t['muted']}; }}
.suw-accent {{ color: {t['accent']}; }}
.suw-mono {{ font-family: monospace; font-size: 9.5pt; }}
.suw-footer {{ color: {t['muted']}; font-size: 9pt; }}
button.suw-action {{ background: {t['surface']}; color: {t['text']}; border-radius: 8px; padding: 6px 14px; font-weight: 600; }}
button.suw-action:hover {{ background: alpha({t['accent']}, 0.18); }}
button.suw-action:focus-visible {{ outline: 2px solid {t['accent']}; outline-offset: 1px; }}
switch:checked {{ background-color: {t['accent']}; }}
"""


def label(text: str = "", *classes: str, xalign: float = 0.0, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.set_ellipsize(3)
    widget.set_selectable(selectable)
    for name in classes:
        widget.add_css_class(name)
    return widget


class Row(Gtk.Box):
    """● Name      note"""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.dot = label("◌", "suw-idle")
        self.name = label("", "suw-name")
        self.name.set_width_chars(9)
        self.note = label("", "suw-note")
        self.note.set_hexpand(True)
        for child in (self.dot, self.name, self.note):
            self.append(child)

    def set(self, state: str, name: str, note: str) -> None:
        symbol, klass = {"ok": ("●", "suw-ok"), "off": ("○", "suw-err"), "idle": ("◌", "suw-idle")}[state]
        self.dot.set_label(symbol)
        for old in ("suw-ok", "suw-err", "suw-idle"):
            self.dot.remove_css_class(old)
        self.dot.add_css_class(klass)
        self.name.set_label(name)
        self.note.set_label(note)
        self.set_tooltip_text(f"{name}: {note}")


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, cfg: config.Config) -> None:
        super().__init__(application=app, title=cfg.get("brand.control_center", "Control Center"))
        self.cfg = cfg
        self.busy = False
        self.set_default_size(400, -1)
        self.set_resizable(False)
        self.add_css_class("suw")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(18)

        # header: brand, mode, switch
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.set_hexpand(True)
        titles.append(label(cfg.brand.upper(), "suw-brand"))
        self.mode_label = label("", "suw-mode")
        titles.append(self.mode_label)
        header.append(titles)
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.set_tooltip_text("Workstation Mode (Ctrl+M)")
        self.switch.update_property([Gtk.AccessibleProperty.LABEL], ["Workstation Mode"])
        self.switch_handler = self.switch.connect("state-set", self.on_switch)
        header.append(self.switch)
        root.append(header)

        self.workstations = self.card(root, "WORKSTATIONS")
        self.servers = self.card(root, "SERVERS")
        self.home_row, self.cloud_row = Row(), Row()
        self.servers.append(self.home_row)
        self.servers.append(self.cloud_row)

        project = self.card(root, "PROJECT")
        self.project_name = label("", "suw-project")
        self.project_branch = label("", "suw-note", "suw-mono")
        self.project_state = label("", "suw-name")
        self.project_others = label("", "suw-note")
        for child in (self.project_name, self.project_branch, self.project_state, self.project_others):
            project.append(child)

        system = self.card(root, "SYSTEM")
        self.system_label = label("", "suw-mono")
        system.append(self.system_label)

        self.health = label("", "suw-name")
        self.health.set_wrap(True)
        self.health.set_ellipsize(0)
        root.append(self.health)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.footer_note = label("", "suw-footer")
        self.footer_note.set_hexpand(True)
        footer.append(self.footer_note)
        self.sync_button = Gtk.Button(label="Sync now")
        self.sync_button.add_css_class("suw-action")
        self.sync_button.set_tooltip_text("Reconcile all repositories (Ctrl+R)")
        self.sync_button.connect("clicked", self.on_sync)
        footer.append(self.sync_button)
        root.append(footer)
        self.set_content(root)

        keys = Gtk.ShortcutController()
        keys.set_scope(Gtk.ShortcutScope.GLOBAL)
        for trigger, callback in (
            ("Escape", lambda *_: self.close() or True),
            ("<Control>w", lambda *_: self.close() or True),
            ("<Control>m", lambda *_: self.switch.activate() or True),
            ("<Control>r", lambda *_: self.on_sync() or True),
        ):
            keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger), Gtk.CallbackAction.new(callback)))
        self.add_controller(keys)

        self.refresh()
        self.timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)
        self.connect("close-request", self.on_close)

    def card(self, root: Gtk.Box, title: str) -> Gtk.Box:
        root.append(label(title, "suw-section"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("suw-card")
        root.append(box)
        return box

    def on_close(self, *_args) -> bool:
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        return False

    # ── actions (off the UI thread) ─────────────────────────────────────────

    def background(self, work, note: str) -> None:
        if self.busy:
            return
        self.busy = True
        self.footer_note.set_label(note)
        self.sync_button.set_sensitive(False)
        self.switch.set_sensitive(False)

        def runner() -> None:
            try:
                work()
            finally:
                GLib.idle_add(self.done)

        threading.Thread(target=runner, daemon=True).start()

    def done(self) -> bool:
        self.busy = False
        self.sync_button.set_sensitive(True)
        self.switch.set_sensitive(True)
        self.refresh()
        return False

    def on_switch(self, _switch: Gtk.Switch, wanted: bool) -> bool:
        if wanted == modes.is_work():
            return False
        cfg = config.load()
        self.background((lambda: modes.on(cfg)) if wanted else (lambda: modes.off(cfg)), "Starting Workstation Mode…" if wanted else "Returning to Default Mode…")
        return False

    def on_sync(self, *_args) -> None:
        def work() -> None:
            from ..core import syncer
            from ..daemon import client

            reply = client.call("sync", timeout=200)
            if not (reply and reply.get("ok")):
                syncer.sync_all(config.load())

        self.background(work, "Syncing…")

    # ── rendering ───────────────────────────────────────────────────────────

    def refresh(self) -> bool:
        def collect() -> None:
            try:
                snap = status.build(config.load())
            except Exception as exc:  # keep the window alive whatever happens underneath
                GLib.idle_add(self.footer_note.set_label, f"status unavailable: {exc}")
                return
            GLib.idle_add(self.render, snap)

        threading.Thread(target=collect, daemon=True).start()
        return True

    def render(self, snap: dict) -> bool:
        work = snap["mode"] != "DEFAULT"
        self.mode_label.set_label("WORK MODE  ●" if work else "DEFAULT MODE  ○")
        (self.mode_label.add_css_class if work else self.mode_label.remove_css_class)("work")
        if not self.busy and self.switch.get_active() != work:
            self.switch.handler_block(self.switch_handler)
            self.switch.set_active(work)
            self.switch.handler_unblock(self.switch_handler)

        while (child := self.workstations.get_first_child()) is not None:
            self.workstations.remove(child)
        for ws in snap["workstations"]:
            row = Row()
            if ws["self"]:
                row.set("ok", ws["name"], "this machine")
            elif not ws["enrolled"]:
                row.set("idle", ws["name"], "not set up yet")
            elif ws["online"]:
                row.set("ok", ws["name"], "online")
            else:
                seen = ago(ws.get("last_seen"))
                row.set("idle", ws["name"], "offline" + (f" • last seen {seen}" if seen else ""))
            self.workstations.append(row)

        for row, name in ((self.home_row, "home"), (self.cloud_row, "cloud")):
            server = snap["servers"][name]
            state_word = server.get("status", "UNKNOWN")
            if state_word == "NOT_CONFIGURED":
                row.set("idle", name, "not configured")
            elif state_word == "UNKNOWN":
                row.set("idle", name, "checking…")
            elif state_word not in ("ONLINE", "DEGRADED"):
                row.set("off", name, state_word.lower().replace("_", " "))
            else:
                bits = [state_word.lower()]
                gpus = server.get("gpus") or []
                if gpus:
                    bits += [gpu_label(gpus), f"{round(max(g['vram_mb'] for g in gpus) / 1024)}GB"]
                elif server.get("cpu_pct") is not None:
                    bits += [f"CPU {int(server['cpu_pct'])}%", f"RAM {int(server.get('ram_used_pct') or 0)}%"]
                row.set("ok", name, " • ".join(bits))

        project = snap.get("project")
        for klass in ("suw-ok", "suw-warn", "suw-err", "suw-idle", "suw-accent"):
            self.project_state.remove_css_class(klass)
        if project:
            symbol, word = LABELS[Status(project["status"])]
            self.project_name.set_label(project["name"])
            self.project_branch.set_label(f"branch: {project.get('branch') or '–'}")
            parts = [f"{symbol} {word}"]
            if project.get("ahead"):
                parts.append(f"↑{project['ahead']}")
            if project.get("behind"):
                parts.append(f"↓{project['behind']}")
            parts.append(f"{project['dirty']} uncommitted" if project.get("dirty") else "clean")
            self.project_state.set_label(" • ".join(parts))
            self.project_state.add_css_class(
                {
                    "SYNCED": "suw-ok", "CLEAN": "suw-ok", "CONFLICT": "suw-err", "RECOVERY_REQUIRED": "suw-err", "PUSH_FAILED": "suw-err",
                    "DIVERGED": "suw-warn", "AUTH_REQUIRED": "suw-warn", "POLICY_BLOCKED": "suw-warn", "OFFLINE": "suw-idle", "LOCAL_COMMITTED": "suw-idle",
                }.get(project["status"], "suw-accent")
            )
        else:
            self.project_name.set_label("No project")
            self.project_branch.set_label("add repositories under ~/Projects/active")
            self.project_state.set_label("")
        others = [r for r in snap["projects"] if not project or r["path"] != project["path"]]
        pending = [r["name"] for r in others if r["status"] not in ("SYNCED", "CLEAN")]
        sync = snap.get("sync", {})
        self.project_others.set_visible(True)
        self.project_others.set_label(
            f"Projects {sync.get('managed', len(snap['projects']))} • Pending {sync.get('pending', 0)} • Conflicts {sync.get('conflicts', 0)}"
            + ("" if not pending else "\ncheck " + ", ".join(pending[:3]))
        )

        system = snap["system"]

        def pct(key: str) -> str:
            value = system.get(key)
            return f"{int(value)}%" if isinstance(value, (int, float)) else "–"

        line = f"CPU {pct('cpu_pct')}   RAM {pct('ram_used_pct')}   Disk {pct('disk_used_pct')}"
        if system.get("battery_pct") is not None:
            line += f"   Bat {pct('battery_pct')}"
        self.system_label.set_label(line)

        for klass in ("suw-ok", "suw-warn"):
            self.health.remove_css_class(klass)
        if snap["attention"]:
            label = "⚠ Needs attention\n" + "\n".join(f"   {item}" for item in snap["attention"][:5])
            if snap.get("still_works"):
                label += "\n✓ Unaffected: " + ", ".join(snap["still_works"][:3])
            self.health.set_label(label)
            self.health.add_css_class("suw-warn")
        else:
            clip = snap.get("clipboard", {})
            note = "" if clip.get("ssh") == "ONLINE" else "\n◌ SSH clipboard not proven: suw clipboard test"
            self.health.set_label("✓ Ready" + note)
            self.health.add_css_class("suw-ok")
        if not self.busy:
            self.footer_note.set_label("" if snap.get("daemon") else "daemon not running · local view")
        return False


def main() -> int:
    cfg = config.load()
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def activate(application: Adw.Application) -> None:
        window = application.get_active_window()
        if window is None:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
            provider = Gtk.CssProvider()
            provider.load_from_string(css(cfg)) if hasattr(provider, "load_from_string") else provider.load_from_data(css(cfg).encode())
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            window = Window(application, cfg)
        window.present()

    app.connect("activate", activate)
    return app.run(None)
