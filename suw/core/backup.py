"""Configuration snapshots, restore, and portable export/import.

Three kinds of data are kept apart on purpose:

* portable settings — safe to move to another computer (theme, language, sync policy, …)
* machine-specific settings — true only here (this computer's name and id, folder paths,
  local ports); exported only when the user asks for a full backup of *this* computer
* secrets — never exported. They stay in the operating system's credential store; a restored
  configuration keeps the *references* and the user re-enters the values once.

Every change that could lose settings (restore, import, reset, migration) takes a snapshot
first, so it can be undone.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .. import __version__, product
from . import config, events, inventory, paths, tomlw

KEEP = 30
FORMAT = 1
# Keys that describe this computer rather than the user's preferences.
MACHINE_KEYS = ("device.name", "device.id", "work.path", "work.api_address", "work.listen", "projects.roots", "projects.scan_roots", "projects.extra", "projects.exclude", "artifacts.path", "cloud.save_to", "general.autostart")
_FILES = ("config.toml", "shared/shared.toml", "shared/urls.toml", "shared/inventory.toml")


class BackupError(RuntimeError):
    pass


def directory() -> Path:
    return paths.ensure(paths.state_dir() / "backups" / "config")


def _present() -> list[tuple[str, Path]]:
    base = paths.config_dir()
    return [(name, base / name) for name in _FILES if (base / name).is_file()]


def snapshot(reason: str = "manual") -> Path | None:
    """Copy every configuration file into one archive. None when there is nothing to save."""
    files = _present()
    if not files:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory() / f"{stamp}-{_slug(reason)}.zip"
    index = 1
    while target.exists():
        index += 1
        target = directory() / f"{stamp}-{_slug(reason)}-{index}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("snapshot.json", json.dumps({"format": FORMAT, "reason": reason, "created": time.time(), "version": __version__, "device": config.load().device}))
        for name, path in files:
            archive.write(path, name)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    _prune()
    return target


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")[:32] or "snapshot"


def _prune() -> None:
    archives = sorted(directory().glob("*.zip"))
    for old in archives[:-KEEP]:
        old.unlink(missing_ok=True)


def snapshots() -> list[dict]:
    out = []
    for path in sorted(directory().glob("*.zip"), reverse=True):
        meta: dict = {}
        try:
            with zipfile.ZipFile(path) as archive:
                meta = json.loads(archive.read("snapshot.json"))
                names = [n for n in archive.namelist() if n != "snapshot.json"]
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
        out.append({"id": path.stem, "created": meta.get("created", path.stat().st_mtime), "reason": meta.get("reason", ""), "version": meta.get("version", ""), "files": names, "bytes": path.stat().st_size})
    return out


def _safe_members(archive: zipfile.ZipFile) -> list[str]:
    return [name for name in archive.namelist() if name in _FILES]


def restore(snapshot_id: str) -> list[str]:
    """Put a snapshot back. The current configuration is snapshotted first."""
    source = directory() / f"{Path(snapshot_id).name}.zip"
    if not source.is_file():
        raise BackupError("That snapshot no longer exists.")
    try:
        with zipfile.ZipFile(source) as archive:
            members = {name: archive.read(name).decode("utf-8").replace("\r\n", "\n") for name in _safe_members(archive)}
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise BackupError("The snapshot is damaged and was not used.") from exc
    _check(members)
    snapshot("before-restore")
    for name, text in members.items():
        tomlw.atomic_write(paths.config_dir() / name, text, 0o600 if name == "config.toml" else 0o644)
    events.emit("config.restored", f"configuration restored from snapshot {snapshot_id}")
    return sorted(members)


def _check(members: dict[str, str]) -> None:
    import tomllib

    for name, text in members.items():
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise BackupError(f"{name} inside the archive is not valid and was not used.") from exc
        if name.endswith("inventory.toml"):
            continue
        errors, _ = config.validate(data)
        if errors:
            raise BackupError(f"{name} inside the archive has invalid settings: {errors[0]}")


def reset() -> Path | None:
    """Back to the shipped defaults. Paired computers and servers are kept (inventory)."""
    saved = snapshot("before-reset")
    keep = {"device": {k: v for k, v in config.read_toml(config.local_file()).get("device", {}).items()}}
    tomlw.dump(config.local_file(), keep, "This machine only.")
    for path in (config.shared_file(), config.urls_file()):
        if path.exists():
            tomlw.dump(path, {}, mode=0o644)
    events.emit("config.reset", "settings reset to defaults")
    return saved


def rebuild() -> list[str]:
    """Recover from unreadable configuration: keep every file that still parses, replace the
    rest with the last version that did, and fall back to defaults when there is none."""
    import shutil
    import tomllib

    actions = []
    snapshot("before-rebuild")
    for name, path in ((n, paths.config_dir() / n) for n in _FILES):
        if not path.exists():
            continue
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
            continue
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            pass
        quarantine = path.with_name(path.name + f".broken-{int(time.time())}")
        shutil.move(str(path), quarantine)
        last_good = paths.state_dir() / "lkg" / path.name
        if last_good.exists():
            shutil.copyfile(last_good, path)
            actions.append(f"{name}: replaced with the last version that worked (the broken file is kept as {quarantine.name})")
        else:
            actions.append(f"{name}: unreadable and no earlier version exists; defaults are used (the broken file is kept as {quarantine.name})")
    if not actions:
        actions.append("Every configuration file is readable; nothing needed rebuilding.")
    events.emit("config.rebuilt", "; ".join(actions))
    return actions


# ── portable export / import ────────────────────────────────────────────────


def classify(flat: dict) -> dict[str, dict]:
    """Split flattened settings into portable / machine / secret references."""
    out: dict[str, dict] = {"portable": {}, "machine": {}, "secrets": {}}
    for key, value in flat.items():
        if key.startswith("secrets.") or (isinstance(value, str) and value.startswith("keychain://")):
            out["secrets"][key] = value
        elif key in MACHINE_KEYS or key.startswith("platform."):
            out["machine"][key] = value
        else:
            out["portable"][key] = value
    return out


def _current_flat() -> dict:
    merged: dict = {}
    for path in (config.shared_file(), config.urls_file(), config.local_file()):
        merged = config.deep_merge(merged, config.read_toml(path))
    return config._flatten(merged)


def export_preview() -> dict:
    groups = classify(_current_flat())
    inv = inventory.load()
    return {
        "portable": len(groups["portable"]),
        "machine": len(groups["machine"]),
        "secret_references": sorted(groups["secrets"]),
        "devices": sorted(inv["devices"]),
        "note": "Passwords and keys are never part of an export. They stay in this computer's credential store.",
    }


def export(include_machine: bool = False, include_devices: bool = False) -> bytes:
    groups = classify(_current_flat())
    document = {
        "format": FORMAT,
        "product": product.SLUG,
        "version": __version__,
        "created": time.time(),
        "portable": groups["portable"],
        "machine": groups["machine"] if include_machine else {},
        "secret_references": sorted(groups["secrets"]),   # names only, so the importer can ask for the values
        "devices": {},
    }
    if include_devices:
        inv = inventory.load()
        # Public identities and logical names only — never a credential.
        document["devices"] = {name: {k: v for k, v in device.items() if k in ("logical_name", "role", "platform", "host", "tailscale_name", "ssh_user", "ssh_port", "syncthing_id", "syncthing_port", "deskflow_fp")} for name, device in inv["devices"].items()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("unified-workstation-settings.json", json.dumps(document, indent=2, sort_keys=True))
        archive.writestr("README.txt", "Unified Workstation settings export.\nContains no passwords, keys or tokens.\nImport it under Settings → Advanced → Import configuration.\n")
    events.emit("config.exported", "settings exported" + (" (including this computer's own settings)" if include_machine else ""))
    return buffer.getvalue()


def read_export(blob: bytes) -> dict:
    if len(blob) > 5_000_000:
        raise BackupError("That file is too large to be a settings export.")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            document = json.loads(archive.read("unified-workstation-settings.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError("That file is not a settings export from this product.") from exc
    if not isinstance(document, dict) or document.get("product") != product.SLUG or int(document.get("format", 0)) > FORMAT:
        raise BackupError("That export was made by a newer or different product and can not be imported here.")
    return document


def import_preview(blob: bytes) -> dict:
    document = read_export(blob)
    current = _current_flat()
    portable = document.get("portable", {})
    return {
        "from_version": document.get("version", ""),
        "changes": sorted(key for key, value in portable.items() if current.get(key) != value),
        "machine": sorted(document.get("machine", {})),
        "secret_references": document.get("secret_references", []),
        "devices": sorted(document.get("devices", {})),
    }


def import_(blob: bytes, include_machine: bool = False) -> dict:
    """Apply an export. Validated as a whole first; a snapshot is taken; secrets are never imported."""
    document = read_export(blob)
    wanted = dict(document.get("portable", {}))
    if include_machine:
        wanted.update(document.get("machine", {}))
    wanted = {k: v for k, v in wanted.items() if not k.startswith("secrets.") and k not in ("device.id",)}
    candidate: dict = {}
    for key, value in wanted.items():
        config._set_path(candidate, key, value)
    errors, _ = config.validate(candidate)
    if errors:
        raise BackupError("The export contains settings this version does not accept: " + errors[0])
    saved = snapshot("before-import")
    local = config.read_toml(config.local_file())
    for key, value in wanted.items():
        config._set_path(local, key, value)
    tomlw.dump(config.local_file(), local, "This machine only.")
    events.emit("config.imported", f"{len(wanted)} settings imported")
    return {"applied": len(wanted), "snapshot": saved.stem if saved else "", "secrets_to_reenter": document.get("secret_references", [])}
