"""Translations. Catalogs are flat JSON files (`resources/i18n/<lang>.json`); no user-facing
sentence lives in program logic. English is the source language and the fallback."""

from __future__ import annotations

import json
import locale
import os
from functools import lru_cache

from . import paths

SOURCE = "en"
LANGUAGES = {"en": "English", "ru": "Русский"}


@lru_cache(maxsize=8)
def catalog(lang: str) -> dict[str, str]:
    file = paths.resources() / "i18n" / f"{lang}.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def system_language() -> str:
    for name in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(name, "")
        code = value.split(":")[0].split(".")[0].split("_")[0].lower()
        if code in LANGUAGES:
            return code
    try:
        code = (locale.getlocale()[0] or "").split("_")[0].lower()
    except (ValueError, TypeError):
        code = ""
    if code in LANGUAGES:
        return code
    return {"russian": "ru", "english": "en"}.get(code, SOURCE)


def resolve(setting: str | None) -> str:
    return setting if setting in LANGUAGES else system_language()


def merged(lang: str) -> dict[str, str]:
    """The catalog for `lang` with English filling any gap."""
    return {**catalog(SOURCE), **catalog(lang)} if lang != SOURCE else dict(catalog(SOURCE))


def t(key: str, lang: str = SOURCE, **values) -> str:
    text = catalog(lang).get(key) or catalog(SOURCE).get(key) or key
    try:
        return text.format(**values) if values else text
    except (KeyError, IndexError, ValueError):
        return text
