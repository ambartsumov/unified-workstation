"""Project registration and discovery.

A repository is *managed* when it sits directly inside a managed root (`projects.roots`,
by default `~/Projects/active`) or was added explicitly with `suw project add`.
`scan()` only ever looks one level deep inside the configured scan roots: it never crawls
the home directory and never changes a repository it finds.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config, paths, state, tomlw
from .config import Config
from .proc import run

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def roots(cfg: Config) -> list[Path]:
    return [paths.expand(str(r)) for r in cfg.get("projects.roots", [])]


def scan_roots(cfg: Config) -> list[Path]:
    seen: dict[str, Path] = {}
    for raw in [*cfg.get("projects.roots", []), *cfg.get("projects.scan_roots", [])]:
        path = paths.expand(str(raw))
        seen.setdefault(str(path), path)
    return list(seen.values())


def _repos_in(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return [child for child in sorted(root.iterdir()) if (child / ".git").exists()]
    except OSError:
        return []


def extra(cfg: Config) -> list[Path]:
    return [paths.expand(str(p)) for p in cfg.get("projects.extra", [])]


def discover(cfg: Config) -> list[Path]:
    found: dict[str, Path] = {}
    for root in roots(cfg):
        for child in _repos_in(root):
            found[str(child)] = child
    for path in extra(cfg):
        if (path / ".git").exists():
            found[str(path)] = path
    for path in excluded(cfg):
        found.pop(str(path), None)
    return list(found.values())


def excluded(cfg: Config) -> list[Path]:
    return [paths.expand(str(p)) for p in cfg.get("projects.exclude", [])]


def scan(cfg: Config) -> dict:
    """Preview for `suw project scan`: what is managed and what was found but is not."""
    managed = {str(p) for p in discover(cfg)}
    unmanaged, searched = [], []
    for root in scan_roots(cfg):
        searched.append(str(root))
        for child in _repos_in(root):
            if str(child) not in managed:
                unmanaged.append(str(child))
    return {"roots": searched, "managed": sorted(managed), "unmanaged": sorted(set(unmanaged))}


def tilde(path: Path) -> str:
    return "~/" + str(path.relative_to(paths.home())) if path.is_relative_to(paths.home()) else str(path)


def add(cfg: Config, path: Path) -> bool:
    """Register a repository explicitly. Returns False when it already was managed."""
    exclude = [p for p in cfg.get("projects.exclude", []) if str(paths.expand(str(p))) != str(path)]
    if len(exclude) != len(cfg.get("projects.exclude", [])):
        config.set_value("projects.exclude", exclude, "local")
    if any(str(p) == str(path) for p in discover(config.load())):
        return False
    config.set_value("projects.extra", [*[str(p) for p in cfg.get("projects.extra", [])], tilde(path)], "local")
    return True


def remove(cfg: Config, path: Path) -> bool:
    """Stop managing a repository. The repository itself is never touched."""
    was = any(str(p) == str(path) for p in discover(cfg))
    kept = [str(p) for p in cfg.get("projects.extra", []) if str(paths.expand(str(p))) != str(path)]
    config.set_value("projects.extra", kept, "local")
    if any(path.parent == root for root in roots(cfg)):
        exclude = [str(p) for p in cfg.get("projects.exclude", [])]
        if tilde(path) not in exclude:
            config.set_value("projects.exclude", [*exclude, tilde(path)], "local")
    return was


def resolve(cfg: Config, name_or_path: str | None) -> Path | None:
    """A managed project by name, an existing path, or the repository we are standing in."""
    if not name_or_path:
        return active(cfg)
    for path in [paths.shared_dir(), *discover(cfg)]:
        if path.name == name_or_path:
            return path
    candidate = Path(name_or_path).expanduser()
    if (candidate / ".git").exists():
        return candidate.resolve()
    return None


def containing(cwd: str | Path | None = None) -> Path | None:
    res = run(["git", "-C", str(cwd or os.getcwd()), "rev-parse", "--show-toplevel"], timeout=5)
    return Path(res.out.strip()) if res.ok and res.out.strip() else None


def active(cfg: Config, cwd: str | Path | None = None) -> Path | None:
    """The repository we are standing in, else the remembered one, else the first known."""
    here = containing(cwd)
    if here is not None:
        return here
    remembered = state.load("project").get("active")
    if remembered and (Path(remembered) / ".git").exists():
        return Path(remembered)
    known = discover(cfg)
    return known[0] if known else None


def remember(path: Path) -> None:
    state.save("project", {"active": str(path)})


# ── shared project list (so a new workstation knows what to clone) ───────────


def catalog_file() -> Path:
    return paths.shared_dir() / "projects.toml"


def catalog() -> dict[str, dict]:
    return dict(config.read_toml(catalog_file()).get("projects", {}))


def _safe_remote(url: str) -> str:
    """Only remotes without embedded credentials are ever written to shared config."""
    if not url or re.match(r"^[a-z+]+://[^/@\s]*:[^/@\s]*@", url) or re.match(r"^https?://[^/@\s]+@", url):
        return ""
    return url


def publish(cfg: Config) -> bool:
    """Record name -> remote for every managed project in the shared configuration.

    Entries are only added or updated, never removed here: another workstation may
    manage projects this one does not have. Returns True when the file changed.
    """
    if not (paths.shared_dir() / ".git").exists():
        return False
    known = catalog()
    before = tomlw.dumps({"projects": known})
    for path in discover(cfg):
        if not _NAME.match(path.name):
            continue
        url = _safe_remote(run(["git", "-C", str(path), "config", "--get", "remote.origin.url"], timeout=5).out.strip())
        if url:
            known[path.name] = {"remote": url}
    if tomlw.dumps({"projects": known}) == before:
        return False
    tomlw.dump(catalog_file(), {"projects": dict(sorted(known.items()))}, "Projects known to SUW (name -> Git remote). No secrets.", mode=0o644)
    return True


def missing(cfg: Config) -> list[tuple[str, str, Path]]:
    """Catalogued projects that are not on this machine: (name, remote, destination)."""
    have = {p.name for p in discover(cfg)}
    base = roots(cfg)[0] if roots(cfg) else paths.home() / "Projects" / "active"
    out = []
    for name, entry in sorted(catalog().items()):
        remote = str(entry.get("remote", ""))
        if name not in have and _NAME.match(name) and _safe_remote(remote) and not (base / name).exists():
            out.append((name, remote, base / name))
    return out
