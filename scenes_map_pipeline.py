"""SceneMap Agent — по-блочная обработка macro_map → JSONL сцен."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scenes_map_check import parse_macromap_payload

BASE_DIR = Path(__file__).resolve().parent
SCENE_MAP_DIR = BASE_DIR / "data" / "scenes_map" / "scene_map"
RUN_STATE_PATH = SCENE_MAP_DIR / "run_state.json"
FINAL_SCENE_MAP_PATH = SCENE_MAP_DIR / "final_scene_map.jsonl"

PH_MACRO_MAP = "{{MACRO_MAP}}"
PH_CURRENT_BLOCK = "{{CURRENT_BLOCK}}"
PH_PREVIOUS_SCENE_TAIL = "{{PREVIOUS_SCENE_TAIL}}"

PREVIOUS_SCENE_TAIL_MAX = 5
PREVIOUS_SCENE_TAIL_MIN = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_markdown_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:jsonl|json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def scene_map_dir() -> Path:
    SCENE_MAP_DIR.mkdir(parents=True, exist_ok=True)
    return SCENE_MAP_DIR


def block_jsonl_filename(block_id: str, index: int) -> str:
    bid = (block_id or "").strip() or f"block_{index:02d}"
    safe = re.sub(r"[^\w\-]+", "_", bid)
    return f"scene_map_{safe}.jsonl"


def macro_map_strip_text(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        out.append({k: v for k, v in block.items() if k != "text"})
    return out


def load_macromap_from_prefs(prefs: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    """Взять macro_map с полным text из result_as (или собрать из result + inbox)."""
    result_as = str(prefs.get("result_as") or "").strip()
    if not result_as:
        from scenes_map_check import build_result_as

        result = str(prefs.get("result") or "")
        inbox = str(prefs.get("inbox") or "")
        if result.strip() and inbox.strip():
            result_as, meta = build_result_as(result=result, inbox=inbox)
            if not meta.get("ok"):
                return [], {}, "Не удалось собрать Result AS из Result + Inbox."

    payload, err = parse_macromap_payload(result_as)
    if payload is None:
        return [], {}, err or "Result AS: невалидный JSON macro_map."

    macro_map = payload.get("macro_map")
    if not isinstance(macro_map, list) or not macro_map:
        return [], {}, "Result AS: macro_map пуст или отсутствует."

    blocks = [b for b in macro_map if isinstance(b, dict)]
    if not blocks:
        return [], {}, "Result AS: нет валидных блоков в macro_map."

    for i, block in enumerate(blocks, start=1):
        if not str(block.get("text") or "").strip():
            bid = block.get("block_id") or f"block_{i:02d}"
            return [], {}, f"Блок {bid}: отсутствует text (нужен Result AS с полным текстом)."

    global_summary = payload.get("global_structure_summary")
    if not isinstance(global_summary, dict):
        global_summary = {}

    return blocks, global_summary, None


def previous_scene_tail(scenes: list[dict[str, Any]], *, max_tail: int = PREVIOUS_SCENE_TAIL_MAX) -> list[dict[str, Any]]:
    if not scenes:
        return []
    n = max(1, min(max_tail, len(scenes)))
    if len(scenes) >= PREVIOUS_SCENE_TAIL_MIN:
        n = min(max_tail, len(scenes))
    return [dict(s) for s in scenes[-n:]]


def scenes_to_jsonl(scenes: list[dict[str, Any]]) -> str:
    lines = [json.dumps(s, ensure_ascii=False) for s in scenes if isinstance(s, dict)]
    return "\n".join(lines) + ("\n" if lines else "")


def scene_text_value(scene: dict[str, Any]) -> str:
    """Текст сцены: canonical `text`, fallback на `hero_text` из ответа модели."""
    if not isinstance(scene, dict):
        return ""
    text = str(scene.get("text") or "").strip()
    if text:
        return text
    return str(scene.get("hero_text") or "").strip()


def normalize_scene_row(scene: dict[str, Any]) -> dict[str, Any]:
    """Привести строку сцены к canonical виду (text заполнен из hero_text при необходимости)."""
    row = dict(scene)
    text = scene_text_value(row)
    if text:
        row["text"] = text
    return row


def normalize_scene_rows(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_scene_row(s) for s in scenes if isinstance(s, dict)]


def parse_scenemap_jsonl(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Распарсить JSONL: одна строка = одна сцена."""
    text = _strip_markdown_fence(raw)
    scenes: list[dict[str, Any]] = []
    errors: list[str] = []

    if text.startswith("["):
        try:
            arr = json.loads(text)
        except json.JSONDecodeError as exc:
            return [], [f"Невалидный JSON-массив: {exc.msg}"]
        if isinstance(arr, list):
            for i, item in enumerate(arr, start=1):
                if isinstance(item, dict):
                    scenes.append(item)
                else:
                    errors.append(f"Элемент {i}: ожидается объект сцены")
            return scenes, errors
        return [], ["Ожидается JSON-массив сцен"]

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

    if not scenes and not errors:
        errors.append("Пустой ответ SceneMap Agent (нет JSONL строк)")
    return scenes, errors


