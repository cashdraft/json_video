"""Режимы «Длина сцены» для Scene Writer и макрос {{SCENE_LENGTH_RULES}}."""
from __future__ import annotations

from typing import Any

PH_SCENE_LENGTH_RULES = "{{SCENE_LENGTH_RULES}}"

SCENE_LENGTH_MODE_STANDARD = "standard"
SCENE_LENGTH_MODE_LONG = "long"
SCENE_LENGTH_MODE_CHOPPY = "choppy"
SCENE_LENGTH_MODE_ADAPTIVE = "adaptive"

SCENE_LENGTH_MODE_IDS = (
    SCENE_LENGTH_MODE_STANDARD,
    SCENE_LENGTH_MODE_LONG,
    SCENE_LENGTH_MODE_CHOPPY,
    SCENE_LENGTH_MODE_ADAPTIVE,
)

SCENE_LENGTH_MODE_LABELS: dict[str, str] = {
    SCENE_LENGTH_MODE_STANDARD: "Стандарт",
    SCENE_LENGTH_MODE_LONG: "Длинные сцены",
    SCENE_LENGTH_MODE_CHOPPY: "Рваный / напряжённый",
    SCENE_LENGTH_MODE_ADAPTIVE: "Адаптивный",
}

SCENE_LENGTH_MODE_RULES: dict[str, str] = {
    SCENE_LENGTH_MODE_STANDARD: """ДЛИНА СЦЕН — СТАНДАРТ
— 1 сцена ≈ 1 предложение (~8–20 слов)
— короткое предложение → можно объединить максимум 2
— длинное предложение → обязательно разбить
— грамматику исходного текста НЕ меняешь, только ставишь границы сцен""",
    SCENE_LENGTH_MODE_LONG: """ДЛИНА СЦЕН — ДЛИННЫЕ СЦЕНЫ
— 1 сцена = 1–2 полных предложения (~20–40 слов)
— объединяй смежные предложения одной мысли
— дроби только если предложение реально длинное (3+ придаточных)
— грамматику исходного текста НЕ меняешь, только ставишь границы сцен""",
    SCENE_LENGTH_MODE_CHOPPY: """ДЛИНА СЦЕН — РВАНЫЙ / НАПРЯЖЁННЫЙ
— сцена = от 1 слова до короткой фразы (1–6 слов)
— режь на драматических ударах: отдельное слово → отдельная сцена
— цель: ощущение пульса, отрывистости, нагнетания
— грамматику текста НЕ меняешь, просто ставишь границы сцен чаще""",
    SCENE_LENGTH_MODE_ADAPTIVE: """ДЛИНА СЦЕН — АДАПТИВНЫЙ
Длину сцены выбираешь по эмоциональной интенсивности куска:
— спокойное/описательное → длинные сцены (1–2 предложения)
— нарастание → средние сцены (≈1 предложение)
— кульминация/шок/удар → короткие сцены, вплоть до 1 слова
Сам определяешь зоны по смыслу. Текст не меняешь.""",
}

SCENE_LENGTH_MODE_HINTS: dict[str, str] = {
    SCENE_LENGTH_MODE_STANDARD: (
        "1 сцена ≈ 1 предложение (~8–20 слов). "
        "Короткое предложение → можно объединить максимум 2. "
        "Длинное → обязательно разбить."
    ),
    SCENE_LENGTH_MODE_LONG: (
        "1 сцена = 1–2 полных предложения (~20–40 слов). "
        "Объединяй смежные предложения одной мысли. "
        "Дроби только при 3+ придаточных."
    ),
    SCENE_LENGTH_MODE_CHOPPY: (
        "Сцена = 1 слово … короткая фраза (1–6 слов). "
        "Режь на драматических ударах. Пульс и нагнетание."
    ),
    SCENE_LENGTH_MODE_ADAPTIVE: (
        "Спокойное → длинные сцены; нарастание → средние; "
        "кульминация → короткие, вплоть до 1 слова. Зоны по смыслу."
    ),
}

DEFAULT_SCENE_LENGTH_MODE = SCENE_LENGTH_MODE_STANDARD


def normalize_scene_length_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    aliases = {
        "стандарт": SCENE_LENGTH_MODE_STANDARD,
        "standard": SCENE_LENGTH_MODE_STANDARD,
        "long": SCENE_LENGTH_MODE_LONG,
        "длинные": SCENE_LENGTH_MODE_LONG,
        "длинные сцены": SCENE_LENGTH_MODE_LONG,
        "choppy": SCENE_LENGTH_MODE_CHOPPY,
        "рваный": SCENE_LENGTH_MODE_CHOPPY,
        "напряжённый": SCENE_LENGTH_MODE_CHOPPY,
        "adaptive": SCENE_LENGTH_MODE_ADAPTIVE,
        "адаптивный": SCENE_LENGTH_MODE_ADAPTIVE,
    }
    if v in SCENE_LENGTH_MODE_IDS:
        return v
    return aliases.get(v, DEFAULT_SCENE_LENGTH_MODE)


def scene_length_rules_text(mode: Any) -> str:
    key = normalize_scene_length_mode(mode)
    return SCENE_LENGTH_MODE_RULES.get(key, SCENE_LENGTH_MODE_RULES[DEFAULT_SCENE_LENGTH_MODE])


def scene_length_modes_for_ui() -> list[dict[str, str]]:
    return [
        {
            "id": mid,
            "label": SCENE_LENGTH_MODE_LABELS[mid],
            "hint": SCENE_LENGTH_MODE_HINTS[mid],
        }
        for mid in SCENE_LENGTH_MODE_IDS
    ]
