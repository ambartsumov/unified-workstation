"""First run: from a fresh install to a working setup, without editing a file.

`plan()` turns the assistant's answers into a list of concrete steps and says exactly what
each one touches; `apply()` runs them. Nothing optional is done unless it was chosen, and a
"Personal computer" setup changes nothing outside the product's own folders: no shell files,
no shortcuts, no background service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import platform as osplatform
from . import backup, capabilities, config, events, inventory, paths, settings, tomlw
from .config import Config

PERSONAL, WORKSTATION = "personal", "workstation"
# Optional integration steps a Workstation setup may add. id → (bootstrap step, default on)
INTEGRATIONS = {
    "ssh": ("step_ssh", True),            # `ssh home` / `ssh cloud` names in the user's SSH configuration
    "desktop": ("step_desktop", True),    # global shortcut, launcher entry, window placement helper
    "terminal": ("step_terminal", False),  # clipboard-over-SSH settings for WezTerm
    "shell": ("step_shell", False),       # aliases and PATH in the shell start-up file
    "tmux": ("step_tmux", False),         # clipboard settings for tmux
    "git": ("step_git", False),           # recommended Git defaults (one include line)
}


@dataclass
class Step:
    id: str
    touches: list[str]
    optional: bool = False
    available: bool = True
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def state(cfg: Config | None = None) -> dict:
    cfg = cfg or config.load()
    return {
        "done": bool(cfg.get("onboarding.done", False)),
        "kind": str(cfg.get("onboarding.kind", PERSONAL)),
        "existing_setup": config.local_file().exists(),
        "suggested_workspace": str(suggested_workspace(cfg)),
        "device": cfg.device,
    }


def suggested_workspace(cfg: Config):
    """An existing work folder wins over the platform default, so nothing is duplicated."""
    if config.local_file().exists() and cfg.get("work.path"):
        current = paths.expand(str(cfg.get("work.path")))
        if current.is_dir():
            return current
    return paths.default_workspace()


def plan(choices: dict, cfg: Config | None = None) -> list[Step]:
    cfg = cfg or config.load()
    parts = capabilities.by_id(capabilities.components(with_versions=False))
    kind = choices.get("kind", PERSONAL)
    workspace = str(choices.get("workspace") or suggested_workspace(cfg))
    steps = [
        Step("settings", [_show(paths.config_dir()), _show(paths.state_dir())]),
        Step("workspace", [workspace + ("" if paths.expand(workspace).exists() else "  (created; an existing folder is never changed)")]),
    ]
    if kind != WORKSTATION:
        return steps
    if choices.get("sync", True):
        steps.append(Step("sync", ["a private Syncthing configuration inside the application's data folder", "a background sync service for your user account"], True, parts["syncthing"].installed, "" if parts["syncthing"].installed else "Syncthing is not installed"))
    if choices.get("peripherals", False):
        steps.append(Step("peripherals", ["a private Deskflow profile and certificate inside the application's data folder", "a background sharing service for your user account"], True, parts["deskflow"].installed, "" if parts["deskflow"].installed else "Deskflow is not installed"))
    steps.append(Step("service", ["a background service for your user account (status, health, reconnect)"]))
    wanted = choices.get("integrations")
    for ident, (_step, default) in INTEGRATIONS.items():
        if (ident in wanted) if isinstance(wanted, list) else default:
            steps.append(Step(f"integration.{ident}", _touches(ident), True, _integration_available(ident, parts)))
    if choices.get("autostart", True) and osplatform.current().service_manager not in ("systemd", "launchd"):
        steps.append(Step("autostart", [osplatform.current().autostart().mechanism]))
    return steps


def _show(path) -> str:
    text = str(path)
    home = str(paths.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def _touches(ident: str) -> list[str]:
    system = paths.platform()
    return {
        "ssh": ["~/.ssh/config: one Include line inside a marked block", "~/.ssh/suw.conf and ~/.ssh/suw_known_hosts (owned by this product)"],
        "desktop": {"linux": ["a keyboard shortcut and a launcher entry", "a small GNOME Shell helper extension (GNOME only)"], "macos": ["~/.hammerspoon/init.lua: a marked block (only if Hammerspoon is installed)"], "windows": ["a Start menu entry"]}[system],
        "terminal": ["~/.wezterm.lua (only if you have none yet)"],
        "shell": ["your shell start-up file: a marked block with aliases and PATH"],
        "tmux": ["~/.tmux.conf: a marked block with clipboard settings"],
        "git": ["~/.gitconfig: one include line"],
    }[ident]


def _integration_available(ident: str, parts: dict) -> bool:
    if paths.platform() == "windows":
        return ident in ("ssh", "git")
    need = {"terminal": "wezterm", "tmux": "tmux", "git": "git", "ssh": "ssh"}.get(ident)
    return parts[need].installed if need else True


def initialise(cfg: Config | None = None) -> bool:
    """The product's own files: local settings, a stable identity, this computer in the inventory."""
    changed = False
    for folder in (paths.config_dir(), paths.state_dir(), paths.runtime_dir()):
        paths.ensure(folder)
    local = config.local_file()
    if not local.exists():
        tomlw.dump(local, {"device": {"name": config.default_device_name()}, "config": {"schema": 2}}, "This computer only. Change settings in the application, not here.")
        changed = True
    cfg = config.load()
    device_id = str(cfg.get("device.id", "") or "")
    if not device_id:
        device_id = inventory.new_device_id(cfg.device)
        config.set_value("device.id", device_id, "local")
        changed = True
    paths.ensure(paths.shared_dir())
    inv = inventory.load()
    if inv["devices"].get(cfg.device, {}).get("id") != device_id:
        inventory.ensure_device(inv, cfg.device, role="workstation", platform=paths.platform(), id=device_id)
        inventory.save(inv)
        changed = True
    return changed


