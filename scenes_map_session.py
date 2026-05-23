"""Персистентное состояние /scenes-map (MacroMap Agent и далее)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scenes_map_modes import (
    DEFAULT_ELEMENTS,
    DEFAULT_SCENE_TYPES,
    DEFAULT_VIDEO_DYNAMICS,
    elements_used_rules_text,
    normalize_elements_used,
    normalize_scene_types,
    normalize_video_dynamics,
    scene_types_rules_text,
    video_dynamics_rules_text,
)

BASE_DIR = Path(__file__).resolve().parent
SCENES_MAP_DATA_DIR = BASE_DIR / "data" / "scenes_map"
PREFS_PATH = SCENES_MAP_DATA_DIR / "prefs.json"
DEFAULTS_DIR = BASE_DIR / "scenes_map" / "defaults"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_default(name: str) -> str:
    path = DEFAULTS_DIR / name
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def default_prefs() -> dict[str, Any]:
    return {
        "saved_at": "",
        "model": "gpt-5.4",
        "video_dynamics_mode": DEFAULT_VIDEO_DYNAMICS,
        "scene_types_mode": DEFAULT_SCENE_TYPES,
        "elements_used": list(DEFAULT_ELEMENTS),
        "system_prompt": _read_default("system_prompt.txt"),
        "user_prompt": _read_default("user_prompt.txt"),
        "inbox": "",
        "result": "",
        "result_as": "",
        "scenemap_model": "gpt-5.4",
        "scenemap_video_dynamics_mode": DEFAULT_VIDEO_DYNAMICS,
        "scenemap_elements_used": list(DEFAULT_ELEMENTS),
        "scenemap_system_prompt": _read_default("scenemap_system_prompt.txt"),
        "scenemap_user_prompt": _read_default("scenemap_user_prompt.txt"),
        "scenemap_result": "",
    }


def load_prefs() -> dict[str, Any]:
    if not PREFS_PATH.is_file():
        return default_prefs()
    try:
        raw = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default_prefs()
    if not isinstance(raw, dict):
        return default_prefs()
    base = default_prefs()
    base.update(raw)
    base["video_dynamics_mode"] = normalize_video_dynamics(base.get("video_dynamics_mode"))
    base["scene_types_mode"] = normalize_scene_types(base.get("scene_types_mode"))
    base["elements_used"] = normalize_elements_used(base.get("elements_used"))
    base["scenemap_video_dynamics_mode"] = normalize_video_dynamics(base.get("scenemap_video_dynamics_mode"))
    base["scenemap_elements_used"] = normalize_elements_used(base.get("scenemap_elements_used"))
    return base


def save_prefs(data: dict[str, Any]) -> dict[str, Any]:
    prev = load_prefs()
    payload = dict(prev)
    for key in (
        "model",
        "video_dynamics_mode",
        "scene_types_mode",
        "elements_used",
        "system_prompt",
        "user_prompt",
        "inbox",
        "result",
        "result_as",
        "scenemap_model",
        "scenemap_video_dynamics_mode",
        "scenemap_elements_used",
        "scenemap_system_prompt",
        "scenemap_user_prompt",
        "scenemap_result",
    ):
        if key not in data:
            continue
        val = data[key]
        if key in ("elements_used", "scenemap_elements_used"):
            payload[key] = normalize_elements_used(val)
        elif key in ("video_dynamics_mode", "scenemap_video_dynamics_mode"):
            payload[key] = normalize_video_dynamics(val)
        elif key in ("scene_types_mode",):
            payload[key] = normalize_scene_types(val)
        elif key in ("result", "result_as", "scenemap_result"):
            payload[key] = str(val or "")
        else:
            payload[key] = str(val or "").strip()
    payload["saved_at"] = _now_iso()
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def prefs_for_page() -> dict[str, Any]:
    return load_prefs()


def apply_prompt_macros(text: str, prefs: dict[str, Any], *, agent: str = "macromap") -> str:
    from scenes_map_modes import PH_ELEMENTS_USED, PH_SCENE_TYPES, PH_VIDEO_DYNAMICS

    s = str(text or "")
    if agent == "scenemap":
        s = s.replace(
            PH_VIDEO_DYNAMICS,
            video_dynamics_rules_text(prefs.get("scenemap_video_dynamics_mode")),
        )
        s = s.replace(
            PH_ELEMENTS_USED,
            elements_used_rules_text(prefs.get("scenemap_elements_used")),
        )
        return s

    s = s.replace(PH_VIDEO_DYNAMICS, video_dynamics_rules_text(prefs.get("video_dynamics_mode")))
    s = s.replace(PH_SCENE_TYPES, scene_types_rules_text(prefs.get("scene_types_mode")))
    s = s.replace(PH_ELEMENTS_USED, elements_used_rules_text(prefs.get("elements_used")))
    return s
