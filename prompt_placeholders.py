"""Плейсхолдеры в промтах ReWrite (locked, этапы, Master/Hero и т.д.).

Подстановка выполняется в рантайме перед отправкой в модель, а не при
сохранении файлов — в редакторе остаются литералы {{…}}.

Токены (как в UI: «Promt»):
- {{LANGUAGE}} — язык конвейера (одна строка, без переносов)
- {{DURATION}} — сводка Duration (одна строка)
- {{ORIGINAL_TITLE}} — исходное название ролика (одна строка)
- {{MASTER_PROMT}} — Master Promt (вставка с двойным переноса \\n\\n вокруг непустого текста)
- {{HERO_PROMT}} — Hero Promt (аналогично)
"""

from __future__ import annotations

import re
from typing import Any

PH_LANGUAGE = "{{LANGUAGE}}"
PH_DURATION = "{{DURATION}}"
PH_ORIGINAL_TITLE = "{{ORIGINAL_TITLE}}"
PH_MASTER = "{{MASTER_PROMT}}"
PH_HERO = "{{HERO_PROMT}}"


_WS_RE = re.compile(r"\s+")


def _single_line(s: str) -> str:
    """Одна строка: любые пробелы/переносы → один пробел по краям схлопнуты."""
    t = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return ""
    return _WS_RE.sub(" ", t).strip()


def _block_wrap(s: str) -> str:
    """Двойной перенос вокруг непустого фрагмента; пусто → пустая строка."""
    t = (s or "").strip()
    if not t:
        return ""
    return "\n\n" + t + "\n\n"


def _normalize_pipeline_language(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in ("en", "english", "англ"):
        return "en"
    return "ru"


def language_display(pipeline_language: Any) -> str:
    return "English" if _normalize_pipeline_language(pipeline_language) == "en" else "Русский"


def format_duration_placeholder_line(
    *,
    duration_minutes: int,
    chars_per_minute: int,
    target_chars: int,
) -> str:
    try:
        dm = int(duration_minutes)
    except (TypeError, ValueError):
        dm = 5
    dm = max(1, min(30, dm))
    try:
        cpm = int(chars_per_minute)
    except (TypeError, ValueError):
        cpm = 344
    cpm = max(1, min(2000, cpm))
    try:
        tc = int(target_chars)
    except (TypeError, ValueError):
        tc = dm * cpm
    return _single_line(f"{dm} мин · {cpm} симв./мин · цель ~{tc} симв.")


def apply_prompt_placeholders(
    text: str | None,
    *,
    language: str = "ru",
    duration_minutes: int = 5,
    chars_per_minute: int = 344,
    target_chars: int = 1500,
    original_title: str = "",
    master_prompt: str = "",
    hero_prompt: str = "",
    allow_nested_master_hero: bool = True,
) -> str:
    """Заменить известные плейсхолдеры. При allow_nested_master_hero=False
    не разворачиваются {{MASTER_PROMT}}/{{HERO_PROMT}} (для вложенного разбора
    Master/Hero до подстановки их в другие тексты)."""
    s = "" if text is None else str(text)
    if not s:
        return ""

    dur = format_duration_placeholder_line(
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
        target_chars=target_chars,
    )
    lang = language_display(language)
    title = _single_line(original_title)

    s = s.replace(PH_LANGUAGE, lang)
    s = s.replace(PH_DURATION, dur)
    s = s.replace(PH_ORIGINAL_TITLE, title)

    if allow_nested_master_hero:
        m_inner = apply_prompt_placeholders(
            master_prompt,
            language=language,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
            target_chars=target_chars,
            original_title=original_title,
            master_prompt="",
            hero_prompt="",
            allow_nested_master_hero=False,
        )
        h_inner = apply_prompt_placeholders(
            hero_prompt,
            language=language,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
            target_chars=target_chars,
            original_title=original_title,
            master_prompt="",
            hero_prompt="",
            allow_nested_master_hero=False,
        )
        s = s.replace(PH_MASTER, _block_wrap(m_inner))
        s = s.replace(PH_HERO, _block_wrap(h_inner))

    return s
