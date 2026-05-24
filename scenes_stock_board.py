"""Сборка и синхронизация board-сцен Finder → UI поиска stock."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from scenes_stock_check import parse_scene_jsonl, scene_hero_text


def normalize_queries(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            q = str(item or "").strip()
            if q and q not in out:
                out.append(q)
        return out
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        out = []
        for p in parts:
            if p and p not in out:
                out.append(p)
        return out
    return []


def empty_search_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "error": "",
        "items": [],
        "searched_at": "",
        "source": "",
    }


def board_scene_from_result(row: dict[str, Any]) -> dict[str, Any]:
    sid = str(row.get("scene_id") or "").strip()
    hero = scene_hero_text(row)
    text = str(row.get("text") or hero).strip()
    intent = str(row.get("visual_intent") or "").strip()
    queries = normalize_queries(row.get("queries"))
    return {
        "scene_id": sid,
        "hero_text": hero,
        "text": text,
        "visual_intent": intent,
        "queries": queries,
        "search": empty_search_state(),
    }


def _board_index(board: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in board:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("scene_id") or "").strip()
        if sid:
            out[sid] = row
    return out


def sync_board_from_result(*, result_raw: str, existing_board: Any) -> list[dict[str, Any]]:
    """Построить board из Result, сохранив правки queries/intent/search по scene_id."""
    result_scenes, _ = parse_scene_jsonl(result_raw)
    prev = existing_board if isinstance(existing_board, list) else []
    prev_by_id = _board_index([x for x in prev if isinstance(x, dict)])

    out: list[dict[str, Any]] = []
    for row in result_scenes:
        if not isinstance(row, dict):
            continue
        base = board_scene_from_result(row)
        sid = base["scene_id"]
        old = prev_by_id.get(sid)
        if not old:
            out.append(base)
            continue

        merged = deepcopy(base)
        old_intent = str(old.get("visual_intent") or "").strip()
        old_queries = normalize_queries(old.get("queries"))
        old_hero = scene_hero_text(old)
        if old_hero and old_hero == merged["hero_text"]:
            if old_intent:
                merged["visual_intent"] = old_intent
            if old_queries:
                merged["queries"] = old_queries

        old_search = old.get("search") if isinstance(old.get("search"), dict) else {}
        merged["search"] = {
            "status": str(old_search.get("status") or "idle"),
            "error": str(old_search.get("error") or ""),
            "items": old_search.get("items") if isinstance(old_search.get("items"), list) else [],
            "searched_at": str(old_search.get("searched_at") or ""),
            "source": str(old_search.get("source") or ""),
        }
        out.append(merged)
    return out


def normalize_board(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("scene_id") or "").strip()
        if not sid:
            continue
        hero = scene_hero_text(row)
        search = row.get("search") if isinstance(row.get("search"), dict) else {}
        out.append({
            "scene_id": sid,
            "hero_text": hero,
            "text": str(row.get("text") or hero).strip(),
            "visual_intent": str(row.get("visual_intent") or "").strip(),
            "queries": normalize_queries(row.get("queries")),
            "search": {
                "status": str(search.get("status") or "idle"),
                "error": str(search.get("error") or ""),
                "items": search.get("items") if isinstance(search.get("items"), list) else [],
                "searched_at": str(search.get("searched_at") or ""),
                "source": str(search.get("source") or ""),
            },
        })
    return out


def merge_board_preserve_search(incoming: Any, prev_board: Any) -> list[dict[str, Any]]:
    """Не затирать сохранённые результаты поиска при частичном обновлении board."""
    merged = normalize_board(incoming)
    prev_by_id = _board_index(normalize_board(prev_board))
    for row in merged:
        sid = str(row.get("scene_id") or "").strip()
        if not sid:
            continue
        old = prev_by_id.get(sid)
        if not old:
            continue
        old_search = old.get("search") if isinstance(old.get("search"), dict) else {}
        new_search = row.get("search") if isinstance(row.get("search"), dict) else {}
        old_items = old_search.get("items") if isinstance(old_search.get("items"), list) else []
        new_items = new_search.get("items") if isinstance(new_search.get("items"), list) else []
        if old_items and not new_items:
            row["search"] = {
                "status": str(old_search.get("status") or "done"),
                "error": str(new_search.get("error") or old_search.get("error") or ""),
                "items": old_items,
                "searched_at": str(old_search.get("searched_at") or ""),
                "source": str(old_search.get("source") or ""),
            }
    return merged


def board_for_page(prefs: dict[str, Any]) -> list[dict[str, Any]]:
    result_raw = str(prefs.get("result") or "")
    existing = normalize_board(prefs.get("board"))
    if not result_raw.strip():
        return []
    synced = sync_board_from_result(result_raw=result_raw, existing_board=existing)
    return synced


def find_board_scene(board: list[dict[str, Any]], scene_id: str) -> dict[str, Any] | None:
    sid = str(scene_id or "").strip()
    if not sid:
        return None
    for row in board:
        if isinstance(row, dict) and str(row.get("scene_id") or "").strip() == sid:
            return row
    return None
