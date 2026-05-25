"""Экспорт wire payload для /overlay-text (кнопка J)."""

from __future__ import annotations

from typing import Any

from overlay_text_agent import (
    build_overlay_wire_payload,
    build_overlay2_wire_payload,
    build_remotion_preview_wire_payload,
    build_remotion_preview2_wire_payload,
)
from overlay_text_session import load_prefs

OVERLAY_TEXT_EXPORT_ABOUT = (
    "Логика как у кнопки ↻: те же макросы {{TEXT}} / {{STYLE}} / {{DURATION_SEC}} "
    "(алиас {{SCENE_DURATION_SEC}}), то же vision-тело POST. "
    "Файл — читаемый JSON (UTF-8, отступы)."
)

REMOTION_PREVIEW_EXPORT_ABOUT = (
    "Remotion Preview Agent: то же vision-тело POST, что при ↻. "
    "Файл — читаемый JSON (UTF-8, отступы)."
)

OVERLAY_TEXT2_EXPORT_ABOUT = (
    "Overlay Text Agent 2: фото из Remotion Preview + макрос {{CM2_RESULT}}. "
    "Файл — читаемый JSON (UTF-8, отступы)."
)

REMOTION_PREVIEW2_EXPORT_ABOUT = (
    "Remotion Preview Agent 2: фото из Remotion Preview + {{CM2_RESULT}}. "
    "Файл — читаемый JSON (UTF-8, отступы)."
)


def merge_prefs_snapshot(body: dict[str, Any] | None) -> dict[str, Any]:
    prefs = load_prefs()
    if isinstance(body, dict):
        prefs.update(body)
    return prefs


def export_overlay_wire_bodies(prefs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    wire, err = build_overlay_wire_payload(prefs)
    if err or wire is None:
        return [], [f"[Overlay Text] {err or 'тело POST не формируется'}"], None
    return [wire], [], None


def export_remotion_preview_wire_bodies(
    prefs: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    wire, err = build_remotion_preview_wire_payload(prefs)
    if err or wire is None:
        msg = err or "Прикрепите фото и заполните User Prompt — тело POST не формируется."
        return [], [f"[Remotion Preview] {msg}"], msg
    return [wire], [], None


def export_overlay2_wire_bodies(prefs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    wire, err = build_overlay2_wire_payload(prefs)
    if err or wire is None:
        return [], [f"[Overlay Text Agent 2] {err or 'тело POST не формируется'}"], None
    return [wire], [], None


def export_remotion_preview2_wire_bodies(
    prefs: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    wire, err = build_remotion_preview2_wire_payload(prefs)
    if err or wire is None:
        msg = err or "Нет фото Remotion Preview / User Prompt — тело POST не формируется."
        return [], [f"[Remotion Preview Agent 2] {msg}"], msg
    return [wire], [], None