def assign_scene_ids(
    scenes: list[dict[str, Any]],
    *,
    start_index: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Присвоить/проверить scene_id: scene_001, scene_002, …"""
    notes: list[str] = []
    out: list[dict[str, Any]] = []
    for i, scene in enumerate(scenes):
        expected = f"scene_{start_index + i:03d}"
        row = dict(scene)
        got = str(row.get("scene_id") or "").strip()
        if got and got != expected:
            notes.append(f"scene_id {got!r} → {expected}")
        row["scene_id"] = expected
        out.append(row)
    return out, notes


def validate_scene_rows(scenes: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for i, scene in enumerate(scenes, start=1):
        sid = str(scene.get("scene_id") or "").strip()
        if not sid:
            errors.append(f"Сцена {i}: нет scene_id")
        if not scene_text_value(scene):
            errors.append(f"Сцена {sid or i}: нет text")
    return errors


def apply_scenemap_block_macros(
    text: str,
    prefs: dict[str, Any],
    *,
    macro_map_no_text: list[dict[str, Any]],
    current_block: dict[str, Any],
    previous_tail: list[dict[str, Any]],
    global_summary: dict[str, Any] | None = None,
) -> str:
    from scenes_map_session import apply_prompt_macros

    s = apply_prompt_macros(str(text or ""), prefs, agent="scenemap")
    macro_payload = {
        "macro_map": macro_map_no_text,
        "global_structure_summary": global_summary or {},
    }
    s = s.replace(PH_MACRO_MAP, json.dumps(macro_payload, ensure_ascii=False, indent=2))
    s = s.replace(PH_CURRENT_BLOCK, json.dumps(current_block, ensure_ascii=False, indent=2))
    tail_json = json.dumps(previous_tail, ensure_ascii=False, indent=2) if previous_tail else "[]"
    s = s.replace(PH_PREVIOUS_SCENE_TAIL, tail_json)
    return s


def compose_scenemap_block_message(
    *,
    user_prompt: str,
    macro_map_no_text: list[dict[str, Any]],
    current_block: dict[str, Any],
    previous_tail: list[dict[str, Any]],
    global_summary: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    up = (user_prompt or "").strip()
    if up:
        parts.append(up)
    parts.append("=== MACRO_MAP (без text) ===\n" + json.dumps(
        {"macro_map": macro_map_no_text, "global_structure_summary": global_summary or {}},
        ensure_ascii=False,
        indent=2,
    ))
    parts.append("=== CURRENT_BLOCK (с text) ===\n" + json.dumps(current_block, ensure_ascii=False, indent=2))
    tail_json = json.dumps(previous_tail, ensure_ascii=False, indent=2) if previous_tail else "[]"
    parts.append("=== PREVIOUS_SCENE_TAIL ===\n" + tail_json)
    return "\n\n".join(parts).strip()


def load_run_state() -> dict[str, Any]:
    if not RUN_STATE_PATH.is_file():
        return {}
    try:
        raw = json.loads(RUN_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_run_state(state: dict[str, Any]) -> dict[str, Any]:
    scene_map_dir()
    payload = dict(state)
    payload["updated_at"] = _now_iso()
    RUN_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def reset_run_state(*, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    scene_map_dir()
    for old in SCENE_MAP_DIR.glob("scene_map_*.jsonl"):
        try:
            old.unlink()
        except OSError:
            pass
    if FINAL_SCENE_MAP_PATH.is_file():
        try:
            FINAL_SCENE_MAP_PATH.unlink()
        except OSError:
            pass

    state = {
        "started_at": _now_iso(),
        "blocks_total": len(blocks),
        "blocks_done": 0,
        "scene_count": 0,
        "block_results": [],
        "final_scene_map_path": str(FINAL_SCENE_MAP_PATH.relative_to(BASE_DIR)),
    }
    return save_run_state(state)


def load_final_scenes_from_blocks(state: dict[str, Any]) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for row in state.get("block_results") or []:
        if not isinstance(row, dict):
            continue
        path = SCENE_MAP_DIR / str(row.get("file") or "")
        if not path.is_file():
            continue
        block_scenes, _ = parse_scenemap_jsonl(path.read_text(encoding="utf-8"))
        scenes.extend(block_scenes)
    return scenes


def save_block_jsonl(filename: str, scenes: list[dict[str, Any]]) -> Path:
    scene_map_dir()
    path = SCENE_MAP_DIR / filename
    path.write_text(scenes_to_jsonl(scenes), encoding="utf-8")
    return path


def merge_final_scene_map(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    scenes = load_final_scenes_from_blocks(state)
    content = scenes_to_jsonl(scenes)
    scene_map_dir()
    FINAL_SCENE_MAP_PATH.write_text(content, encoding="utf-8")
    meta = {
        "ok": True,
        "scene_count": len(scenes),
        "path": str(FINAL_SCENE_MAP_PATH.relative_to(BASE_DIR)),
    }
    state = dict(state)
    state["scene_count"] = len(scenes)
    state["finalized_at"] = _now_iso()
    state["final_scene_map_path"] = meta["path"]
    save_run_state(state)
    return content, meta


def run_scenemap_audit_stub(*, final_path: Path | None = None) -> dict[str, Any]:
    """Заглушка audit agent — отдельный этап после final_scene_map.jsonl."""
    path = final_path or FINAL_SCENE_MAP_PATH
    ok = path.is_file() and path.stat().st_size > 0
    return {
        "ok": ok,
        "status": "pending",
        "message": "Audit agent будет запущен отдельно (пока не реализован).",
        "final_path": str(path.relative_to(BASE_DIR)) if path.is_file() else "",
    }


def process_scenemap_block(
    *,
    prefs: dict[str, Any],
    block_index: int,
    model: str,
    user_prompt_template: str,
    run_agent: Callable[..., tuple[str | None, str | None]],
    reset: bool = False,
) -> dict[str, Any]:
    """Обработать один macro block (block_index 0-based)."""
    blocks, global_summary, err = load_macromap_from_prefs(prefs)
    if err:
        return {"ok": False, "error": err}

    if block_index < 0 or block_index >= len(blocks):
        return {"ok": False, "error": f"block_index {block_index} вне диапазона 0..{len(blocks) - 1}"}

    state = load_run_state()
    if reset or not state or state.get("blocks_total") != len(blocks):
        state = reset_run_state(blocks=blocks)

    macro_no_text = macro_map_strip_text(blocks)
    current = dict(blocks[block_index])
    block_id = str(current.get("block_id") or f"block_{block_index + 1:02d}")

    existing_results = [r for r in (state.get("block_results") or []) if isinstance(r, dict)]
    if any(r.get("block_index") == block_index and r.get("ok") for r in existing_results):
        prior = next(r for r in existing_results if r.get("block_index") == block_index)
        return {
            "ok": True,
            "skipped": True,
            "block_index": block_index,
            "block_id": block_id,
            "scene_count": prior.get("scene_count", 0),
            "file": prior.get("file", ""),
            "stats": prior.get("stats") or {},
            "coverage": prior.get("coverage") or {},
            "state": state,
        }

    prior_scenes = load_final_scenes_from_blocks(state)
    tail = previous_scene_tail(prior_scenes)

    prompt = apply_scenemap_block_macros(
        user_prompt_template,
        prefs,
        macro_map_no_text=macro_no_text,
        current_block=current,
        previous_tail=tail,
        global_summary=global_summary,
    )

    from scenes_map_agent import resolve_scenemap_system_prompt

    system_prompt = resolve_scenemap_system_prompt(prefs)

    answer, agent_err = run_agent(
        model=model,
        user_prompt=prompt,
        system_prompt=system_prompt,
        macro_map_no_text=macro_no_text,
        current_block=current,
        previous_tail=tail,
        global_summary=global_summary,
    )
    if agent_err or answer is None:
        return {"ok": False, "error": agent_err or "generation_failed", "block_index": block_index, "block_id": block_id}

    raw_scenes, parse_errors = parse_scenemap_jsonl(answer)
    if parse_errors and not raw_scenes:
        return {
            "ok": False,
            "error": "; ".join(parse_errors),
            "block_index": block_index,
            "block_id": block_id,
            "raw": answer,
        }

    raw_scenes = normalize_scene_rows(raw_scenes)
    start_idx = len(prior_scenes) + 1
    scenes, id_notes = assign_scene_ids(raw_scenes, start_index=start_idx)
    val_errors = validate_scene_rows(scenes)
    all_notes = parse_errors + id_notes + val_errors

    filename = block_jsonl_filename(block_id, block_index + 1)
    save_block_jsonl(filename, scenes)

    from scenes_map_check import compute_block_scene_stats, validate_block_scene_coverage

    block_text = str(current.get("text") or "")
    stats = compute_block_scene_stats(scenes)
    coverage = validate_block_scene_coverage(source_text=block_text, scenes=scenes)

    block_result = {
        "block_index": block_index,
        "block_id": block_id,
        "macro_block_type": str(current.get("macro_block_type") or ""),
        "title": str(current.get("title") or ""),
        "ok": len(val_errors) == 0,
        "scene_count": len(scenes),
        "file": filename,
        "notes": all_notes,
        "stats": stats,
        "coverage": coverage,
        "completed_at": _now_iso(),
    }

    block_results = [r for r in existing_results if r.get("block_index") != block_index]
    block_results.append(block_result)
    block_results.sort(key=lambda r: int(r.get("block_index", 0)))

    state = dict(state)
    state["block_results"] = block_results
    state["blocks_done"] = sum(1 for r in block_results if r.get("ok"))
    state["scene_count"] = sum(int(r.get("scene_count") or 0) for r in block_results if r.get("ok"))
    save_run_state(state)

    if val_errors:
        return {
            "ok": False,
            "error": "; ".join(val_errors),
            "block_index": block_index,
            "block_id": block_id,
            "scenes": scenes,
            "file": filename,
            "notes": all_notes,
            "raw": answer,
            "state": state,
        }

    return {
        "ok": True,
        "block_index": block_index,
        "block_id": block_id,
        "scene_count": len(scenes),
        "file": filename,
        "notes": all_notes,
        "stats": stats,
        "coverage": coverage,
        "state": state,
    }


def build_scenemap_progress_report(prefs: dict[str, Any]) -> dict[str, Any]:
    """Сводка прогресса SceneMap из run_state + сохранённых jsonl."""
    from scenes_map_check import (
        compute_block_scene_stats,
        summarize_scenemap_progress,
        validate_block_scene_coverage,
    )

    blocks, _, err = load_macromap_from_prefs(prefs)
    if err:
        return {"ok": False, "error": err}

    state = load_run_state()
    block_rows: list[dict[str, Any]] = []

    for row in state.get("block_results") or []:
        if not isinstance(row, dict) or not row.get("ok"):
            continue
        bi = int(row.get("block_index", -1))
        enriched = dict(row)
        if bi < 0 or bi >= len(blocks):
            block_rows.append(enriched)
            continue
        if enriched.get("stats") and enriched.get("coverage"):
            block_rows.append(enriched)
            continue
        path = SCENE_MAP_DIR / str(row.get("file") or "")
        if not path.is_file():
            block_rows.append(enriched)
            continue
        scenes, _ = parse_scenemap_jsonl(path.read_text(encoding="utf-8"))
        block_text = str(blocks[bi].get("text") or "")
        enriched["stats"] = compute_block_scene_stats(scenes)
        enriched["coverage"] = validate_block_scene_coverage(source_text=block_text, scenes=scenes)
        block_rows.append(enriched)

    summary = summarize_scenemap_progress(blocks=blocks, block_rows=block_rows)
    return {
        "ok": True,
        "blocks_total": len(blocks),
        "blocks_done": len(block_rows),
        "summary": summary,
        "block_results": block_rows,
        "state": state,
    }


def run_scenemap_pipeline(
    *,
    prefs: dict[str, Any],
    model: str,
    user_prompt_template: str,
    run_agent: Callable[..., tuple[str | None, str | None]],
    reset: bool = True,
) -> dict[str, Any]:
    """Полный цикл по всем macro blocks + merge + audit stub."""
    blocks, _, err = load_macromap_from_prefs(prefs)
    if err:
        return {"ok": False, "error": err}

    if reset:
        reset_run_state(blocks=blocks)

    block_outcomes: list[dict[str, Any]] = []
    for idx in range(len(blocks)):
        outcome = process_scenemap_block(
            prefs=prefs,
            block_index=idx,
            model=model,
            user_prompt_template=user_prompt_template,
            run_agent=run_agent,
            reset=False,
        )
        block_outcomes.append(outcome)
        if not outcome.get("ok"):
            return {
                "ok": False,
                "error": outcome.get("error") or "block_failed",
                "block_index": idx,
                "blocks": block_outcomes,
                "state": outcome.get("state") or load_run_state(),
            }

    state = load_run_state()
    final_content, final_meta = merge_final_scene_map(state)
    audit = run_scenemap_audit_stub()

    return {
        "ok": True,
        "result": final_content,
        "blocks": block_outcomes,
        "final": final_meta,
        "audit": audit,
        "state": load_run_state(),
    }
