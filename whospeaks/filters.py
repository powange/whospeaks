"""Textes que Whisper invente sur du silence ou du bruit (« ... », « Merci. »…)."""

from __future__ import annotations

import functools
import logging
import re
import unicodedata

_LOGGER = logging.getLogger(__name__)


def normalize_text(text: str | None) -> str:
    """Minuscules, sans ponctuation, symboles (♪…) ni espaces : « Merci ! » -> « merci »."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFC", text).casefold()
        if unicodedata.category(c)[0] in "LNM"  # lettres, chiffres, accents combinés
    )


@functools.lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as err:
        _LOGGER.warning("Expression d'hallucination invalide ignorée « %s » : %s", pattern, err)
        return None


def is_hallucination(text: str | None, entries: list[str]) -> bool:
    """Vrai si le texte est vide, n'est que de la ponctuation, ou figure dans la liste.

    Une entrée est comparée au texte normalisé, sauf si elle commence par « ~ » :
    c'est alors une expression régulière cherchée dans le texte brut (sans casse).
    """
    normalized = normalize_text(text)
    if not normalized:
        return True
    raw = (text or "").strip()
    for entry in entries:
        if entry.startswith("~"):
            regex = _compile(entry[1:])
            if regex is not None and regex.search(raw):
                return True
        elif normalize_text(entry) == normalized:
            return True
    return False


def check_regexes(entries: list[str]) -> None:
    """Lève ValueError sur la première expression régulière invalide."""
    for entry in entries:
        if entry.startswith("~"):
            try:
                re.compile(entry[1:])
            except re.error as err:
                raise ValueError(f"hallucinations : expression invalide « {entry} » ({err})") from err