def apply(choices: dict) -> dict:
    """Run the plan. Each step reports ok / skipped / failed; one failed optional step never
    undoes the others, and the result says what the user can do about it."""
    from . import bootstrap

    results: list[dict] = []

    def done(ident: str, status: str, note: str = "") -> None:
        results.append({"id": ident, "status": status, "note": note})

    if config.local_file().exists():
        backup.snapshot("before-setup")
    initialise()
    done("settings", "ok")

    kind = WORKSTATION if choices.get("kind") == WORKSTATION else PERSONAL
    workspace = str(choices.get("workspace") or suggested_workspace(config.load()))
    problem = settings.workspace_problem(workspace)
    if problem:
        done("workspace", "failed", problem)
        return {"ok": False, "steps": results}
    target = paths.expand(workspace)
    try:
        target.mkdir(parents=True, exist_ok=True)  # never journaled: undoing setup must not remove user data
    except OSError as exc:
        done("workspace", "failed", str(exc))
        return {"ok": False, "steps": results}
    config.set_value("work.path", workspace, "local")
    config.set_value("onboarding.kind", kind, "local")
    if choices.get("language") in ("auto", "en", "ru"):
        config.set_value("general.language", choices["language"], "local")
    done("workspace", "ok", str(target))

    planned = {step.id: step for step in plan({**choices, "kind": kind, "workspace": workspace})}
    if kind == WORKSTATION:
        config.set_value("work.enabled", bool(choices.get("sync", True)), "local")
        config.set_value("peripherals.enabled", bool(choices.get("peripherals", False)), "local")
        cfg = config.load()
        for ident, runner in (("sync", bootstrap.step_work), ("peripherals", bootstrap.step_peripherals), ("service", bootstrap.step_service)):
            step = planned.get(ident)
            if step is None:
                continue
            if not step.available:
                done(ident, "skipped", step.reason)
                continue
            try:
                _title, note = runner(cfg)
                done(ident, "failed" if note.startswith("FAILED") else "ok", note)
            except Exception as exc:  # one integration never takes the setup down
                done(ident, "failed", str(exc)[:200])
        for ident, (step_name, _default) in INTEGRATIONS.items():
            step = planned.get(f"integration.{ident}")
            if step is None:
                continue
            if not step.available:
                done(step.id, "skipped", "not available on this computer")
                continue
            try:
                _title, note = getattr(bootstrap, step_name)(config.load())
                done(step.id, "ok", note)
            except Exception as exc:
                done(step.id, "failed", str(exc)[:200])
        if "autostart" in planned:
            ok = osplatform.current().set_autostart([str(paths.launcher()), "start"], True)
            config.set_value("general.autostart", ok, "local")
            done("autostart", "ok" if ok else "failed")
    else:
        config.set_value("work.enabled", False, "local")
        config.set_value("peripherals.enabled", False, "local")

    config.set_value("onboarding.done", True, "local")
    events.emit("onboarding.completed", f"first-run setup finished ({kind})")
    return {"ok": all(r["status"] != "failed" or planned.get(r["id"], Step(r["id"], [])).optional for r in results), "kind": kind, "steps": results}


def reset() -> None:
    """Show the first-run assistant again. Changes no other setting."""
    config.set_value("onboarding.done", False, "local")
