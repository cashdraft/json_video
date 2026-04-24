from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
ELEVENLABS_TEMPLATES_DIR = MODULE_DIR / "elevenlabs_templates"


def _safe_template_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    # Allow simple readable names only.
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,80}", s):
        return ""
    return s


def _template_path(name: str) -> Path | None:
    safe = _safe_template_name(name)
    if not safe:
        return None
    return ELEVENLABS_TEMPLATES_DIR / f"{safe}.json"


def list_elevenlabs_template_names() -> list[str]:
    root = ELEVENLABS_TEMPLATES_DIR
    if not root.is_dir():
        return []
    names: list[str] = []
    for p in root.glob("*.json"):
        if not p.is_file():
            continue
        nm = _safe_template_name(p.stem)
        if nm:
            names.append(nm)
    return sorted(set(names), key=str.lower)


def load_elevenlabs_template(name: str) -> dict[str, Any] | None:
    p = _template_path(name)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "name": p.stem,
        "model_id": str(data.get("model_id") or ""),
        "voice_id": str(data.get("voice_id") or ""),
        "voice_name": str(data.get("voice_name") or ""),
        "speed_pct": int(data.get("speed_pct") or 50),
        "stability_pct": int(data.get("stability_pct") or 50),
        "similarity_pct": int(data.get("similarity_pct") or 75),
        "style_pct": int(data.get("style_pct") or 0),
        "use_speaker_boost": bool(data.get("use_speaker_boost", True)),
    }


def save_elevenlabs_template(name: str, data: dict[str, Any]) -> tuple[bool, str]:
    p = _template_path(name)
    if p is None:
        return False, "bad_name"
    ELEVENLABS_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_id": str(data.get("model_id") or ""),
        "voice_id": str(data.get("voice_id") or ""),
        "voice_name": str(data.get("voice_name") or ""),
        "speed_pct": max(0, min(100, int(data.get("speed_pct") or 50))),
        "stability_pct": max(0, min(100, int(data.get("stability_pct") or 50))),
        "similarity_pct": max(0, min(100, int(data.get("similarity_pct") or 75))),
        "style_pct": max(0, min(100, int(data.get("style_pct") or 0))),
        "use_speaker_boost": bool(data.get("use_speaker_boost", True)),
    }
    try:
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False, "write_failed"
    return True, ""
