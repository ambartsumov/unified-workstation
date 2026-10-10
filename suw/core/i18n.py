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


class Msg(str):
    """A sentence taken from the catalog. It reads as English — logs, the command line and tests
    see ordinary text — and remembers its key, so the window can show it in the user's language."""

    key: str = ""
    values: dict = {}


def msg(key: str, **values) -> Msg:
    out = Msg(t(key, SOURCE, **values))
    out.key, out.values = key, values
    return out


def render(text, lang: str):
    """`text` in `lang` if it came from the catalog; anything else is returned unchanged."""
    if isinstance(text, Msg):
        return t(text.key, lang, **{name: render(value, lang) for name, value in text.values.items()})
    return text


def localize(node, lang: str):
    """A copy of a response for the window with every catalog sentence in `lang`."""
    if isinstance(node, Msg):
        return render(node, lang)
    if isinstance(node, dict):
        return {key: localize(value, lang) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [localize(value, lang) for value in node]
    return node
