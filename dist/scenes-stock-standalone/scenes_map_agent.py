"""Агенты Scenes Map — вызов модели для /scenes-map."""

from __future__ import annotations

import json
import os
from typing import Any

from claude_kie import CLAUDE_MODELS, is_claude_model, kie_api_key_present
from rewrite_openai import (
    _chat_timeout_seconds,
    _post_chat_completion_sync,
    rewrite_chat_completion_wire_payload,
)

SCENES_MAP_MODELS: list[dict[str, str]] = [
    {"id": "gpt-4", "label": "ChatGPT 4"},
    {"id": "gpt-4.1", "label": "ChatGPT 4.1"},
    {"id": "gpt-5.4", "label": "ChatGPT 5.4"},
    *CLAUDE_MODELS,
]

SCENES_MAP_MODEL_IDS = {m["id"] for m in SCENES_MAP_MODELS}
SCENES_MAP_DEFAULT_MODEL = "gpt-5.4"


def scenes_map_models_for_ui() -> list[dict[str, str]]:
    return list(SCENES_MAP_MODELS)


def normalize_scenes_map_model(model: str) -> str:
    mid = (model or "").strip()
    return mid if mid in SCENES_MAP_MODEL_IDS else SCENES_MAP_DEFAULT_MODEL


def openai_api_key_present() -> bool:
    return bool((os.getenv("OPENAI_API_KEY") or "").strip())


def scenes_map_api_ready() -> bool:
    return openai_api_key_present() or kie_api_key_present()


def model_key_ok(model: str) -> bool:
    mid = normalize_scenes_map_model(model)
    if is_claude_model(mid):
        return kie_api_key_present()
    return openai_api_key_present()


SCENEMAP_SYSTEM_PROMPT_DEFAULT = (
    "Ты — SceneMap Agent. Разбивай macro block на сцены. "
    "Отвечай только JSONL: одна строка = один JSON-объект сцены."
)

# Обратная совместимость
SCENEMAP_SYSTEM_PROMPT = SCENEMAP_SYSTEM_PROMPT_DEFAULT


def resolve_scenemap_system_prompt(prefs: dict[str, Any]) -> str:
    from scenes_map_session import apply_prompt_macros

    raw = str(prefs.get("scenemap_system_prompt") or "").strip()
    if not raw:
        raw = SCENEMAP_SYSTEM_PROMPT_DEFAULT
    return apply_prompt_macros(raw, prefs, agent="scenemap")


def build_wire_payload(*, model: str, system_prompt: str, user_body: str) -> dict[str, Any]:
    """Тело POST к OpenAI/Claude — как в _post_model_completion, без HTTP."""
    mid = normalize_scenes_map_model(model)
    if is_claude_model(mid):
        return rewrite_chat_completion_wire_payload(mid, system_prompt, user_body)
    from rewrite_openai import openai_chat_completions_request_dict

    payload = openai_chat_completions_request_dict(mid, system_prompt, user_body, sanitize=True)
    payload["model"] = mid
    return payload


def _post_model_completion(*, model: str, system_prompt: str, user_body: str) -> tuple[str | None, str | None]:
    mid = normalize_scenes_map_model(model)
    payload = build_wire_payload(model=mid, system_prompt=system_prompt, user_body=user_body)
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if is_claude_model(mid):
        from claude_kie import _kie_api_key

        api_key = _kie_api_key()
    return _post_chat_completion_sync(api_key, payload, _chat_timeout_seconds())


def compose_macromap_user_message(*, user_prompt: str, inbox: str) -> str:
    parts: list[str] = []
    up = (user_prompt or "").strip()
    if up:
        parts.append(up)
    box = (inbox or "").strip()
    if box:
        parts.append("=== INBOX ===\n" + box)
    return "\n\n".join(parts).strip()


