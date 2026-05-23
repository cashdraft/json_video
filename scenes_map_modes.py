"""Режимы MacroMap Agent (dropdowns на /scenes-map)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PH_VIDEO_DYNAMICS = "{{VIDEO_DYNAMICS}}"
PH_SCENE_TYPES = "{{SCENE_TYPES}}"
PH_ELEMENTS_USED = "{{ELEMENTS_USED}}"
PH_MACRO_MAP = "{{MACRO_MAP}}"
PH_CURRENT_BLOCK = "{{CURRENT_BLOCK}}"
PH_PREVIOUS_SCENE_TAIL = "{{PREVIOUS_SCENE_TAIL}}"

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DYNAMICS_FILE = BASE_DIR / "scenes_map" / "defaults" / "video_dynamics.txt"
ELEMENTS_USED_FILE = BASE_DIR / "scenes_map" / "defaults" / "elements_used.txt"

_PROFILE_ID_LINE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_PROFILE_HEADER = re.compile(r"^#\s*PACING PROFILE\s*[—-]\s*(.+)\s*$")
_ELEMENT_HEADER = re.compile(r"^#\s*(.+)\s*$")

_LEGACY_VIDEO_DYNAMICS: dict[str, str] = {
    "calm": "slow_documentary",
    "dynamic": "dynamic_explainer",
    "cinematic": "cinematic_slow",
}

_LEGACY_ELEMENTS: dict[str, str] = {
    "text": "kinetic_text",
    "charts": "chart_scene",
    "gauges": "chart_scene",
    "maps": "infographic_simple",
    "icons": "infographic_simple",
    "tables": "table_scene",
    "illustrations": "ai_scene",
    "animations": "svg_explainer",
}

SCENE_TYPES_MODES: dict[str, dict[str, str]] = {
    "narrative": {
        "label": "Повествовательные",
        "hint": "Сцены с текстом и сюжетной подачей.",
        "rules": "Типы сцен: повествовательные — текст и сюжетная подача.",
    },
    "data": {
        "label": "Данные / инфографика",
        "hint": "Графики, цифры, сравнения, дашборды.",
        "rules": "Типы сцен: данные и инфографика — графики, цифры, сравнения.",
    },
    "mixed": {
        "label": "Смешанные",
        "hint": "Чередование narrative и data-сцен по смыслу.",
        "rules": "Типы сцен: смешанные — narrative и data по смыслу блока.",
    },
}

DEFAULT_SCENE_TYPES = "mixed"
DEFAULT_ELEMENTS: list[str] = ["ai_scene", "kinetic_text", "chart_scene"]

# Краткие описания для dropdown (не попадают в макрос {{VIDEO_DYNAMICS}}).
VIDEO_DYNAMICS_UI_HINTS: dict[str, str] = {
    "extreme_fast": (
        "Максимально быстрый, клиповый и attention-driven монтаж. Агрессивное дробление текста, "
        "частые смены visual unit, micro scenes, короткие ударные фразы, высокий ритм удержания внимания."
    ),
    "fast_dynamic": (
        "Быстрый современный YouTube-монтаж с высокой динамикой, но без полного хаоса. "
        "Частые visual resets, короткие сцены и активный ритм, при этом сохраняется читаемость и понятность."
    ),
    "dynamic_explainer": (
        "Основной режим для explainer-видео. Баланс между динамикой и ясностью. "
        "Текст режется по visual beats и логическим шагам, но важные объяснения могут «дышать» "
        "и занимать более длинные сцены."
    ),
    "dynamic_documentary": (
        "Документальный стиль с современным монтажным ритмом. Видео ощущается серьёзным и атмосферным, "
        "но не медленным. Динамика умеренная, сцены длиннее, emphasis используется точечно."
    ),
    "slow_documentary": (
        "Спокойный, уверенный и глубокий документальный монтаж. Минимум агрессивной нарезки, "
        "длинные смысловые сцены, плавное раскрытие мыслей, акцент на доверии и погружении."
    ),
    "cinematic_slow": (
        "Максимально атмосферный и кинематографичный стиль. Очень редкая нарезка, длинные visual holds, "
        "ощущение дорогого documentary cinema, паузы, вес и эмоциональное погружение."
    ),
}


def _apply_video_dynamics_ui_hints(profiles: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    for pid, meta in profiles.items():
        meta["hint"] = VIDEO_DYNAMICS_UI_HINTS.get(pid, meta.get("hint", ""))
    return profiles


def _profile_hint(body: str) -> str:
    """Короткая подсказка для dropdown — первая строка GENERAL MEANING."""
    in_general = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "GENERAL MEANING":
            in_general = True
            continue
        if in_general and stripped:
            if stripped.endswith(":") and stripped.isupper():
                break
            return stripped[:120]
    return ""


def parse_video_dynamics_file(text: str) -> dict[str, dict[str, str]]:
    """Парсит video_dynamics.txt: id-строка + # PACING PROFILE → body до следующего профиля."""
    lines = text.splitlines(keepends=True)
    profiles: dict[str, dict[str, str]] = {}
    i = 0
    n = len(lines)

    while i < n:
        line_stripped = lines[i].strip()
        if not _PROFILE_ID_LINE.match(line_stripped):
            i += 1
            continue

        profile_name = line_stripped
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j >= n:
            break
        header_match = _PROFILE_HEADER.match(lines[j].strip())
        if not header_match:
            i += 1
            continue

        start = i
        k = j + 1
        end = n
        while k < n:
            candidate = lines[k].strip()
            if _PROFILE_ID_LINE.match(candidate):
                m = k + 1
                while m < n and not lines[m].strip():
                    m += 1
                if m < n and _PROFILE_HEADER.match(lines[m].strip()):
                    end = k
                    break
            k += 1

        body = "".join(lines[start:end]).strip()
        profile_id = profile_name.lower()
        profiles[profile_id] = {
            "label": profile_name,
            "hint": _profile_hint(body),
            "rules": body,
        }
        i = end

    return profiles


