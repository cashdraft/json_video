"""Персистентное состояние единственного блока Later… (/scenes-lab)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
LATER_SESSION_PATH = BASE_DIR / "data" / "scenes_lab" / "later_session.json"
LATER_PREFS_PATH = BASE_DIR / "data" / "scenes_lab" / "later_prefs.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clear_later_session() -> None:
    LATER_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LATER_SESSION_PATH.is_file():
        LATER_SESSION_PATH.unlink()


def save_later_prefs(
    *,
    svg_prompt: str = "",
    scene_description: str = "",
    scene_duration_sec: str = "",
    model: str = "",
    image_url: str = "",
    img_1_prompt: str = "",
    editor_prompt: str = "",
    anim_prompt: str = "",
) -> None:
    """Сохранить поля формы Later… без ответа модели (svg промт и т.д.)."""
    LATER_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev = load_later_prefs() or {}

    def _keep(new: str, old_key: str) -> str:
        n = (new or "").strip()
        if n:
            return n
        return str(prev.get(old_key) or "").strip()

    ep = (editor_prompt or img_1_prompt or "").strip() or _keep(
        "", "editor_prompt"
    ) or _keep("", "img_1_prompt")

    payload = {
        "saved_at": _now_iso(),
        "svg_prompt": _keep(svg_prompt, "svg_prompt"),
        "scene_description": _keep(scene_description, "scene_description"),
        "scene_duration_sec": _keep(scene_duration_sec, "scene_duration_sec"),
        "model": _keep(model, "model"),
        "image_url": _keep(image_url, "image_url"),
        "editor_prompt": ep,
        "img_1_prompt": ep,
        "anim_prompt": _keep(anim_prompt, "anim_prompt"),
    }
    LATER_PREFS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_later_prefs() -> dict[str, Any] | None:
    if not LATER_PREFS_PATH.is_file():
        return None
    try:
        raw = json.loads(LATER_PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def later_prefs_for_page() -> dict[str, str]:
    """Значения по умолчанию для GET /scenes-lab (сессия → prefs → код)."""
    from scenes_lab_later import (
        DEFAULT_LATER_SCENE_DESCRIPTION,
        DEFAULT_LATER_SCENE_DURATION,
        DEFAULT_LATER_SVG_USER_TEMPLATE,
    )

    out = {
        "svg_prompt": DEFAULT_LATER_SVG_USER_TEMPLATE,
        "scene_description": DEFAULT_LATER_SCENE_DESCRIPTION,
        "scene_duration_sec": DEFAULT_LATER_SCENE_DURATION,
        "model": "",
        "image_url": "",
        "editor_prompt": "",
        "img_1_prompt": "",
        "anim_prompt": "",
    }
    prefs = load_later_prefs()
    if prefs:
        if str(prefs.get("svg_prompt") or "").strip():
            out["svg_prompt"] = str(prefs["svg_prompt"]).strip()
        if str(prefs.get("scene_description") or "").strip():
            out["scene_description"] = str(prefs["scene_description"]).strip()
        if str(prefs.get("scene_duration_sec") or "").strip():
            out["scene_duration_sec"] = str(prefs["scene_duration_sec"]).strip()
        if str(prefs.get("model") or "").strip():
            out["model"] = str(prefs["model"]).strip()
        if str(prefs.get("image_url") or "").strip():
            out["image_url"] = str(prefs["image_url"]).strip()
        ep = str(prefs.get("editor_prompt") or prefs.get("img_1_prompt") or "").strip()
        if ep:
            out["editor_prompt"] = ep
            out["img_1_prompt"] = ep
        ap = str(prefs.get("anim_prompt") or "").strip()
        if ap:
            out["anim_prompt"] = ap
    row = load_later_session()
    if row:
        if not str(out.get("svg_prompt") or "").strip() or out["svg_prompt"] == DEFAULT_LATER_SVG_USER_TEMPLATE:
            sp = str(row.get("svg_prompt") or "").strip()
            if sp:
                out["svg_prompt"] = sp
        if not str(out.get("model") or "").strip():
            sm = str(row.get("model") or "").strip()
            if sm:
                out["model"] = sm
        if not str(out.get("image_url") or "").strip():
            iu = str(row.get("image_url") or "").strip()
            if iu:
                out["image_url"] = iu
    return out


def save_later_prefs_from_body(body: dict[str, Any] | None) -> None:
    """Записать в later_prefs.json все поля формы из JSON-тела API."""
    b = body if isinstance(body, dict) else {}
    save_later_prefs(
        svg_prompt=str(b.get("svg_prompt") or ""),
        scene_description=str(b.get("scene_description") or ""),
        scene_duration_sec=str(b.get("scene_duration_sec") or ""),
        model=str(b.get("model") or ""),
        image_url=str(b.get("image_url") or ""),
        editor_prompt=str(b.get("editor_prompt") or b.get("img_1_prompt") or ""),
        img_1_prompt=str(b.get("editor_prompt") or b.get("img_1_prompt") or ""),
        anim_prompt=str(b.get("anim_prompt") or ""),
    )


def save_later_session(
    *,
    text: str,
    parsed: dict[str, Any],
    validation: dict[str, Any],
    pipeline_ok: bool,
    model: str = "",
    user_prompt: str = "",
    image_url: str = "",
    svg_prompt: str = "",
    scene_description: str = "",
    scene_duration_sec: str = "",
) -> None:
    LATER_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": _now_iso(),
        "model": (model or "").strip(),
        "user_prompt": (user_prompt or "").strip(),
        "svg_prompt": (svg_prompt or "").strip(),
        "scene_description": (scene_description or "").strip(),
        "scene_duration_sec": (scene_duration_sec or "").strip(),
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
    save_later_prefs(
        svg_prompt=svg_prompt,
        scene_description=scene_description,
        scene_duration_sec=scene_duration_sec,
        model=model,
        image_url=image_url,
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
    from scenes_lab_later import (
        compose_later_user_prompt,
        split_legacy_user_prompt,
    )

    bundle = process_later_model_response(text)
    validation = bundle["validation"]
    prefs = load_later_prefs() or {}
    svg_prompt = str(row.get("svg_prompt") or "").strip()
    scene_description = (
        str(prefs.get("scene_description") or "").strip()
        or str(row.get("scene_description") or "").strip()
    )
    scene_duration_sec = (
        str(prefs.get("scene_duration_sec") or "").strip()
        or str(row.get("scene_duration_sec") or "").strip()
    )
    if not svg_prompt and row.get("user_prompt"):
        split = split_legacy_user_prompt(str(row.get("user_prompt") or ""))
        svg_prompt = split["svg_prompt"]
        scene_description = scene_description or split["scene_description"]
        scene_duration_sec = scene_duration_sec or split["scene_duration_sec"]
    user_prompt = compose_later_user_prompt(
        svg_prompt=svg_prompt,
        scene_description=scene_description,
        scene_duration_sec=scene_duration_sec,
        user_prompt=str(row.get("user_prompt") or ""),
    )
    img_1_prompt = str(prefs.get("editor_prompt") or prefs.get("img_1_prompt") or "").strip()
    anim_prompt = str(prefs.get("anim_prompt") or "").strip()

    from scenes_lab_img_slots import list_img_slots_payload

    slots_payload = list_img_slots_payload("")

    payload = {
        "ok": True,
        "has_saved": True,
        "saved_at": row.get("saved_at"),
        "model": str(prefs.get("model") or "").strip()
        or str(row.get("model") or "").strip(),
        "user_prompt": user_prompt,
        "svg_prompt": svg_prompt,
        "scene_description": scene_description,
        "scene_duration_sec": scene_duration_sec,
        "image_url": row.get("image_url") or "",
        "img_1_prompt": img_1_prompt,
        "editor_prompt": img_1_prompt,
        "anim_prompt": anim_prompt,
        "text": text,
        "slots": slots_payload.get("slots") or [],
        "latest_slot_id": slots_payload.get("latest_id"),
        "parsed": bundle["parsed"],
        "validation": validation,
        "pipeline_ok": bool(validation.get("ok")),
        "anim_dictionary": None,
        "img_slot": None,
    }
    latest = slots_payload.get("latest_id")
    if validation.get("ok") and latest:
        from scenes_lab_img_slots import img_slot_preview_public_url

        url = img_slot_preview_public_url(latest, "")
        if url:
            payload["img_slot"] = {"ok": True, "slot_id": latest, "preview_url": url}
    try:
        from later_anim_dictionary import anim_dictionary_debug

        payload["anim_dictionary"] = anim_dictionary_debug()
    except ImportError:
        pass
    return payload