def run_macromap_agent(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    inbox: str = "",
) -> tuple[str | None, str | None]:
    mid = normalize_scenes_map_model(model)
    if not model_key_ok(mid):
        if is_claude_model(mid):
            return None, "Не задан KEYAI_API_KEY в .env (Claude)."
        return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."

    sys_p = (system_prompt or "").strip() or "Ты — MacroMap Agent. Отвечай порационально и структурированно."
    user_body = compose_macromap_user_message(user_prompt=user_prompt, inbox=inbox)
    if not user_body:
        return None, "Заполните User Prompt и/или Inbox."

    return _post_model_completion(model=mid, system_prompt=sys_p, user_body=user_body)


def build_generation_context(prefs: dict[str, Any]) -> dict[str, Any]:
    from scenes_map_session import apply_prompt_macros

    sys_raw = str(prefs.get("system_prompt") or "")
    user_raw = str(prefs.get("user_prompt") or "")
    return {
        "system_prompt": sys_raw,
        "user_prompt": apply_prompt_macros(user_raw, prefs),
        "inbox": str(prefs.get("inbox") or ""),
        "model": normalize_scenes_map_model(str(prefs.get("model") or "")),
    }


def compose_scenemap_block_user_message(
    *,
    user_prompt: str,
    macro_map_no_text: list[dict[str, Any]] | None = None,
    current_block: dict[str, Any] | None = None,
    previous_tail: list[dict[str, Any]] | None = None,
    global_summary: dict[str, Any] | None = None,
) -> str:
    from scenes_map_pipeline import compose_scenemap_block_message

    return compose_scenemap_block_message(
        user_prompt=user_prompt,
        macro_map_no_text=macro_map_no_text or [],
        current_block=current_block or {},
        previous_tail=previous_tail or [],
        global_summary=global_summary,
    )


def run_scenemap_block_agent(
    *,
    model: str,
    user_prompt: str,
    macro_map_no_text: list[dict[str, Any]] | None = None,
    current_block: dict[str, Any] | None = None,
    previous_tail: list[dict[str, Any]] | None = None,
    global_summary: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    mid = normalize_scenes_map_model(model)
    if not model_key_ok(mid):
        if is_claude_model(mid):
            return None, "Не задан KEYAI_API_KEY в .env (Claude)."
        return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."

    sys_p = SCENEMAP_SYSTEM_PROMPT
    user_body = compose_scenemap_block_user_message(
        user_prompt=user_prompt,
        macro_map_no_text=macro_map_no_text,
        current_block=current_block,
        previous_tail=previous_tail,
        global_summary=global_summary,
    )
    if not user_body:
        return None, "Пустой запрос SceneMap Agent."

    return _post_model_completion(model=mid, system_prompt=sys_p, user_body=user_body)


def build_scenemap_generation_context(prefs: dict[str, Any]) -> dict[str, Any]:
    from scenes_map_session import apply_prompt_macros

    user_raw = str(prefs.get("scenemap_user_prompt") or "")
    return {
        "system_prompt": resolve_scenemap_system_prompt(prefs),
        "user_prompt": apply_prompt_macros(user_raw, prefs, agent="scenemap"),
        "result_as": str(prefs.get("result_as") or ""),
        "model": normalize_scenes_map_model(str(prefs.get("scenemap_model") or "")),
    }


def scenemap_run_agent_adapter(
    *,
    model: str,
    user_prompt: str,
    system_prompt: str | None = None,
    **_: Any,
) -> tuple[str | None, str | None]:
    """Адаптер для pipeline: user_prompt уже содержит полное тело запроса."""
    mid = normalize_scenes_map_model(model)
    if not model_key_ok(mid):
        if is_claude_model(mid):
            return None, "Не задан KEYAI_API_KEY в .env (Claude)."
        return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."

    sys_p = (system_prompt or "").strip() or SCENEMAP_SYSTEM_PROMPT_DEFAULT
    return _post_model_completion(model=mid, system_prompt=sys_p, user_body=user_prompt)
