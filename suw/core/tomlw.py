"""Minimal TOML writer (the stdlib only ships a reader).

Supports what SUW stores: scalars, lists of scalars, nested tables and arrays of tables.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE.match(key) else json.dumps(key, ensure_ascii=False)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    if value is None:
        return '""'
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _emit(table: dict, prefix: list[str], out: list[str]) -> None:
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict) and not _is_table_array(v)]
    if prefix and (scalars or not table):
        out.append(f"[{'.'.join(_key(p) for p in prefix)}]")
    for key, value in scalars:
        out.append(f"{_key(key)} = {_scalar(value)}")
    if scalars or (prefix and not table):
        out.append("")
    for key, value in table.items():
        if isinstance(value, dict):
            _emit(value, prefix + [key], out)
        elif _is_table_array(value):
            for item in value:
                out.append(f"[[{'.'.join(_key(p) for p in prefix + [key])}]]")
                for k, v in item.items():
                    if not isinstance(v, dict):
                        out.append(f"{_key(k)} = {_scalar(v)}")
                out.append("")


def dumps(data: dict, header: str = "") -> str:
    out: list[str] = []
    if header:
        out.extend(f"# {line}".rstrip() for line in header.splitlines())
        out.append("")
    _emit(data, [], out)
    return "\n".join(out).rstrip() + "\n"


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write-then-rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump(path: Path, data: dict, header: str = "", mode: int = 0o600) -> None:
    atomic_write(path, dumps(data, header), mode)
