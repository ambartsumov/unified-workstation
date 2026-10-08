"""`suw bootstrap` — idempotent, journaled machine setup for Linux and macOS.

Run it as often as you like: every step checks the current state first, touches only
SUW-managed blocks/files, backs up anything it modifies and records it for `suw rollback`.
Nothing here needs root. System packages are a separate, explicit step (installers/*/packages.sh).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import config, events, gitsync, inventory, journal, paths, supervise, tomlw
from .proc import have, run

RES = paths.resources()
Step = tuple[str, str]  # (title, outcome)


def _note(changed: bool, extra: str = "") -> str:
    return ("configured" if changed else "ok") + (f" · {extra}" if extra else "")


def step_dirs(cfg: config.Config) -> Step:
    for path in (paths.config_dir(), paths.state_dir(), paths.runtime_dir()):
        paths.ensure(path)
    made = []
    root = paths.home() / "Projects"
    for name in cfg.get("projects.layout", []):
        target = root / str(name)
        if not target.exists():
            target.mkdir(parents=True)
            made.append(str(name))
    return "Directories", _note(bool(made), "~/Projects/{" + ",".join(made) + "}" if made else "")


def step_config(cfg: config.Config) -> Step:
    """Local config + the shared configuration repository + this device in the inventory."""
    from ..integrations import tailscale

    changed = False
    local = config.local_file()
    if not local.exists():
        tomlw.dump(local, {"device": {"name": config.default_device_name()}}, "This machine only. Shared settings live in ./shared/ (git-synced).")
        changed = True
    shared = paths.ensure(paths.shared_dir(), 0o700)
    if not (shared / ".git").exists():
        run(["git", "init", "-q", "-b", "main", str(shared)], timeout=10)
        (shared / ".gitignore").write_text("*.bak\n*.tmp\n")
        (shared / "README.md").write_text(
            "# SUW shared configuration\n\nNon-secret settings shared by every workstation: inventory, URLs, layout.\n"
            "Synced automatically by `suwd`. Never store secrets here - use `suw secret set`.\n"
        )
        changed = True
    for name in ("shared.toml", "urls.toml"):
        target = shared / name
        if not target.exists():
            example = RES / "config" / name.replace(".toml", ".example.toml")
            shutil.copyfile(example, target)
            changed = True

    device = config.load().device
    device_id = str(config.load().get("device.id", "") or "")
    if not device_id:
        device_id = inventory.new_device_id(device)
        config.set_value("device.id", device_id, "local")
        changed = True
    inv = inventory.load()
    before = tomlw.dumps(inv)
    me = tailscale.status().get("self") or {}
    inventory.ensure_device(
        inv, device, role="workstation", platform=paths.platform(), tailscale_name=me.get("dns", ""), ssh_user=os.environ.get("USER", ""), id=device_id
    )
    if tomlw.dumps(inv) != before or not inventory.file().exists():
        inventory.save(inv)
        changed = True
    if changed:
        policy = gitsync.project_policy(shared, config.load().data)
        policy["enabled"] = True
        gitsync.checkpoint(shared, device, policy)
    return "Configuration", _note(changed, f"device '{device}'")


def step_launcher(cfg: config.Config) -> Step:
    changed = journal.symlink(paths.home() / ".local" / "bin" / "suw", paths.launcher())
    return "Command `suw`", _note(changed, "~/.local/bin/suw")


def step_shell(cfg: config.Config) -> Step:
    init = RES / "dotfiles" / "shell" / "init.sh"
    body = f'[ -r "{init}" ] && . "{init}"'
    changed, files = False, []
    login = os.path.basename(os.environ.get("SHELL", "zsh"))
    for rc, shell in ((".zshrc", "zsh"), (".bashrc", "bash")):
        path = paths.home() / rc
        if path.exists() or shell == login:
            changed |= journal.managed_block(path, body)
            files.append(rc)
    return "Shell integration", _note(changed, " ".join(files))


def step_tmux(cfg: config.Config) -> Step:
    changed = journal.managed_block(paths.home() / ".tmux.conf", f"source-file {RES / 'dotfiles' / 'tmux' / 'tmux.conf'}")
    if changed and have("tmux"):
        run(["tmux", "source-file", str(paths.home() / ".tmux.conf")], timeout=5)
    return "tmux (clipboard, OSC 52)", _note(changed)


def step_git(cfg: config.Config) -> Step:
    include = str(RES / "dotfiles" / "git" / "gitconfig")
    current = run(["git", "config", "--global", "--get-all", "include.path"], timeout=5).out.split("\n")
    if include in current:
        return "Git defaults", "ok"
    journal.backup_file(paths.home() / ".gitconfig")
    run(["git", "config", "--global", "--add", "include.path", include], timeout=5)
    journal.record("gitconfig", include, key="include.path", pattern="^" + re.escape(include) + "$")
    return "Git defaults", "configured · include.path"


def step_ssh(cfg: config.Config) -> Step:
    from ..integrations import ssh as sshcfg

    changed = sshcfg.apply(inventory.load(), config.load().device)
    return "SSH aliases", _note(bool(changed), "home / cloud / peers")


def step_terminal(cfg: config.Config) -> Step:
    existing = [paths.home() / ".wezterm.lua", paths.home() / ".config" / "wezterm" / "wezterm.lua"]
    if any(p.exists() for p in existing):
        ours = existing[1].exists() and "suw" in existing[1].read_text(errors="replace")
        return "Terminal profile", "ok" if ours else "kept your existing WezTerm config"
    source = RES / "dotfiles" / "wezterm" / "wezterm.lua"
    changed = journal.write_file(existing[1], f'-- Managed by suw. Edit {source} (shared) or replace this file to opt out.\nreturn dofile("{source}")\n')
    return "Terminal profile", _note(changed, "WezTerm" + ("" if have("wezterm") else " (not installed yet)"))


def step_prompt(cfg: config.Config) -> Step:
    if cfg.get("shell.prompt", "keep") != "starship":
        return "Prompt", "kept as is"
    target = paths.home() / ".config" / "starship.toml"
    if target.exists() and not target.is_symlink():
        return "Prompt", "kept your existing starship.toml"
    return "Prompt", _note(journal.symlink(target, RES / "dotfiles" / "starship" / "starship.toml"), "starship")


def step_service(cfg: config.Config) -> Step:
    launcher = paths.launcher()
    if supervise.needed():
        was = supervise.running("suwd")
        ok = supervise.start("suwd", [str(launcher), "daemon", "run"])
        return "Background daemon", ("could not be started" if not ok else _note(not was, "started by the application (no session service manager)"))
    if paths.platform() == "macos":
        label = "io.github.ambartsumov.suwd"
        plist = paths.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        text = (RES / "launchd" / f"{label}.plist").read_text().replace("@SUW@", str(launcher)).replace("@HOME@", str(paths.home()))
        changed = journal.write_file(plist, text)
        domain = f"gui/{os.getuid()}"
        loaded = run(["launchctl", "print", f"{domain}/{label}"], timeout=10).ok
        if changed and loaded:
            run(["launchctl", "bootout", f"{domain}/{label}"], timeout=10)
            loaded = False
        if not loaded:
            run(["launchctl", "bootstrap", domain, str(plist)], timeout=15)
            if not any(e["action"] == "service" and e["target"] == label for e in journal.entries()):
                journal.record("service", label, undo=["launchctl", "bootout", f"{domain}/{label}"])
        return "Background daemon", _note(changed, "LaunchAgent")
    unit = paths.home() / ".config" / "systemd" / "user" / "suwd.service"
    text = (RES / "systemd" / "suwd.service").read_text().replace("@SUW@", str(launcher))
    changed = journal.write_file(unit, text)
    if not have("systemctl"):
        return "Background daemon", "systemd not available"
    if changed:
        run(["systemctl", "--user", "daemon-reload"], timeout=15)
    enabled = run(["systemctl", "--user", "is-enabled", "suwd.service"], timeout=10).out.strip() == "enabled"
    active = run(["systemctl", "--user", "is-active", "suwd.service"], timeout=10).out.strip() == "active"
    if not enabled or not active:
        run(["systemctl", "--user", "enable", "--now", "suwd.service"], timeout=20)
        if not any(e["action"] == "service" and e["target"] == "suwd.service" for e in journal.entries()):
            journal.record("service", "suwd.service", undo=["systemctl", "--user", "disable", "--now", "suwd.service"])
    elif changed:
        run(["systemctl", "--user", "restart", "suwd.service"], timeout=20)
    return "Background daemon", _note(changed or not (enabled and active), "systemd user service")


def user_service(unit: str, label: str, subs: dict[str, str]) -> bool:
    """Install and start a per-user background service (systemd user unit / LaunchAgent)
    from its template. Idempotent; journaled so `suw rollback` stops and removes it."""
    subs = {"@SUW@": str(paths.launcher()), "@HOME@": str(paths.home()), **subs}

    def fill(text: str) -> str:
        for key, value in subs.items():
            text = text.replace(key, value)
        return text

    if supervise.needed():
        # No systemd / launchd in this session (Windows, some Linux sessions): the product
        # starts the component itself and restores it at sign-in.
        template = (RES / "systemd" / unit).read_text()
        line = next((ln.split("=", 1)[1] for ln in template.splitlines() if ln.startswith("ExecStart=")), "")
        env = dict(ln.split("=", 1)[1].split("=", 1) for ln in template.splitlines() if ln.startswith("Environment=") and "PATH=" not in ln)
        name = {"suwd.service": "suwd", "suw-syncthing.service": "syncthing", "suw-deskflow.service": "deskflow"}.get(unit, unit)
        was = supervise.running(name)
        argv = [fill(part) for part in line.split()]
        return bool(argv) and supervise.start(name, argv, env) and not was
    if paths.platform() == "macos":
        plist = paths.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        changed = journal.write_file(plist, fill((RES / "launchd" / f"{label}.plist").read_text()))
        domain = f"gui/{os.getuid()}"
        loaded = run(["launchctl", "print", f"{domain}/{label}"], timeout=10).ok
        if changed and loaded:
            run(["launchctl", "bootout", f"{domain}/{label}"], timeout=10)
            loaded = False
        if not loaded:
            run(["launchctl", "bootstrap", domain, str(plist)], timeout=15)
            if not any(e["action"] == "service" and e["target"] == label for e in journal.entries()):
                journal.record("service", label, undo=["launchctl", "bootout", f"{domain}/{label}"])
            return True
        return changed
    target = paths.home() / ".config" / "systemd" / "user" / unit
    changed = journal.write_file(target, fill((RES / "systemd" / unit).read_text()))
    if not have("systemctl"):
        return changed
    if changed:
        run(["systemctl", "--user", "daemon-reload"], timeout=15)
    enabled = run(["systemctl", "--user", "is-enabled", unit], timeout=10).out.strip() == "enabled"
    active = run(["systemctl", "--user", "is-active", unit], timeout=10).out.strip() == "active"
    if not enabled or not active:
        run(["systemctl", "--user", "enable", "--now", unit], timeout=30)
        if not any(e["action"] == "service" and e["target"] == unit for e in journal.entries()):
            journal.record("service", unit, undo=["systemctl", "--user", "disable", "--now", unit])
    elif changed:
        run(["systemctl", "--user", "restart", unit], timeout=30)
    return changed or not (enabled and active)


def step_work(cfg: config.Config) -> Step:
    """The shared work folder: private Syncthing identity, folder, ignore rules, service.
    Pairing with another workstation is a separate, confirmed step (`suw work pair`)."""
    from ..integrations import worksync

    title = "Shared work folder"
    if not worksync.enabled(cfg):
        return title, "switched off (work.enabled = false)"
    if not worksync.binary():
        return title, "Syncthing is not installed (installers/" + ("macos" if paths.platform() == "macos" else "ubuntu") + "/packages.sh)"
    base = worksync.root(cfg)
    created = not base.exists()
    base.mkdir(parents=True, exist_ok=True)  # never journaled: rollback must not remove user data
    inst = worksync.instance(cfg)
    changed = inst.generate() or created
    device_id = inst.device_id()
    inv = inventory.load()
    if device_id and inv["devices"].get(cfg.device, {}).get("syncthing_id") != device_id:
        inventory.ensure_device(inv, cfg.device, role="workstation", syncthing_id=device_id)
        inventory.save(inv)
        changed = True
    found = worksync.scan(cfg)
    changed = worksync.write_ignore(cfg, worksync.held_large(cfg, found)) or changed
    config_changed = inst.apply(worksync.peers(cfg, inv), trash_days=int(cfg.get("work.trash_days", 30)))
    started = user_service(worksync.SERVICE, worksync.LABEL, {"@SYNCTHING@": worksync.binary(), "@STHOME@": str(inst.home)})
    if config_changed and not started:
        worksync.service("restart")
    if not inst.wait_running(40):
        return title, "FAILED: the sync service did not start (suw work doctor)"
    worksync.publish_repos(cfg, found.repos)
    worksync.publish_manifest(cfg)
    waiting = [name for name, _ in worksync.unpaired(cfg, inv)]
    note = str(base).replace(str(paths.home()), "~") + (f"; '{waiting[0]}' is waiting: suw work pair {waiting[0]}" if waiting else "")
    return title, _note(changed or config_changed or started, note)


def step_peripherals(cfg: config.Config) -> Step:
    from ..integrations import peripherals

    title = "Keyboard/mouse sharing"
    if not peripherals.enabled(cfg):
        return title, "switched off (peripherals.enabled = false)"
    if not peripherals.core():
        return title, "Deskflow is not installed (installers/" + ("macos" if paths.platform() == "macos" else "ubuntu") + "/packages.sh)"
    changed = peripherals.prepare(cfg)
    if not peripherals.fingerprint():
        return title, "FAILED: could not create the TLS certificate (is openssl installed?)"
    changed = peripherals.publish(cfg) or changed
    started = user_service(peripherals.SERVICE, peripherals.LABEL, {})
    if changed and not started:
        peripherals.service("restart")
    waiting = [name for name, _ in peripherals.unpaired(cfg)]
    note = f"{peripherals.role(cfg)}, {peripherals.screen_name(cfg.device)}" + (f"; '{waiting[0]}' is waiting: suw peripherals pair {waiting[0]}" if waiting else "")
    return title, _note(changed or started, note)


def step_desktop(cfg: config.Config) -> Step:
    launcher = paths.launcher()
    if paths.platform() == "macos":
        lua = RES / "dotfiles" / "hammerspoon" / "suw.lua"
        body = f'SUW_BIN = "{launcher}"\ndofile("{lua}")'
        changed = journal.managed_block(paths.home() / ".hammerspoon" / "init.lua", body, comment="--")
        return "Menu bar + shortcut", _note(changed, "Hammerspoon" + ("" if Path("/Applications/Hammerspoon.app").exists() else " (app not installed yet)"))

    from ..integrations import gnome

    if not gnome.available():
        return "Desktop integration", "skipped (not a GNOME session)"
    notes = []
    name = cfg.get("brand.control_center", "Control Center")
    desktop = (
        "[Desktop Entry]\nType=Application\n"
        f"Name={name}\nComment=Workstation status and mode switch\n"
        f"Exec={launcher} ui\nIcon=utilities-system-monitor\nTerminal=false\nCategories=Utility;Development;\n"
        "StartupWMClass=io.github.ambartsumov.ControlCenter\nStartupNotify=false\n"
    )
    changed = journal.write_file(paths.home() / ".local/share/applications/io.github.ambartsumov.ControlCenter.desktop", desktop)
    for slug, title, command, key in (
        ("suw-toggle", "Toggle Workstation Mode", f"{launcher} mode toggle --ask", "workstation.shortcut"),
        ("suw-on", "Workstation Mode on", f"{launcher} mode on", "workstation.shortcut_on"),
        ("suw-off", "Workstation Mode off", f"{launcher} mode off --ask", "workstation.shortcut_off"),
        ("suw-control", name, f"{launcher} ui", "workstation.control_center_shortcut"),
    ):
        binding = str(cfg.get(key, ""))
        if not binding:
            continue
        ok, note = gnome.register_shortcut(slug, title, command, binding)
        notes.append(binding if ok else f"NOT bound: {note}")
    source = RES / "gnome_extension"
    target = paths.home() / ".local/share/gnome-shell/extensions" / gnome.EXTENSION_UUID
    if cfg.get("workstation.helper_extension", True) and have("gnome-extensions"):
        if journal.symlink(target, source):
            changed = True
        if gnome.extension_state() != "active":
            run(["gnome-extensions", "enable", gnome.EXTENSION_UUID], timeout=10)
            enabled = journal.gsettings_get("org.gnome.shell", "enabled-extensions") or ""
            if gnome.EXTENSION_UUID not in enabled:
                current = gnome._list(enabled)
                journal.gsettings_set("org.gnome.shell", "enabled-extensions", gnome._fmt(current + [gnome.EXTENSION_UUID]))
            notes.append("helper extension loads at next login")
    return "Desktop integration", _note(changed, "; ".join(notes))


STEPS = [step_dirs, step_config, step_launcher, step_shell, step_tmux, step_git, step_ssh, step_terminal, step_prompt, step_service, step_work, step_peripherals, step_desktop]


def preview(cfg: config.Config) -> dict[str, list[str]]:
    """What `suw bootstrap` would touch, without touching it (`--dry-run`)."""
    home = paths.home()

    def has_block(path) -> bool:
        try:
            return "suw managed block" in path.read_text(errors="replace")
        except OSError:
            return False

    login = os.path.basename(os.environ.get("SHELL", "zsh"))
    files, changes = [], []
    for rc, shell in ((".zshrc", "zsh"), (".bashrc", "bash")):
        if (home / rc).exists() or shell == login:
            files.append(f"~/{rc}")
            changes.append(f"~/{rc}: SUW managed block (aliases, PATH)" + (" — already present" if has_block(home / rc) else ""))
    files += ["~/.tmux.conf", "~/.gitconfig", "~/.ssh/config", "~/.ssh/suw.conf", "~/.ssh/suw_known_hosts"]
    changes.append("~/.tmux.conf: SUW managed block (clipboard / OSC 52)" + (" — already present" if has_block(home / ".tmux.conf") else ""))
    changes.append("~/.gitconfig: one `include.path` entry (git defaults)")
    changes.append("~/.ssh/config: SUW managed block with a single Include line" + (" — already present" if has_block(home / ".ssh" / "config") else ""))
    changes.append("~/.ssh/suw.conf, ~/.ssh/suw_known_hosts: rendered from the inventory (SUW-owned files)")
    changes.append("~/.local/bin/suw: symlink to this repository")
    changes.append("~/.config/suw/: configuration; ~/.local/state/suw/: state, history, backups")
    if paths.platform() == "macos":
        files.append("~/Library/LaunchAgents/io.github.ambartsumov.suwd.plist")
        changes.append("LaunchAgent io.github.ambartsumov.suwd (user-level daemon)")
        changes.append("~/.hammerspoon/init.lua: SUW managed block (menu bar, shortcut)")
    else:
        files.append("~/.config/systemd/user/suwd.service")
        changes.append("systemd user service suwd (no root)")
        changes.append(f"GNOME: shortcuts {cfg.get('workstation.shortcut')} and {cfg.get('workstation.control_center_shortcut')}, launcher entry, helper extension")
    changes.append("existing files are backed up (with checksum) before their first modification")
    return {"files": files, "changes": changes}


def run_all(only: list[str] | None = None) -> list[Step]:
    results: list[Step] = []
    for step in STEPS:
        if only and step.__name__.removeprefix("step_") not in only:
            continue
        cfg = config.load()  # earlier steps may have created configuration
        try:
            results.append(step(cfg))
        except Exception as exc:  # keep going: each step is independent
            results.append((step.__name__.removeprefix("step_"), f"FAILED: {exc}"))
            events.emit("install.error", f"{step.__name__}: {exc}", "error")
    events.emit("install", "bootstrap completed")
    return results


def uninstall(purge: bool = False) -> list[str]:
    """Stop the daemon, leave Workstation Mode, undo every journaled change."""
    from . import modes

    lines = []
    try:
        modes.off(config.load())
        lines.append("left Workstation Mode")
    except Exception:
        pass
    lines += journal.rollback()
    from .. import platform as osplatform

    for name in list(state_names()):  # components this product started itself (no systemd / launchd)
        if supervise.stop(name):
            lines.append(f"stop                 {name}")
    if osplatform.current().autostart().enabled and osplatform.current().set_autostart([], False):
        lines.append("remove               start at sign-in")
    if purge:
        for path in (paths.config_dir(), paths.state_dir()):
            shutil.rmtree(path, ignore_errors=True)
            lines.append(f"remove               {path}")
    return lines


def state_names() -> list[str]:
    from . import state

    return sorted(state.load("supervised", {}) or {})
