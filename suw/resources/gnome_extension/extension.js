// Unified Workstation helper for GNOME Shell (45+).
//
// Does two small things, driven by ~/.local/state/suw/shell.json (written by `suw mode`):
//   1. shows a minimal mode indicator in the top bar with a toggle;
//   2. for a short window after Workstation Mode starts, moves the freshly launched
//      workstation windows to their workspaces (each rule is used once).
//   3. remembers which windows it placed ("owned" by Workstation Mode) so that leaving the
//      mode can close exactly those and never a window the user opened on their own.
// It never moves windows the user opens later.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const STATE_DIR = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'state', 'suw']);
const STATE_FILE = 'shell.json';
const RETRY_MS = 300;
const RETRIES = 12;

export default class SuwHelper extends Extension {
    enable() {
        this._state = {mode: 'DEFAULT', rules: [], until: 0, suw: ''};
        this._timeouts = new Set();
        this._closeId = null;  // null until the first load: a stale request is never replayed
        this._owned = new Set();  // ids of windows Workstation Mode opened in this session

        this._button = new PanelMenu.Button(0.0, 'Unified Workstation', false);
        this._label = new St.Label({
            text: '○',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'suw-indicator suw-indicator-idle',
        });
        this._button.add_child(this._label);

        this._toggleItem = new PopupMenu.PopupMenuItem('Switch to Work Mode');
        this._toggleItem.connect('activate', () => this._suw(['mode', 'toggle']));
        this._button.menu.addMenuItem(this._toggleItem);
        const center = new PopupMenu.PopupMenuItem('Open Control Center');
        center.connect('activate', () => this._suw(['ui']));
        this._button.menu.addMenuItem(center);
        Main.panel.addToStatusArea(this.uuid, this._button);

        // Watch the directory: the state file is replaced atomically (rename).
        this._dir = Gio.File.new_for_path(STATE_DIR);
        try {
            this._monitor = this._dir.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitorId = this._monitor.connect('changed', (_m, file) => {
                if (file && file.get_basename() === STATE_FILE)
                    this._reload();
            });
        } catch (e) {
            console.warn(`suw-helper: cannot watch ${STATE_DIR}: ${e.message}`);
        }
        this._createdId = global.display.connect('window-created', (_d, win) => this._onWindow(win, 0));
        this._reload();
    }

