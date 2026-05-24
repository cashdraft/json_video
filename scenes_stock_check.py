"""Проверка Finder Agent: Stock_Video в исходнике vs Result."""

from __future__ import annotations

import json
import re
from typing import Any

STOCK_VIDEO_SOURCE = "Stock_Video"


def _strip_markdown_fence(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def normalize_visual_source(value: Any) -> str:
    return re.sub(r"[\s_]+", "", str(value or "").strip().lower())


def is_stock_video_source(value: Any) -> bool:
    return normalize_visual_source(value) == normalize_visual_source(STOCK_VIDEO_SOURCE)


def normalize_hero_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def scene_hero_text(scene: dict[str, Any]) -> str:
    hero = str(scene.get("hero_text") or "").strip()
    if hero:
        return hero
    return str(scene.get("text") or "").strip()


def parse_scene_jsonl(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """JSONL или JSON-массив сцен."""
    text = _strip_markdown_fence(raw)
    if not text:
        return [], []

    if text.startswith("["):
        try:
            arr = json.loads(text)
        except json.JSONDecodeError as exc:
            return [], [f"Невалидный JSON-массив: {exc.msg}"]
        if not isinstance(arr, list):
            return [], ["Ожидается JSON-массив сцен"]
        rows = [x for x in arr if isinstance(x, dict)]
        return rows, []

    scenes: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        chunk = line.strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError as exc:
            errors.append(f"Строка {line_no}: невалидный JSON — {exc.msg}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"Строка {line_no}: ожидается JSON-объект")
            continue
        scenes.append(obj)
    return scenes, errors


def validate_finder_stock_video_match(*, scene_raw: str, result_raw: str) -> dict[str, Any]:
    """Сравнить Stock_Video сцены из исходника с Result по scene_id и hero_text."""
    input_scenes, input_errors = parse_scene_jsonl(scene_raw)
    result_scenes, result_errors = parse_scene_jsonl(result_raw)

    expected = [s for s in input_scenes if is_stock_video_source(s.get("visual_source"))]
    result_by_id: dict[str, dict[str, Any]] = {}
    result_by_hero: dict[str, list[dict[str, Any]]] = {}
    for row in result_scenes:
        sid = str(row.get("scene_id") or "").strip()
        if sid:
            result_by_id[sid] = row
        hero_key = normalize_hero_text(scene_hero_text(row))
        if hero_key:
            result_by_hero.setdefault(hero_key, []).append(row)

    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    used_result_ids: set[str] = set()

    for exp in expected:
        sid = str(exp.get("scene_id") or "").strip()
        exp_hero = scene_hero_text(exp)
        exp_hero_norm = normalize_hero_text(exp_hero)
        got = result_by_id.get(sid) if sid else None
        match_kind = "scene_id"

        if got is None and exp_hero_norm:
            candidates = result_by_hero.get(exp_hero_norm) or []
            for cand in candidates:
                cid = str(cand.get("scene_id") or "").strip()
                if cid and cid in used_result_ids:
                    continue
                got = cand
                match_kind = "hero_text"
                break

        if got is None:
            missing.append({
                "scene_id": sid,
                "hero_text": exp_hero,
            })
            continue

        got_id = str(got.get("scene_id") or "").strip()
        if got_id:
            used_result_ids.add(got_id)
        got_hero = scene_hero_text(got)
        hero_ok = normalize_hero_text(got_hero) == exp_hero_norm
        sid_ok = (not sid) or (not got_id) or sid == got_id

        row = {
            "scene_id": sid or got_id,
            "hero_text": exp_hero,
            "result_hero_text": got_hero,
            "match_kind": match_kind,
            "hero_ok": hero_ok,
            "scene_id_ok": sid_ok,
            "ok": hero_ok and sid_ok,
        }
        matched.append(row)
        if not row["ok"]:
            mismatches.append(row)

    extra: list[dict[str, Any]] = []
    expected_ids = {str(s.get("scene_id") or "").strip() for s in expected if str(s.get("scene_id") or "").strip()}
    expected_heroes = {normalize_hero_text(scene_hero_text(s)) for s in expected if normalize_hero_text(scene_hero_text(s))}

    for row in result_scenes:
        sid = str(row.get("scene_id") or "").strip()
        hero_norm = normalize_hero_text(scene_hero_text(row))
        if sid and sid in expected_ids:
            continue
        if hero_norm and hero_norm in expected_heroes:
            continue
        extra.append({
            "scene_id": sid,
            "hero_text": scene_hero_text(row),
        })

    input_stock = len(expected)
    result_count = len(result_scenes)
    matched_ok = sum(1 for r in matched if r.get("ok"))
    parse_error = "; ".join(input_errors + result_errors)

    count_ok = input_stock == result_count
    coverage_ok = matched_ok == input_stock and not missing
    hero_ok_all = not mismatches
    all_ok = coverage_ok and count_ok and hero_ok_all and not extra and not parse_error

    return {
        "summary": {
            "ok": all_ok,
            "input_stock_video_count": input_stock,
            "result_count": result_count,
            "matched_ok": matched_ok,
            "missing_count": len(missing),
            "extra_count": len(extra),
            "mismatch_count": len(mismatches),
            "count_ok": count_ok,
            "coverage_ok": coverage_ok,
            "hero_text_ok": hero_ok_all,
            "parse_error": parse_error,
        },
        "missing": missing,
        "extra": extra,
        "mismatches": mismatches,
        "matched": matched,
    }
