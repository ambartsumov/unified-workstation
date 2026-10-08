"""Local structured event log: timestamped JSON lines, rotated, secrets redacted.

Record shape (one JSON object per line):
    {"timestamp": "...", "event": "sync.completed", "level": "info", "device": "ubuntu",
     "message": "...", "project": "...", "status": "ok", "duration_ms": 842}
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import paths

MAX_BYTES = 2_000_000
KEEP = 3
_lock = threading.Lock()
_device: list[str] = []

# (pattern, replacement). Order matters: multi-line blocks first, generic assignments last.
_REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "<redacted private key>"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})"), "<redacted>"),
    (re.compile(r"\btskey-[A-Za-z0-9-]{6,}"), "<redacted>"),
    (re.compile(r"\bhf_[A-Za-z0-9]{8,}"), "<redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{12,}"), "<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "<redacted>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), "<redacted>"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"), "<redacted>"),  # Telegram bot token
    (re.compile(r"(?i)\b((?:set-)?cookie|authorization|proxy-authorization)(\s*[:=]\s*)[^\r\n]+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+"), r"\1<redacted>"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _device_name() -> str:
    if not _device:
        try:
            from . import config

            _device.append(config.load().device)
        except Exception:
            _device.append("")
    return _device[0]


def log_path() -> Path:
    return paths.state_dir() / "events.jsonl"


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for index in range(KEEP - 1, 0, -1):
        older = path.with_suffix(f".jsonl.{index}")
        if older.exists():
            older.replace(path.with_suffix(f".jsonl.{index + 1}"))
    path.replace(path.with_suffix(".jsonl.1"))


def emit(kind: str, message: str, level: str = "info", **fields: Any) -> dict:
    """Append one audit event. Never raises: logging must not break the caller."""
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": kind,
        "level": level,
        "device": _device_name(),
        "message": redact(message),
    }
    record.update(_clean(fields))
    try:
        with _lock:
            path = log_path()
            paths.ensure(path.parent)
            _rotate(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return record


def normalize(record: dict) -> dict:
    """Accept records written by 0.1 (`ts`/`kind`/`msg`) alongside the current shape."""
    if "timestamp" not in record and "ts" in record:
        record["timestamp"] = record.pop("ts")
    if "event" not in record and "kind" in record:
        record["event"] = record.pop("kind")
    if "message" not in record and "msg" in record:
        record["message"] = record.pop("msg")
    return record


def _files() -> list[Path]:
    path = log_path()
    older = [path.with_suffix(f".jsonl.{i}") for i in range(KEEP, 0, -1)]
    return [p for p in [*older, path] if p.exists()]


def read(
    limit: int = 50,
    grep: str | None = None,
    level: str | None = None,
    *,
    kinds: tuple[str, ...] = (),
    project: str | None = None,
) -> list[dict]:
    """Newest `limit` matching records, oldest first. `kinds` are event-name prefixes."""
    needle = grep.lower() if grep else None
    out: list[dict] = []
    for path in reversed(_files()):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if needle and needle not in line.lower():
                continue
            try:
                record = normalize(json.loads(line))
            except ValueError:
                continue
            if level and record.get("level") != level:
                continue
            if kinds and not str(record.get("event", "")).startswith(kinds):
                continue
            if project and record.get("project") != project:
                continue
            out.append(record)
            if len(out) >= limit:
                return list(reversed(out))
    return list(reversed(out))


def follow(poll: float = 1.0) -> Iterator[dict]:
    path = log_path()
    position = path.stat().st_size if path.exists() else 0
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size < position:
            position = 0
        if size > position:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                for line in handle:
                    try:
                        yield normalize(json.loads(line))
                    except ValueError:
                        pass
                position = handle.tell()
        time.sleep(poll)


def format_record(record: dict) -> str:
    record = normalize(record)
    stamp = str(record.get("timestamp", ""))[:16].replace("T", " ")
    mark = {"warn": "!", "error": "x"}.get(record.get("level", ""), " ")
    return f"{stamp} {mark} {record.get('message', '')}"