def _element_id(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")


def parse_elements_used_file(text: str) -> dict[str, dict[str, str]]:
    """Парсит elements_used.txt: # Name + описание до следующего #."""
    lines = text.splitlines()
    elements: dict[str, dict[str, str]] = {}
    i = 0
    n = len(lines)

    while i < n:
        header_match = _ELEMENT_HEADER.match(lines[i].strip())
        if not header_match:
            i += 1
            continue

        label = header_match.group(1).strip()
        element_id = _element_id(label)
        if not element_id:
            i += 1
            continue

        desc_parts: list[str] = []
        i += 1
        while i < n:
            stripped = lines[i].strip()
            if _ELEMENT_HEADER.match(stripped):
                break
            if stripped:
                desc_parts.append(stripped)
            i += 1

        description = "\n".join(desc_parts).strip()
        body = f"# {label}\n{description}".strip() if description else f"# {label}"
        elements[element_id] = {
            "label": label,
            "hint": description,
            "rules": body,
        }

    return elements


@lru_cache(maxsize=1)
def load_elements_options() -> dict[str, dict[str, str]]:
    if not ELEMENTS_USED_FILE.is_file():
        return {
            "ai_scene": {
                "label": "Ai_Scene",
                "hint": "AI-сцена / cinematic image",
                "rules": "# Ai_Scene\nAI-сцена / cinematic image",
            }
        }
    try:
        raw = ELEMENTS_USED_FILE.read_text(encoding="utf-8")
    except OSError:
        return {
            "ai_scene": {
                "label": "Ai_Scene",
                "hint": "AI-сцена / cinematic image",
                "rules": "# Ai_Scene\nAI-сцена / cinematic image",
            }
        }

    parsed = parse_elements_used_file(raw)
    if parsed:
        return parsed
    return {
        "ai_scene": {
            "label": "Ai_Scene",
            "hint": "AI-сцена / cinematic image",
            "rules": "# Ai_Scene\nAI-сцена / cinematic image",
        }
    }


def elements_options_dict() -> dict[str, dict[str, str]]:
    return load_elements_options()


@lru_cache(maxsize=1)
def load_video_dynamics_modes() -> dict[str, dict[str, str]]:
    if not VIDEO_DYNAMICS_FILE.is_file():
        return {
            "dynamic_explainer": {
                "label": "DYNAMIC_EXPLAINER",
                "hint": "",
                "rules": "Динамика видео: DYNAMIC_EXPLAINER (файл профилей не найден).",
            }
        }
    try:
        raw = VIDEO_DYNAMICS_FILE.read_text(encoding="utf-8")
    except OSError:
        return {
            "dynamic_explainer": {
                "label": "DYNAMIC_EXPLAINER",
                "hint": "",
                "rules": "Динамика видео: не удалось прочитать файл профилей.",
            }
        }

    parsed = parse_video_dynamics_file(raw)
    if parsed:
        return _apply_video_dynamics_ui_hints(parsed)
    return {
        "dynamic_explainer": {
            "label": "DYNAMIC_EXPLAINER",
            "hint": VIDEO_DYNAMICS_UI_HINTS["dynamic_explainer"],
            "rules": "Динамика видео: файл профилей пуст или не распознан.",
        }
    }


def video_dynamics_modes_dict() -> dict[str, dict[str, str]]:
    return load_video_dynamics_modes()


DEFAULT_VIDEO_DYNAMICS = "dynamic_explainer"


def _modes_for_ui(mapping: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"id": mid, "label": meta["label"], "hint": meta.get("hint", "")}
        for mid, meta in mapping.items()
    ]


