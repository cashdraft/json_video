"""Режимы «Стиль Image» (start.prompt) для Scene Writer и макрос {{IMAGE_STYLE_RULES}}."""
from __future__ import annotations

from typing import Any

PH_IMAGE_STYLE_RULES = "{{IMAGE_STYLE_RULES}}"

IMAGE_STYLE_HERO_EVERYWHERE = "hero_everywhere"
IMAGE_STYLE_DATA_INFOGRAPHIC = "data_infographic"
IMAGE_STYLE_HYBRID = "hybrid"

IMAGE_STYLE_MODE_IDS = (
    IMAGE_STYLE_HERO_EVERYWHERE,
    IMAGE_STYLE_DATA_INFOGRAPHIC,
    IMAGE_STYLE_HYBRID,
)

IMAGE_STYLE_MODE_LABELS: dict[str, str] = {
    IMAGE_STYLE_HERO_EVERYWHERE: "Герой везде",
    IMAGE_STYLE_DATA_INFOGRAPHIC: "Данные и инфографика",
    IMAGE_STYLE_HYBRID: "Гибрид",
}

IMAGE_STYLE_MODE_RULES: dict[str, str] = {
    IMAGE_STYLE_HERO_EVERYWHERE: """start.prompt: наш герой — главный субъект действия в КАЖДОЙ сцене.
— подробно опиши героя, его действие, окружение, свет, ракурс
— максимально детально, чтобы nano banana не ошиблась в сцене
— придерживайся единого визуального облика героя во всех сценах""",
    IMAGE_STYLE_DATA_INFOGRAPHIC: """start.prompt: опирайся на графики, реальные числа, диаграммы, инфографику.
— если в тексте есть цифры/факты — визуализируй их (графики, таймлайны, схемы)
— герой появляется редко, только как акцент
— чисто, читаемо, как в объясняющем ролике""",
    IMAGE_STYLE_HYBRID: """start.prompt: чередуй.
— смысловые/числовые куски → инфографика и графики с реальными числами
— эмоциональные/сюжетные куски → наш герой как субъект действия
— решай по содержанию конкретной сцены""",
}

IMAGE_STYLE_MODE_HINTS: dict[str, str] = {
    IMAGE_STYLE_HERO_EVERYWHERE: (
        "Герой — субъект в каждой сцене. Детально: действие, свет, ракурс, единый облик."
    ),
    IMAGE_STYLE_DATA_INFOGRAPHIC: (
        "Графики, числа, схемы. Герой редко, как акцент. Чисто, как explain-ролик."
    ),
    IMAGE_STYLE_HYBRID: (
        "Числа и факты → инфографика; сюжет и эмоции → герой. По смыслу сцены."
    ),
}

DEFAULT_IMAGE_STYLE_MODE = IMAGE_STYLE_HERO_EVERYWHERE


def normalize_image_style_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    aliases = {
        "герой везде": IMAGE_STYLE_HERO_EVERYWHERE,
        "hero": IMAGE_STYLE_HERO_EVERYWHERE,
        "hero_everywhere": IMAGE_STYLE_HERO_EVERYWHERE,
        "данные": IMAGE_STYLE_DATA_INFOGRAPHIC,
        "инфографика": IMAGE_STYLE_DATA_INFOGRAPHIC,
        "data": IMAGE_STYLE_DATA_INFOGRAPHIC,
        "data_infographic": IMAGE_STYLE_DATA_INFOGRAPHIC,
        "гибрид": IMAGE_STYLE_HYBRID,
        "hybrid": IMAGE_STYLE_HYBRID,
    }
    if v in IMAGE_STYLE_MODE_IDS:
        return v
    return aliases.get(v, DEFAULT_IMAGE_STYLE_MODE)


def image_style_rules_text(mode: Any) -> str:
    key = normalize_image_style_mode(mode)
    return IMAGE_STYLE_MODE_RULES.get(key, IMAGE_STYLE_MODE_RULES[DEFAULT_IMAGE_STYLE_MODE])


def image_style_modes_for_ui() -> list[dict[str, str]]:
    return [
        {
            "id": mid,
            "label": IMAGE_STYLE_MODE_LABELS[mid],
            "hint": IMAGE_STYLE_MODE_HINTS[mid],
        }
        for mid in IMAGE_STYLE_MODE_IDS
    ]
