"""The question asked when leaving Workstation Mode from the keyboard shortcut.

It shows what is in flight (uncommitted work, Claude Code and SSH sessions, pending work
folder sync, conflict copies) and offers exactly two choices:
  "close"  — SAVE & CLOSE: checkpoint what is safe to checkpoint, then ask the windows
             Workstation Mode itself opened to close;
  "cancel" — nothing happens, the mode stays on (also Esc / closing the dialog).
Without a graphical session nothing can be asked, so the non-destructive "keep" is returned:
the mode is left, every window stays open. `suw mode off --save` does the same on request.
"""

from __future__ import annotations

from ..core.config import Config

CHOICES = ("close", "keep", "cancel")
NOTE = (
    "Save & Close commits and pushes projects that have automatic checkpoints on; other uncommitted work "
    "stays exactly as it is, on disk. The work folder keeps synchronising in the background. Only windows "
    "Workstation Mode opened are closed, each still asks about unsaved documents, and terminal sessions "
    "(tmux) keep running."
)


def summary_lines(summary: dict) -> list[tuple[str, str]]:
    """(label, value) rows for the dialog and for the terminal. Plain facts, no alarm."""
    rows = [
        ("Projects with uncommitted changes", str(summary.get("uncommitted", 0))),
        ("Claude Code sessions running", str(summary.get("claude", 0))),
        ("SSH sessions open", str(summary.get("ssh", 0))),
        ("Work folder items still syncing", str(summary.get("pending_sync", 0))),
    ]
    if summary.get("conflicts"):
        rows.append(("Conflict copies to review", str(summary["conflicts"])))
    return rows


def ask_macos(cfg: Config, summary: dict | None = None) -> str:
    """The same question as a native macOS dialog (no GTK on a Mac)."""
    from ..core.proc import run

    facts = "\n".join(f"{label}: {value}" for label, value in summary_lines(summary or {}))
    script = (
        "on run argv\n"
        '  set answer to display dialog (item 1 of argv) with title (item 2 of argv) buttons {"Cancel", "Save & Close"} default button "Save & Close" cancel button "Cancel"\n'
        "  return button returned of answer\n"
        "end run"
    )
    res = run(["osascript", "-e", script, f"Leave Workstation Mode\n\n{facts}\n\n{NOTE}", str(cfg.get("brand.name", "Workstation"))], timeout=600)
    if res.rc in (126, 127):
        return "keep"  # no dialog can be shown at all
    return "close" if res.ok and "Save & Close" in res.out else "cancel"


def ask(cfg: Config, summary: dict | None = None) -> str:
    import sys

    if sys.platform == "darwin":
        return ask_macos(cfg, summary)
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except Exception:
        return "keep"

    result = {"choice": "cancel"}
    app = Adw.Application(application_id="io.github.ambartsumov.ExitDialog")

    def activate(application) -> None:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        window = Adw.ApplicationWindow(application=application, title=str(cfg.get("brand.name", "Workstation")), resizable=False, default_width=440)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=26, margin_bottom=22, margin_start=26, margin_end=26)
        title = Gtk.Label(label="Leave Workstation Mode", xalign=0)
        title.add_css_class("title-2")
        note = Gtk.Label(label=NOTE, xalign=0, wrap=True)
        note.add_css_class("dim-label")
        box.append(title)
        if summary:
            grid = Gtk.Grid(column_spacing=18, row_spacing=4)
            for index, (label, value) in enumerate(summary_lines(summary)):
                grid.attach(Gtk.Label(label=label, xalign=0, hexpand=True), 0, index, 1, 1)
                number = Gtk.Label(label=value, xalign=1)
                number.add_css_class("numeric")
                grid.attach(number, 1, index, 1, 1)
            box.append(grid)
        box.append(note)

        def choose(value: str):
            def handler(_button) -> None:
                result["choice"] = value
                window.close()

            return handler

        close_all = Gtk.Button(label="Save & Close")
        close_all.add_css_class("suggested-action")
        close_all.add_css_class("pill")
        close_all.connect("clicked", choose("close"))
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("pill")
        cancel.connect("clicked", choose("cancel"))
        box.append(close_all)
        box.append(cancel)
        hint = Gtk.Label(label="Esc — stay in Workstation Mode", xalign=0.5)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(hint)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, _code, _state: (window.close(), True)[1] if keyval == 0xFF1B else False)
        window.add_controller(keys)
        window.set_content(box)
        window.present()
        close_all.grab_focus()

    app.connect("activate", activate)
    app.run(None)
    return result["choice"]
