"""Персистентное состояние /scenes-stock (Finder Agent)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scenes_stock_board import board_for_page, merge_board_preserve_search, normalize_board, sync_board_from_result
from scenes_stock_modes import DEFAULT_SOURCE, PH_SCENE, PH_SOURCE, normalize_source, source_rules_text

BASE_DIR = Path(__file__).resolve().parent
SCENES_STOCK_DATA_DIR = BASE_DIR / "data" / "scenes_stock"
PREFS_PATH = SCENES_STOCK_DATA_DIR / "prefs.json"
DEFAULTS_DIR = BASE_DIR / "scenes_stock" / "defaults"


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
        "source": DEFAULT_SOURCE,
        "system_prompt": _read_default("system_prompt.txt"),
        "user_prompt": _read_default("user_prompt.txt"),
        "scene": "",
        "result": "",
        "board": [],
        "pexels_api_key": "",
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
    base["source"] = normalize_source(base.get("source"))
    base["board"] = normalize_board(base.get("board"))
    base["pexels_api_key"] = str(base.get("pexels_api_key") or "").strip()
    return base


def save_prefs(data: dict[str, Any]) -> dict[str, Any]:
    prev = load_prefs()
    payload = dict(prev)
    for key in ("model", "source", "system_prompt", "user_prompt", "scene", "result", "board", "pexels_api_key"):
        if key not in data:
            continue
        val = data[key]
        if key == "source":
            payload[key] = normalize_source(val)
        elif key == "board":
            payload[key] = merge_board_preserve_search(val, prev.get("board"))
        elif key == "pexels_api_key":
            new_val = str(val or "").strip()
            if not new_val and str(prev.get("pexels_api_key") or "").strip():
                continue
            payload[key] = new_val
        elif key in ("scene", "result"):
            payload[key] = str(val or "")
        else:
            payload[key] = str(val or "").strip()

    if "result" in data and "board" not in data:
        payload["board"] = sync_board_from_result(
            result_raw=str(payload.get("result") or ""),
            existing_board=payload.get("board"),
        )
    payload["saved_at"] = _now_iso()
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def prefs_for_page() -> dict[str, Any]:
    prefs = load_prefs()
    board = board_for_page(prefs)
    if board != normalize_board(prefs.get("board")):
        prefs = save_prefs({**prefs, "board": board})
    else:
        prefs = dict(prefs)
        prefs["board"] = board
    return prefs


def apply_prompt_macros(text: str, prefs: dict[str, Any]) -> str:
    s = str(text or "")
    s = s.replace(PH_SOURCE, source_rules_text(prefs.get("source")))
    s = s.replace(PH_SCENE, str(prefs.get("scene") or "").strip())
    return s
