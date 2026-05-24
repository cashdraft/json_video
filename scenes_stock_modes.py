"""Режимы и макросы для /scenes-stock (Finder Agent)."""

from __future__ import annotations

from typing import Any

PH_SOURCE = "{{SOURCE}}"
PH_SCENE = "{{SCENE}}"

DEFAULT_SOURCE = "pexels"

STOCK_SOURCES: list[dict[str, str]] = [
    {"id": "pexels", "label": "Pexels", "hint": "Бесплатные stock video и photo (API)."},
]

STOCK_SOURCE_IDS = {s["id"] for s in STOCK_SOURCES}


def normalize_source(value: Any) -> str:
    return DEFAULT_SOURCE


def stock_sources_for_ui() -> list[dict[str, str]]:
    return list(STOCK_SOURCES)


def stock_source_valid_ids() -> list[str]:
    return [s["id"] for s in STOCK_SOURCES]


def stock_source_label(source_id: str | None = None) -> str:
    return "Pexels"


def source_rules_text(source_id: Any = None) -> str:
    return "Pexels (pexels) — бесплатные stock video и photo через API."
