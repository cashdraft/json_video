"""Нормализация plain-text ответов модели (без HTML-разметки в сценарии)."""

from __future__ import annotations

import re

_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")


def normalize_model_plain_text(text: str) -> str:
    """Заменяет HTML <br> на переносы строк; схлопывает лишние пустые строки."""
    s = str(text or "")
    if not s or "<br" not in s.lower():
        return s
    s = _BR_TAG_RE.sub("\n", s)
    return _EXCESS_NEWLINES_RE.sub("\n\n", s)
