"""Персистентное состояние /overlay-text."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PH_TEXT = "{{TEXT}}"
PH_OVERLAY_TEXT = "{{OVERLAY_TEXT}}"
PH_STYLE = "{{STYLE}}"
PH_DURATION_SEC = "{{DURATION_SEC}}"
PH_SCENE_DURATION_SEC = "{{SCENE_DURATION_SEC}}"

BASE_DIR = Path(__file__).resolve().parent
OVERLAY_DATA_DIR = BASE_DIR / "data" / "overlay_text"
PREFS_PATH = OVERLAY_DATA_DIR / "prefs.json"
DEFAULTS_DIR = BASE_DIR / "overlay_text" / "defaults"


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
        "system_prompt": _read_default("system_prompt.txt"),
        "user_prompt": _read_default("user_prompt.txt"),
        "text": "",
        "style": "",
        "duration_sec": "",
        "result": "",
        "image_url": "",
        "image_preview_url": "",
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
    return base


def save_prefs(data: dict[str, Any]) -> dict[str, Any]:
    prev = load_prefs()
    payload = dict(prev)
    for key in (
        "model",
        "system_prompt",
        "user_prompt",
        "text",
        "style",
        "duration_sec",
        "result",
        "image_url",
        "image_preview_url",
    ):
        if key not in data:
            continue
        val = data[key]
        if key in ("text", "style", "duration_sec", "result", "image_url", "image_preview_url"):
            payload[key] = str(val or "")
        else:
            payload[key] = str(val or "").strip()
    payload["saved_at"] = _now_iso()
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def prefs_for_page() -> dict[str, Any]:
    return load_prefs()


def apply_prompt_macros(text: str, prefs: dict[str, Any]) -> str:
    s = str(text or "")
    text_val = str(prefs.get("text") or "").strip()
    dur_val = str(prefs.get("duration_sec") or "").strip()
    s = s.replace(PH_TEXT, text_val)
    s = s.replace(PH_OVERLAY_TEXT, text_val)
    s = s.replace(PH_STYLE, str(prefs.get("style") or "").strip())
    s = s.replace(PH_DURATION_SEC, dur_val)
    s = s.replace(PH_SCENE_DURATION_SEC, dur_val)
    return s
