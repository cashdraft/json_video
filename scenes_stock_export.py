"""Экспорт wire payload OpenAI для /scenes-stock (кнопка J)."""

from __future__ import annotations

from typing import Any

from scenes_stock_agent import build_finder_wire_payload
from scenes_stock_session import load_prefs

SCENES_STOCK_EXPORT_ABOUT = (
    "Логика входов как у кнопки ↻: тот же JSON со страницы (collectPayload), на сервере те же "
    "apply_prompt_macros и сборка user/system, что и в POST /scenes-stock/api/generate. "
    "Дальше: для одного POST — то же, что перед HTTP: "
    "openai_chat_completions_request_dict / rewrite_chat_completion_wire_payload "
    "(нормализация model, sanitize на system/user, temperature=REWRITE_CHAT_TEMPERATURE). "
    "Файл — читаемый JSON (UTF-8, отступы); реальное тело POST кодируется компактнее."
)


def merge_prefs_snapshot(body: dict[str, Any] | None) -> dict[str, Any]:
    prefs = load_prefs()
    if isinstance(body, dict):
        prefs.update(body)
    return prefs


def export_finder_wire_bodies(prefs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    wire, err = build_finder_wire_payload(prefs)
    if err or wire is None:
        return [], [f"[Finder] {err or 'тело POST не формируется'}"], None
    return [wire], [], None