def video_dynamics_modes_for_ui() -> list[dict[str, str]]:
    return _modes_for_ui(video_dynamics_modes_dict())


def video_dynamics_valid_ids() -> list[str]:
    return list(video_dynamics_modes_dict().keys())


def scene_types_modes_for_ui() -> list[dict[str, str]]:
    return _modes_for_ui(SCENE_TYPES_MODES)


def elements_options_for_ui() -> list[dict[str, str]]:
    return _modes_for_ui(elements_options_dict())


def normalize_video_dynamics(value: Any) -> str:
    modes = video_dynamics_modes_dict()
    key = str(value or "").strip().lower()
    if key in modes:
        return key
    if key in _LEGACY_VIDEO_DYNAMICS and _LEGACY_VIDEO_DYNAMICS[key] in modes:
        return _LEGACY_VIDEO_DYNAMICS[key]
    if DEFAULT_VIDEO_DYNAMICS in modes:
        return DEFAULT_VIDEO_DYNAMICS
    return next(iter(modes.keys()), DEFAULT_VIDEO_DYNAMICS)


def normalize_scene_types(value: Any) -> str:
    key = str(value or "").strip().lower()
    return key if key in SCENE_TYPES_MODES else DEFAULT_SCENE_TYPES


def normalize_elements_used(value: Any) -> list[str]:
    options = elements_options_dict()
    if isinstance(value, str):
        raw = [p.strip() for p in value.replace(";", ",").split(",")]
    elif isinstance(value, list):
        raw = [str(p).strip() for p in value]
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        key = item.lower()
        if key in options and key not in out:
            out.append(key)
            continue
        if key in _LEGACY_ELEMENTS:
            mapped = _LEGACY_ELEMENTS[key]
            if mapped in options and mapped not in out:
                out.append(mapped)
    return out or list(DEFAULT_ELEMENTS)


def video_dynamics_rules_text(mode: Any) -> str:
    key = normalize_video_dynamics(mode)
    return video_dynamics_modes_dict()[key]["rules"]


def scene_types_rules_text(mode: Any) -> str:
    key = normalize_scene_types(mode)
    return SCENE_TYPES_MODES[key]["rules"]


def elements_used_rules_text(elements: Any) -> str:
    ids = normalize_elements_used(elements)
    opts = elements_options_dict()
    labels = [opts[eid]["label"] for eid in ids if eid in opts]
    if not labels:
        return "не выбраны"
    return ", ".join(labels)
