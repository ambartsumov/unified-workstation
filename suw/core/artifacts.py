"""Artifact layer: datasets, checkpoints, generated media — everything that is too large
or too binary for Git and too valuable to exist in one place.

`ArtifactStore` is the seam. Two backends ship: a plain directory (any local or mounted
path) and a directory on an SSH host (the home server). Object storage can implement the
same six primitives later without touching callers.

Every artifact carries: project, logical name, SHA-256, size, creation time, optional
metadata and a retention note. `verify()` re-hashes what the store actually holds.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths
from .config import Config
from .policy import file_digest
from .proc import run, ssh_cmd

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MANIFEST = "suw-artifacts.json"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes"]


class ArtifactError(RuntimeError):
    """Expected failure with a message fit to show the user."""


@dataclass
class Artifact:
    project: str
    name: str
    sha256: str
    size: int
    created: str
    kind: str = "file"  # file | directory (stored as an uncompressed tar)
    retention: str = "keep"
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _check(*parts: str) -> None:
    for part in parts:
        if not _SAFE.match(part):
            raise ArtifactError(f"'{part}' is not a valid name (letters, digits, dot, dash, underscore)")


class ArtifactStore(ABC):
    """put / get / list / delete / stat / verify on top of six transport primitives."""

    label = "store"

    # transport primitives --------------------------------------------------
    @abstractmethod
    def _read(self, rel: str) -> bytes | None: ...

    @abstractmethod
    def _write(self, rel: str, data: bytes) -> None: ...

    @abstractmethod
    def _upload(self, local: Path, rel: str) -> None: ...

    @abstractmethod
    def _download(self, rel: str, local: Path) -> None: ...

    @abstractmethod
    def _remove(self, rel: str) -> None: ...

    @abstractmethod
    def _checksum(self, rel: str) -> str | None: ...

    # manifest ---------------------------------------------------------------
    def _manifest(self, project: str) -> dict[str, dict]:
        raw = self._read(f"{project}/{MANIFEST}")
        if not raw:
            return {}
        try:
            return dict(json.loads(raw).get("artifacts", {}))
        except ValueError as exc:
            raise ArtifactError(f"manifest for '{project}' is unreadable: {exc}")

    def _save_manifest(self, project: str, entries: dict[str, dict]) -> None:
        body = json.dumps({"schema": 1, "project": project, "artifacts": dict(sorted(entries.items()))}, ensure_ascii=False, indent=1)
        self._write(f"{project}/{MANIFEST}", body.encode())

    # public API -------------------------------------------------------------
    def put(self, source: Path, project: str, name: str | None = None, *, retention: str = "keep", metadata: dict | None = None) -> Artifact:
        if not source.exists():
            raise ArtifactError(f"{source} does not exist")
        name = name or source.name
        _check(project, name)
        kind = "directory" if source.is_dir() else "file"
        with tempfile.TemporaryDirectory(prefix="suw-artifact-") as tmp:
            payload = source
            if kind == "directory":
                payload = Path(tmp) / f"{name}.tar"
                with tarfile.open(payload, "w") as archive:
                    archive.add(source, arcname=name)
            digest, size = file_digest(payload), payload.stat().st_size
            self._upload(payload, f"{project}/{name}")
        stored = self._checksum(f"{project}/{name}")
        if stored != digest:
            self._remove(f"{project}/{name}")
            raise ArtifactError(f"upload of '{name}' could not be verified (checksum mismatch); nothing was recorded")
        artifact = Artifact(project, name, digest, size, datetime.now().astimezone().isoformat(timespec="seconds"), kind, retention, metadata or {})
        entries = self._manifest(project)
        entries[name] = artifact.as_dict()
        self._save_manifest(project, entries)
        return artifact

    def stat(self, project: str, name: str) -> Artifact | None:
        _check(project, name)
        entry = self._manifest(project).get(name)
        return Artifact(**entry) if entry else None

    def list(self, project: str | None = None) -> list[Artifact]:
        out: list[Artifact] = []
        for proj in [project] if project else self.projects():
            _check(proj)
            out += [Artifact(**entry) for entry in self._manifest(proj).values()]
        return sorted(out, key=lambda a: (a.project, a.name))

    def get(self, project: str, name: str, dest: Path, *, extract: bool = True) -> Path:
        artifact = self.stat(project, name)
        if artifact is None:
            raise ArtifactError(f"no artifact '{name}' in project '{project}'")
        dest = dest.expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="suw-artifact-", dir=dest) as tmp:
            staged = Path(tmp) / name
            self._download(f"{project}/{name}", staged)
            if file_digest(staged) != artifact.sha256:
                raise ArtifactError(f"'{name}' failed verification after download; the copy was discarded")
            target = dest / name
            if target.exists():
                raise ArtifactError(f"{target} already exists; choose another destination (-o)")
            if artifact.kind == "directory" and extract:
                with tarfile.open(staged) as archive:
                    archive.extractall(dest, filter="data")
            else:
                shutil.move(str(staged), target)
        return dest / name

    def verify(self, project: str | None = None, name: str | None = None) -> list[tuple[Artifact, bool]]:
        """Re-hash what the store holds. Returns (artifact, intact) pairs."""
        wanted = [a for a in self.list(project) if name is None or a.name == name]
        return [(a, self._checksum(f"{a.project}/{a.name}") == a.sha256) for a in wanted]

    def delete(self, project: str, name: str) -> bool:
        _check(project, name)
        entries = self._manifest(project)
        if name not in entries:
            return False
        self._remove(f"{project}/{name}")
        del entries[name]
        self._save_manifest(project, entries)
        return True

    @abstractmethod
    def projects(self) -> list[str]: ...

    @abstractmethod
    def describe(self) -> str: ...


class DirectoryStore(ArtifactStore):
    label = "dir"

    def __init__(self, root: Path):
        self.root = root

    def describe(self) -> str:
        return str(self.root)

    def projects(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / MANIFEST).exists() and _SAFE.match(p.name))

    def _read(self, rel: str) -> bytes | None:
        try:
            return (self.root / rel).read_bytes()
        except OSError:
            return None

    def _write(self, rel: str, data: bytes) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

    def _upload(self, local: Path, rel: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.part")
        try:
            shutil.copyfile(local, tmp)
            tmp.replace(target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise ArtifactError(f"cannot store artifact: {exc.strerror or exc}")

    def _download(self, rel: str, local: Path) -> None:
        try:
            shutil.copyfile(self.root / rel, local)
        except OSError as exc:
            raise ArtifactError(f"cannot read artifact: {exc.strerror or exc}")

    def _remove(self, rel: str) -> None:
        (self.root / rel).unlink(missing_ok=True)

    def _checksum(self, rel: str) -> str | None:
        try:
            return file_digest(self.root / rel)
        except OSError:
            return None


class SSHStore(ArtifactStore):
    """A directory on an SSH host addressed by its logical alias (normally `home`)."""

    label = "ssh"

    def __init__(self, alias: str, remote_root: str, ssh_opts: list[str] | None = None):
        if not re.match(r"^[A-Za-z0-9._/-]+$", remote_root) or remote_root.startswith("-") or ".." in remote_root:
            raise ArtifactError(f"'{remote_root}' is not a usable remote path")
        self.alias, self.remote_root = alias, remote_root.rstrip("/")
        self.opts = [*SSH_OPTS, *(ssh_opts or [])]

    def describe(self) -> str:
        return f"{self.alias}:{self.remote_root}"

    def _ssh(self, script: str, *args: str, stdin: str | None = None, timeout: float = 120):
        # Fixed scripts only; arguments are validated names passed as positional parameters.
        return run([*ssh_cmd(), *self.opts, self.alias, "--", "sh", "-c", _q(script), "suw", *[_q(a) for a in args]], stdin=stdin, timeout=timeout)

    def _path(self, rel: str) -> str:
        return f"{self.remote_root}/{rel}"

    def projects(self) -> list[str]:
        res = self._ssh('cd "$1" 2>/dev/null && for d in */; do [ -f "${d}' + MANIFEST + '" ] && echo "${d%/}"; done; true', self.remote_root)
        if not res.ok:
            raise ArtifactError(f"cannot reach the artifact store on {self.alias}: {(res.err.strip().splitlines() or ['unreachable'])[-1]}")
        return sorted(line for line in res.out.split() if _SAFE.match(line))

    def _read(self, rel: str) -> bytes | None:
        res = self._ssh('cat "$1" 2>/dev/null', self._path(rel))
        if res.rc == 255:
            raise ArtifactError(f"cannot reach the artifact store on {self.alias}: {(res.err.strip().splitlines() or ['unreachable'])[-1]}")
        return res.out.encode() if res.ok and res.out else None

    def _write(self, rel: str, data: bytes) -> None:
        res = self._ssh('mkdir -p "$(dirname "$1")" && cat > "$1.tmp" && mv "$1.tmp" "$1"', self._path(rel), stdin=data.decode())
        if not res.ok:
            raise ArtifactError(f"cannot write to the artifact store: {res.err.strip()[-200:]}")

    def _rsync(self, source: str, dest: str) -> None:
        res = run(["rsync", "-a", "--partial", "--inplace", "-e", " ".join([*ssh_cmd(), *self.opts]), source, dest], timeout=12 * 3600)
        if not res.ok:
            raise ArtifactError(f"transfer failed: {(res.err.strip().splitlines() or ['rsync error'])[-1]}")

    def _upload(self, local: Path, rel: str) -> None:
        made = self._ssh('mkdir -p "$(dirname "$1")"', self._path(rel))
        if not made.ok:
            raise ArtifactError(f"cannot reach the artifact store on {self.alias}: {(made.err.strip().splitlines() or ['unreachable'])[-1]}")
        self._rsync(str(local), f"{self.alias}:{self._path(rel)}")

    def _download(self, rel: str, local: Path) -> None:
        self._rsync(f"{self.alias}:{self._path(rel)}", str(local))

    def _remove(self, rel: str) -> None:
        self._ssh('rm -f -- "$1"', self._path(rel))

    def _checksum(self, rel: str) -> str | None:
        res = self._ssh('if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi', self._path(rel), timeout=3600)
        return res.out.split()[0] if res.ok and res.out.split() else None


def _q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def from_config(cfg: Config) -> ArtifactStore:
    kind = str(cfg.get("artifacts.store", "dir"))
    if kind == "ssh":
        return SSHStore(str(cfg.get("artifacts.host", "home")), str(cfg.get("artifacts.remote_path", "suw-artifacts")))
    return DirectoryStore(paths.expand(str(cfg.get("artifacts.path", "~/Projects/archive/artifacts"))))
