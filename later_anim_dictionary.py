"""Единый словарь anim для Later… — промт и валидатор берут отсюда."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ANIM_DICT_PATH = BASE_DIR / "data" / "later_anim_dictionary.json"

# Единственный источник allowed (9 кубиков + none). Не читать из JSON — иначе легко разъехаться.
CANONICAL_ALLOWED: tuple[str, ...] = (
    "none",
    "fade-in",
    "fade-out",
    "fly-up",
    "grow-y",
    "grow-x",
    "scale-in",
    "draw-path",
    "count-up",
)


def allowed_anims() -> tuple[str, ...]:
    return CANONICAL_ALLOWED


def allowed_anims_set() -> set[str]:
    return set(CANONICAL_ALLOWED)


def allowed_anims_sorted() -> list[str]:
    return list(CANONICAL_ALLOWED)


def backlog_anims() -> tuple[str, ...]:
    try:
        raw = json.loads(ANIM_DICT_PATH.read_text(encoding="utf-8"))
        items = raw.get("backlog")
        if isinstance(items, list):
            return tuple(str(x).strip() for x in items if str(x).strip())
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return (
        "parallax-drift",
        "pulse-glow",
        "twinkle",
        "kerning-spread",
    )


def anim_dictionary_prompt_line() -> str:
    """Строка для вставки в промт (СЛОВАРЬ ANIM) — join из CANONICAL_ALLOWED."""
    return ", ".join(CANONICAL_ALLOWED)


def anim_dictionary_prompt_block() -> str:
    """Полный блок словаря для system prompt (совпадает с user prompt)."""
    anim_line = anim_dictionary_prompt_line()
    return f"""=== СЛОВАРЬ ANIM (закрытый список, движок умеет ТОЛЬКО это) ===
{anim_line}

Нужен другой тип движения — НЕ выдумывай и НЕ бери из общих знаний об анимации
(никаких slide-in, bounce, pop-in, blur-in, fly-left и т.п. — их движок НЕ умеет).
Используй в tracks ближайший из списка выше, а желаемый новый кубик опиши
словами в блоке NOTES как «заявка на новый кубик».

Памятка по кубикам (чтобы выбирать правильный):
- none — элемент статичен, не двигается
- fade-in / fade-out — проявление / затухание (opacity)
- fly-up — текст въезжает снизу с проявлением
- grow-y — столбик растёт снизу вверх (для баров)
- grow-x — линия/ось/прогресс растёт слева направо
- scale-in — появление из точки (хорошо для точек, бейджей, крупных цифр)
- draw-path — прорисовка линии обводкой (только сплошные линии, НЕ пунктир)
- count-up — накрутка числа от 0 к значению (только для <text> с числом)"""


def anim_dictionary_backlog_prompt_line() -> str:
    bl = backlog_anims()
    return ", ".join(bl) if bl else ""


def anim_dictionary_debug() -> dict[str, Any]:
    """Для GET /scenes-lab/api/anim-dictionary и отладки рантайма."""
    return {
        "allowed": allowed_anims_sorted(),
        "count": len(CANONICAL_ALLOWED),
        "source": "later_anim_dictionary.CANONICAL_ALLOWED",
    }
