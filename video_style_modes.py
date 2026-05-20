"""Режимы «Стиль Video» (video.prompt) для Scene Writer и макрос {{VIDEO_STYLE_RULES}}."""
from __future__ import annotations

from typing import Any

PH_VIDEO_STYLE_RULES = "{{VIDEO_STYLE_RULES}}"

VIDEO_STYLE_MANUAL = "manual"
VIDEO_STYLE_CINEMATIC = "cinematic"
VIDEO_STYLE_BY_INTENSITY = "by_intensity"

VIDEO_STYLE_MODE_IDS = (
    VIDEO_STYLE_MANUAL,
    VIDEO_STYLE_CINEMATIC,
    VIDEO_STYLE_BY_INTENSITY,
)

VIDEO_STYLE_MODE_LABELS: dict[str, str] = {
    VIDEO_STYLE_MANUAL: "Ручная анимация",
    VIDEO_STYLE_CINEMATIC: "Кинематографично",
    VIDEO_STYLE_BY_INTENSITY: "По интенсивности",
}

VIDEO_STYLE_MODE_RULES: dict[str, str] = {
    VIDEO_STYLE_MANUAL: """video.prompt: оживляй сцену так, будто анимируешь вручную.
— простые движения: лёгкий параллакс, дыхание кадра, мягкое движение элементов
— без киношных приёмов, без сложных переходов камеры
— одно ясное движение на сцену""",
    VIDEO_STYLE_CINEMATIC: """video.prompt: максимально кинематографично.
— активная работа камеры: наезды, облёты, dolly, parallax, rack focus
— атмосферный свет, переходы между сценами
— движение усиливает эмоцию текста""",
    VIDEO_STYLE_BY_INTENSITY: """video.prompt: интенсивность движения = интенсивность текста.
— спокойно → медленная камера, минимум движения
— напряжённо → резкие движения, быстрые наезды, тряска""",
}

VIDEO_STYLE_MODE_HINTS: dict[str, str] = {
    VIDEO_STYLE_MANUAL: (
        "Параллакс, дыхание кадра, одно простое движение. Без кино-переходов."
    ),
    VIDEO_STYLE_CINEMATIC: (
        "Камера, свет, dolly/parallax. Движение под эмоцию текста."
    ),
    VIDEO_STYLE_BY_INTENSITY: (
        "Спокойный текст → медленно; напряжённый → резкие наезды, тряска."
    ),
}

DEFAULT_VIDEO_STYLE_MODE = VIDEO_STYLE_MANUAL


def normalize_video_style_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    aliases = {
        "ручная": VIDEO_STYLE_MANUAL,
        "ручная анимация": VIDEO_STYLE_MANUAL,
        "manual": VIDEO_STYLE_MANUAL,
        "кино": VIDEO_STYLE_CINEMATIC,
        "кинематографично": VIDEO_STYLE_CINEMATIC,
        "cinematic": VIDEO_STYLE_CINEMATIC,
        "интенсивность": VIDEO_STYLE_BY_INTENSITY,
        "по интенсивности": VIDEO_STYLE_BY_INTENSITY,
        "by_intensity": VIDEO_STYLE_BY_INTENSITY,
    }
    if v in VIDEO_STYLE_MODE_IDS:
        return v
    return aliases.get(v, DEFAULT_VIDEO_STYLE_MODE)


def video_style_rules_text(mode: Any) -> str:
    key = normalize_video_style_mode(mode)
    return VIDEO_STYLE_MODE_RULES.get(key, VIDEO_STYLE_MODE_RULES[DEFAULT_VIDEO_STYLE_MODE])


def video_style_modes_for_ui() -> list[dict[str, str]]:
    return [
        {
            "id": mid,
            "label": VIDEO_STYLE_MODE_LABELS[mid],
            "hint": VIDEO_STYLE_MODE_HINTS[mid],
        }
        for mid in VIDEO_STYLE_MODE_IDS
    ]
