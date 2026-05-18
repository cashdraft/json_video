"""Починка типичных синтаксических ошибок в JSON-ответах LLM (Analysis, Architect и т.д.)."""

from __future__ import annotations

import json
import re
from typing import Any

_RE_STRING_ENDS_WITH_SINGLE = re.compile(
    r'("(?:[^"\\]|\\.)*)\'(\s*[,}\]])',
)
_RE_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_markdown_json_fence(text: str) -> str:
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n([\s\S]*?)\n?```$", s)
    if m:
        return m.group(1).strip()
    return re.sub(r"^```+|```+$", "", s).strip()


def extract_json_object_from_text(raw: str) -> str | None:
    if not raw:
        return None
    s = strip_markdown_json_fence(raw)
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        return fence_match.group(1).strip()
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        return s[first : last + 1].strip()
    return None


def repair_json_string_delimiter_typos(text: str) -> str:
    """Строка открыта на \", закрыта на ' перед , } ]."""
    return _RE_STRING_ENDS_WITH_SINGLE.sub(r'\1"\2', text)


def repair_trailing_commas(text: str) -> str:
    return _RE_TRAILING_COMMA.sub(r"\1", text)


def _try_parse_object(candidate: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def normalize_llm_json_object(
    raw: str,
    *,
    pretty: bool = True,
) -> tuple[str, bool]:
    """Вернуть (текст, был_ли_исправлен). Если распарсить нельзя — исходный trim."""
    original = (raw or "").strip()
    if not original:
        return "", False

    candidate = extract_json_object_from_text(original) or original
    obj = _try_parse_object(candidate)
    if obj is not None:
        if obj == _try_parse_object(original):
            return original, False
        out = json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None)
        if pretty:
            out += "\n"
        return out, candidate != original

    repaired = repair_json_string_delimiter_typos(candidate)
    repaired = repair_trailing_commas(repaired)
    obj = _try_parse_object(repaired)
    if obj is None:
        return original, False

    out = json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None)
    if pretty:
        out += "\n"
    return out, True


# Этапы ReWrite, где Result — один JSON-объект {…} (не массив, не editor split).
STAGE_JSON_OBJECT_KEYS = frozenset({"analysis", "architect"})
