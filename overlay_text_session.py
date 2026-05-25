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

# Зеркало промптов в git-tracked defaults/ (для push на GitHub)
PROMPT_DEFAULT_FILES: dict[str, str] = {
    "system_prompt": "system_prompt.txt",
    "user_prompt": "user_prompt.txt",
    "rp_system_prompt": "remotion_preview_system_prompt.txt",
    "rp_user_prompt": "remotion_preview_user_prompt.txt",
}


def _write_prompt_file(path: Path, text: str) -> None:
    body = str(text or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    if body.strip():
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
    else:
        path.write_text("", encoding="utf-8")


def sync_prompts_to_defaults(payload: dict[str, Any] | None = None) -> list[str]:
    """Сохранить system/user промты Overlay + Remotion Preview в overlay_text/defaults/."""
    data = payload if isinstance(payload, dict) else load_prefs()
    written: list[str] = []
    for key, fname in PROMPT_DEFAULT_FILES.items():
        if key not in data:
            continue
        path = DEFAULTS_DIR / fname
        _write_prompt_file(path, str(data.get(key) or ""))
        written.append(str(path.relative_to(BASE_DIR)))
    return written


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
        "rp_model": "gpt-5.4",
        "rp_system_prompt": _read_default("remotion_preview_system_prompt.txt"),
        "rp_user_prompt": _read_default("remotion_preview_user_prompt.txt"),
        "rp_result": "",
        "rp_image_url": "",
        "rp_image_preview_url": "",
        "rp_dino_result": "",
        "rp_dino_annotated_url": "",
        "rp_sam2_result": "",
        "rp_sam2_preview_url": "",
        "rp_sam2_auto_result": "",
        "rp_sam2_auto_preview_url": "",
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


RP_PREF_KEYS = (
    "rp_model",
    "rp_system_prompt",
    "rp_user_prompt",
    "rp_result",
    "rp_image_url",
    "rp_image_preview_url",
    "rp_dino_result",
    "rp_dino_annotated_url",
    "rp_sam2_result",
    "rp_sam2_preview_url",
    "rp_sam2_auto_result",
    "rp_sam2_auto_preview_url",
)


def merge_rp_prefs(prefs: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Снимок prefs для Remotion Preview: только rp_* из body поверх сохранённых."""
    out = dict(prefs)
    if not isinstance(body, dict):
        return out
    for key in RP_PREF_KEYS:
        if key not in body:
            continue
        val = body[key]
        if key in ("rp_result", "rp_image_url", "rp_image_preview_url", "rp_dino_result", "rp_dino_annotated_url", "rp_sam2_result", "rp_sam2_preview_url", "rp_sam2_auto_result", "rp_sam2_auto_preview_url"):
            out[key] = str(val or "")
        else:
            out[key] = str(val or "").strip()
    return out


def save_rp_prefs(data: dict[str, Any]) -> dict[str, Any]:
    prev = load_prefs()
    payload = dict(prev)
    for key in RP_PREF_KEYS:
        if key not in data:
            continue
        val = data[key]
        if key in ("rp_result", "rp_image_url", "rp_image_preview_url", "rp_dino_result", "rp_dino_annotated_url", "rp_sam2_result", "rp_sam2_preview_url", "rp_sam2_auto_result", "rp_sam2_auto_preview_url"):
            payload[key] = str(val or "")
        else:
            payload[key] = str(val or "").strip()
    payload["saved_at"] = _now_iso()
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_prompts_to_defaults(payload)
    return payload


def save_prefs(data: dict[str, Any]) -> dict[str, Any]:
    prev = load_prefs()
    payload = dict(prev)
    _preserve_if_empty = ("rp_dino_result", "rp_dino_annotated_url", "rp_sam2_result", "rp_sam2_preview_url", "rp_sam2_auto_result", "rp_sam2_auto_preview_url")
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
        "rp_model",
        "rp_system_prompt",
        "rp_user_prompt",
        "rp_result",
        "rp_image_url",
        "rp_image_preview_url",
        "rp_dino_result",
        "rp_dino_annotated_url",
        "rp_sam2_result",
        "rp_sam2_preview_url",
        "rp_sam2_auto_result",
        "rp_sam2_auto_preview_url",
    ):
        if key not in data:
            continue
        val = data[key]
        if key in _preserve_if_empty and not str(val or "").strip():
            if str(payload.get(key) or "").strip():
                continue
        if key in (
            "text",
            "style",
            "duration_sec",
            "result",
            "image_url",
            "image_preview_url",
            "rp_result",
            "rp_image_url",
            "rp_image_preview_url",
            "rp_dino_result",
            "rp_dino_annotated_url",
            "rp_sam2_result",
            "rp_sam2_preview_url",
            "rp_sam2_auto_result",
            "rp_sam2_auto_preview_url",
        ):
            payload[key] = str(val or "")
        else:
            payload[key] = str(val or "").strip()
    payload["saved_at"] = _now_iso()
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_prompts_to_defaults(payload)
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