    disable() {
        if (this._createdId) {
            global.display.disconnect(this._createdId);
            this._createdId = 0;
        }
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
        }
        for (const id of this._timeouts)
            GLib.source_remove(id);
        this._timeouts.clear();
        this._button?.destroy();
        this._button = null;
        this._label = null;
        this._toggleItem = null;
        this._dir = null;
        this._state = null;
        this._owned = null;
    }

    _suw(args) {
        const bin = this._state?.suw || GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'suw']);
        try {
            Gio.Subprocess.new([bin, ...args], Gio.SubprocessFlags.NONE);
        } catch (e) {
            Main.notify('Unified Workstation', `Could not run suw: ${e.message}`);
        }
    }

    _reload() {
        const file = this._dir?.get_child(STATE_FILE);
        if (!file)
            return;
        file.load_contents_async(null, (source, result) => {
            if (!this._state)
                return;
            try {
                const [, bytes] = source.load_contents_finish(result);
                const data = JSON.parse(new TextDecoder().decode(bytes));
                this._state = {
                    mode: data.mode ?? 'DEFAULT',
                    label: data.label ?? 'WORK',
                    rules: Array.isArray(data.rules) ? data.rules.map(r => ({...r})) : [],
                    until: Number(data.until ?? 0),
                    suw: data.suw ?? '',
                };
                const closeId = Number(data.close_id ?? 0);
                // `close`: windows that are Workstation Mode's by construction (its own terminal
                // classes). `close_owned`: classes shared with the user's own windows (editor,
                // browser) — only the windows this helper placed are closed. The two lists are
                // separate keys so that a helper too old to track ownership never sees the second.
                const rules = [
                    ...(Array.isArray(data.close) ? data.close : []),
                    ...(Array.isArray(data.close_owned) ? data.close_owned.map(r => ({...r, owned: true})) : []),
                ];
                if (this._closeId !== null && closeId > this._closeId && rules.length > 0)
                    this._closeWindows(rules, closeId);
                this._closeId = closeId;
            } catch (_e) {
                this._state = {...this._state, mode: 'DEFAULT', rules: [], until: 0};
            }
            this._render();
        });
    }

    _render() {
        if (!this._label)
            return;
        const work = this._state.mode === 'WORKSTATION';
        this._label.text = work ? `● ${this._state.label || 'WORK'}` : '○';
        if (work)
            this._label.remove_style_class_name('suw-indicator-idle');
        else
            this._label.add_style_class_name('suw-indicator-idle');
        this._toggleItem.label.text = work ? 'Switch to Default Mode' : 'Switch to Work Mode';
    }

    _matches(rule, win) {
        const ids = [win.get_wm_class(), win.get_wm_class_instance(), win.get_gtk_application_id(), win.get_sandboxed_app_id?.()]
            .filter(v => typeof v === 'string' && v.length > 0);
        const title = win.get_title() ?? '';
        try {
            if (rule.class && !ids.some(id => new RegExp(rule.class, 'i').test(id)))
                return false;
            if (rule.title && !new RegExp(rule.title).test(title))
                return false;
        } catch (_e) {
            return false;
        }
        return Boolean(rule.class || rule.title);
    }

    // Ask matching windows to close, exactly as if the user had clicked their close button:
    // each application still gets to prompt about unsaved documents.
    // A rule marked `owned` only applies to windows this extension placed for Workstation Mode.
    _closeTargets(rules) {
        const targets = [];
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!win || win.get_window_type() !== Meta.WindowType.NORMAL)
                continue;
            if (rules.some(rule => this._matches(rule, win) && (!rule.owned || this._owned.has(win.get_id()))))
                targets.push(win);
        }
        return targets;
    }

    _closeWindows(rules, closeId) {
        const now = global.get_current_time();
        for (const win of this._closeTargets(rules))
            win.delete(now);
        // Never force: a few seconds later just report what is still open (unsaved work, a prompt).
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 4000, () => {
            this._timeouts.delete(id);
            if (!this._state)
                return GLib.SOURCE_REMOVE;
            const remaining = this._closeTargets(rules).map(win => win.get_title() ?? '');
            const report = JSON.stringify({close_id: closeId, remaining});
            try {
                this._dir.get_child('shell-report.json').replace_contents(
                    new TextEncoder().encode(report), null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
            } catch (e) {
                console.warn(`suw-helper: cannot write the close report: ${e.message}`);
            }
            return GLib.SOURCE_REMOVE;
        });
        this._timeouts.add(id);
    }

    _onWindow(win, attempt) {
        const state = this._state;
        if (!state || state.mode !== 'WORKSTATION' || state.rules.length === 0)
            return;
        if (Date.now() / 1000 > state.until)
            return;
        if (win.get_window_type() !== Meta.WindowType.NORMAL)
            return;

        const index = state.rules.findIndex(rule => this._matches(rule, win));
        if (index >= 0) {
            const [rule] = state.rules.splice(index, 1);
            const manager = global.workspace_manager;
            const target = Math.min(Math.max(0, Number(rule.workspace) || 0), manager.get_n_workspaces() - 1);
            win.change_workspace_by_index(target, false);
            this._owned.add(win.get_id());
            return;
        }
        // On Wayland the app id and title arrive shortly after the window is created.
        if (attempt < RETRIES) {
            const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, RETRY_MS, () => {
                this._timeouts.delete(id);
                if (this._state && win.get_compositor_private())
                    this._onWindow(win, attempt + 1);
                return GLib.SOURCE_REMOVE;
            });
            this._timeouts.add(id);
        }
    }
}
