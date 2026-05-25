"""Overlay Text Agent — вызов модели для /overlay-text (фото + текст + стиль)."""

from __future__ import annotations

from typing import Any

from claude_kie import is_claude_model
from scenes_map_agent import (
    model_key_ok,
    normalize_scenes_map_model,
    scenes_map_api_ready,
    scenes_map_models_for_ui,
)
from scenes_lab_later import (
    claude_messages_wire_payload,
    openai_vision_wire_payload,
    post_claude_messages_sync,
    post_openai_chat_sync,
)


def overlay_models_for_ui() -> list[dict[str, str]]:
    return scenes_map_models_for_ui()


def overlay_api_ready() -> bool:
    return scenes_map_api_ready()


def compose_overlay_user_message(*, user_prompt: str) -> str:
    """User message = только User Prompt (макросы уже развёрнуты) + картинка в wire."""
    return (user_prompt or "").strip()


def build_overlay_generation_context(prefs: dict[str, Any]) -> dict[str, Any]:
    from overlay_text_session import apply_prompt_macros

    return {
        "system_prompt": apply_prompt_macros(str(prefs.get("system_prompt") or ""), prefs),
        "user_prompt": apply_prompt_macros(str(prefs.get("user_prompt") or ""), prefs),
        "text": str(prefs.get("text") or ""),
        "style": str(prefs.get("style") or ""),
        "duration_sec": str(prefs.get("duration_sec") or ""),
        "image_url": str(prefs.get("image_url") or "").strip(),
        "model": normalize_scenes_map_model(str(prefs.get("model") or "")),
    }


def run_overlay_agent(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    text: str = "",
    style: str = "",
    duration_sec: str = "",
    image_url: str = "",
) -> tuple[str | None, str | None]:
    mid = normalize_scenes_map_model(model)
    if not model_key_ok(mid):
        if is_claude_model(mid):
            return None, "Не задан KEYAI_API_KEY в .env (Claude)."
        return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."

    img = (image_url or "").strip()
    if not img:
        return None, "Прикрепите фото."

    sys_p = (system_prompt or "").strip() or (
        "Ты — Overlay Text Agent. По фото и входным данным верни JSON с overlay-текстами."
    )
    user_body = compose_overlay_user_message(user_prompt=user_prompt)
    if not user_body:
        return None, "Заполните User Prompt."

    if is_claude_model(mid):
        payload = claude_messages_wire_payload(
            mid,
            sys_p,
            user_body,
            image_url=img,
        )
        return post_claude_messages_sync(payload, timeout=300)

    payload = openai_vision_wire_payload(mid, sys_p, user_body, img)
    return post_openai_chat_sync(payload, timeout=300)


def resolve_remotion_preview_image_url(prefs: dict[str, Any]) -> str:
    """Только фото Remotion Preview Agent — не image_url верхнего Overlay Text."""
    url = str(prefs.get("rp_image_url") or "").strip()
    if not url:
        url = str(prefs.get("rp_image_preview_url") or "").strip()
    return url


def build_remotion_preview_context(prefs: dict[str, Any]) -> dict[str, Any]:
    return {
        "system_prompt": str(prefs.get("rp_system_prompt") or "").strip(),
        "user_prompt": str(prefs.get("rp_user_prompt") or "").strip(),
        "image_url": resolve_remotion_preview_image_url(prefs),
        "model": normalize_scenes_map_model(str(prefs.get("rp_model") or "")),
    }


def run_remotion_preview_agent(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    image_url: str = "",
) -> tuple[str | None, str | None]:
    mid = normalize_scenes_map_model(model)
    if not model_key_ok(mid):
        if is_claude_model(mid):
            return None, "Не задан KEYAI_API_KEY в .env (Claude)."
        return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."

    img = (image_url or "").strip()
    if not img:
        return None, "Прикрепите фото."

    sys_p = (system_prompt or "").strip() or (
        "Ты — Remotion Preview Agent. По фото верни JSON по user prompt."
    )
    user_body = (user_prompt or "").strip()
    if not user_body:
        return None, "Заполните User Prompt."

    if is_claude_model(mid):
        payload = claude_messages_wire_payload(mid, sys_p, user_body, image_url=img)
        return post_claude_messages_sync(payload, timeout=300)

    payload = openai_vision_wire_payload(mid, sys_p, user_body, img)
    return post_openai_chat_sync(payload, timeout=300)


def build_remotion_preview_wire_payload(prefs: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    ctx = build_remotion_preview_context(prefs)
    mid = str(ctx.get("model") or "")
    img = str(ctx.get("image_url") or "").strip()
    system_prompt = str(ctx.get("system_prompt") or "")
    user_body = str(ctx.get("user_prompt") or "").strip()
    if not img:
        return None, "Прикрепите фото — тело POST с vision не формируется."
    if not user_body:
        return None, "Заполните User Prompt."

    if is_claude_model(mid):
        return claude_messages_wire_payload(mid, system_prompt, user_body, image_url=img), None
    return openai_vision_wire_payload(mid, system_prompt, user_body, img), None


def build_overlay_wire_payload(prefs: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    ctx = build_overlay_generation_context(prefs)
    mid = str(ctx.get("model") or "")
    img = str(ctx.get("image_url") or "").strip()
    system_prompt = str(ctx.get("system_prompt") or "")
    user_body = compose_overlay_user_message(user_prompt=str(ctx.get("user_prompt") or ""))
    if not img:
        return None, "Прикрепите фото — тело POST с vision не формируется."
    if not user_body:
        return None, "Заполните User Prompt."

    if is_claude_model(mid):
        return claude_messages_wire_payload(mid, system_prompt, user_body, image_url=img), None
    return openai_vision_wire_payload(mid, system_prompt, user_body, img), None
