"""Персистентное состояние единственного блока Later… (/scenes-lab)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
LATER_SESSION_PATH = BASE_DIR / "data" / "scenes_lab" / "later_session.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clear_later_session() -> None:
    LATER_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LATER_SESSION_PATH.is_file():
        LATER_SESSION_PATH.unlink()


def save_later_session(
    *,
    text: str,
    parsed: dict[str, Any],
    validation: dict[str, Any],
    pipeline_ok: bool,
    model: str = "",
    user_prompt: str = "",
    image_url: str = "",
) -> None:
    LATER_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": _now_iso(),
        "model": (model or "").strip(),
        "user_prompt": (user_prompt or "").strip(),
        "image_url": (image_url or "").strip(),
        "text": text or "",
        "parsed": parsed,
        "validation": validation,
        "pipeline_ok": bool(pipeline_ok),
    }
    LATER_SESSION_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_later_session() -> dict[str, Any] | None:
    if not LATER_SESSION_PATH.is_file():
        return None
    try:
        raw = json.loads(LATER_SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
        return None
    return raw


def later_session_api_payload() -> dict[str, Any]:
    """Формат для GET /api/state и восстановления UI."""
    row = load_later_session()
    if not row:
        return {"ok": True, "has_saved": False}
    text = str(row.get("text") or "")
    from later_response_parse import process_later_model_response

    bundle = process_later_model_response(text)
    validation = bundle["validation"]
    payload = {
        "ok": True,
        "has_saved": True,
        "saved_at": row.get("saved_at"),
        "model": row.get("model") or "",
        "user_prompt": row.get("user_prompt") or "",
        "image_url": row.get("image_url") or "",
        "text": text,
        "parsed": bundle["parsed"],
        "validation": validation,
        "pipeline_ok": bool(validation.get("ok")),
        "anim_dictionary": None,
    }
    try:
        from later_anim_dictionary import anim_dictionary_debug

        payload["anim_dictionary"] = anim_dictionary_debug()
    except ImportError:
        pass
    return payload
