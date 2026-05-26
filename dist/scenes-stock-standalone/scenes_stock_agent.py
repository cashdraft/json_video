"""Finder Agent — вызов модели для /scenes-stock."""

from __future__ import annotations

from typing import Any

from scenes_map_agent import (
    build_wire_payload,
    model_key_ok,
    normalize_scenes_map_model,
    scenes_map_api_ready,
    scenes_map_models_for_ui,
    _post_model_completion,
)


def finder_models_for_ui() -> list[dict[str, str]]:
    return scenes_map_models_for_ui()


def finder_api_ready() -> bool:
    return scenes_map_api_ready()


def compose_finder_user_message(*, user_prompt: str, scene: str) -> str:
    parts: list[str] = []
    up = (user_prompt or "").strip()
    if up:
        parts.append(up)
    body = (scene or "").strip()
    if body:
        parts.append("=== SCENE ===\n" + body)
    return "\n\n".join(parts).strip()


def build_finder_generation_context(prefs: dict[str, Any]) -> dict[str, Any]:
    from scenes_stock_session import apply_prompt_macros

    sys_raw = str(prefs.get("system_prompt") or "")
    user_raw = str(prefs.get("user_prompt") or "")
    return {
        "system_prompt": sys_raw,
        "user_prompt": apply_prompt_macros(user_raw, prefs),
        "scene": str(prefs.get("scene") or ""),
        "model": normalize_scenes_map_model(str(prefs.get("model") or "")),
        "source": str(prefs.get("source") or ""),
    }


def run_finder_agent(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    scene: str = "",
) -> tuple[str | None, str | None]:
    from claude_kie import is_claude_model

    mid = normalize_scenes_map_model(model)
    if not model_key_ok(mid):
        if is_claude_model(mid):
            return None, "Не задан KEYAI_API_KEY в .env (Claude)."
        return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."

    sys_p = (system_prompt or "").strip() or (
        "Ты — Finder Agent для Scenes Stock. Подбирай stock-медиа по тексту сцены. "
        "Отвечай структурированно (JSON предпочтителен)."
    )
    user_body = compose_finder_user_message(user_prompt=user_prompt, scene=scene)
    if not user_body:
        return None, "Заполните User Prompt и/или поле «Сцена»."

    return _post_model_completion(model=mid, system_prompt=sys_p, user_body=user_body)


def build_finder_wire_payload(prefs: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    from scenes_stock_session import apply_prompt_macros

    ctx = build_finder_generation_context(prefs)
    system_prompt = apply_prompt_macros(str(ctx.get("system_prompt") or ""), prefs)
    user_body = compose_finder_user_message(
        user_prompt=str(ctx.get("user_prompt") or ""),
        scene=str(ctx.get("scene") or ""),
    )
    if not user_body:
        return None, "Заполните User Prompt и/или поле «Сцена» — тело POST не формируется."
    wire = build_wire_payload(
        model=str(ctx.get("model") or ""),
        system_prompt=system_prompt,
        user_body=user_body,
    )
    return wire, None
