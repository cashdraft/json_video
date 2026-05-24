"""Проверка и сборка Result AS для MacroMap Agent (/scenes-map)."""

from __future__ import annotations

import json
import re
from typing import Any

ALLOWED_MACRO_BLOCK_TYPES = frozenset({
    "hook",
    "hook_expansion",
    "problem_setup",
    "context",
    "concept_explanation",
    "proof",
    "example",
    "escalation",
    "turning_point",
    "solution",
    "warning",
    "recap",
    "final_punch",
    "bridge",
})

ALLOWED_IMPORTANCE = frozenset({"high", "medium", "low"})

REQUIRED_BLOCK_FIELDS = (
    "block_id",
    "macro_block_type",
    "title",
    "start_text",
    "end_text",
    "goal",
    "summary",
    "importance",
)

REQUIRED_GLOBAL_FIELDS = (
    "video_core_problem",
    "main_promise",
    "main_turning_point",
    "final_takeaway",
)

RECOMMENDED_BLOCK_MIN = 6
RECOMMENDED_BLOCK_MAX = 14

ANCHOR_STITCHING_RULES = """
Правила стыковки якорей start_text / end_text:
- end_text текущего блока и start_text следующего блока должны идти подряд в Inbox, без пропущенного текста между ними.
- Между end_text одного блока и start_text следующего не должно оставаться символов сценария (кроме пробелов/переносов на стыке абзацев).
- Якоря должны стыковаться вплотную: если после end_text есть ещё текст до start_text следующего блока — разметка неверна.
- start_text следующего блока должен начинаться сразу после end_text предыдущего (допустимы только пробелы/\\n на стыке).
- Не оставляй «дыр» в покрытии: каждый символ Inbox должен попасть ровно в один macro block.
""".strip()


def _strip_markdown_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(raw: str) -> str:
    text = _strip_markdown_json_fence(raw)
    if not text:
        return ""
    first = text.find("{")
    last = text.rindex("}") if "}" in text else -1
    if first != -1 and last != -1 and last > first:
        return text[first : last + 1].strip()
    return text


def parse_macromap_payload(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    candidate = _extract_json_object(raw)
    if not candidate:
        return None, "Пустой результат"
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"Невалидный JSON: {exc.msg}"
    if not isinstance(obj, dict):
        return None, "Ожидается JSON-объект"
    return obj, None


def _find_anchor(text: str, anchor: str, start_at: int = 0) -> int | None:
    needle = (anchor or "").strip()
    if not needle or not text:
        return None
    idx = text.find(needle, max(0, start_at))
    return idx if idx >= 0 else None


def _macro_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    macro_map = payload.get("macro_map")
    if not isinstance(macro_map, list):
        return []
    return [b for b in macro_map if isinstance(b, dict)]


def extract_block_spans(*, inbox: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Найти start/end в Inbox и вернуть span + text для каждого блока."""
    inbox_text = str(inbox or "")
    rows: list[dict[str, Any]] = []
    search_from = 0

    for i, block in enumerate(blocks, start=1):
        start_anchor = str(block.get("start_text") or "").strip()
        end_anchor = str(block.get("end_text") or "").strip()
        start_idx = _find_anchor(inbox_text, start_anchor, search_from)
        end_idx = None
        end_exclusive = None
        text_slice = ""
        boundary_ok = False

        if start_idx is not None and end_anchor:
            end_idx = _find_anchor(inbox_text, end_anchor, start_idx)
            if end_idx is not None and end_idx >= start_idx:
                end_exclusive = end_idx + len(end_anchor)
                text_slice = inbox_text[start_idx:end_exclusive]
                boundary_ok = True
                search_from = end_exclusive

        rows.append(
            {
                "index": i,
                "block_id": str(block.get("block_id") or ""),
                "macro_block_type": str(block.get("macro_block_type") or ""),
                "title": str(block.get("title") or ""),
                "start_idx": start_idx,
                "end_exclusive": end_exclusive,
                "text": text_slice,
                "chars": len(text_slice),
                "boundary_ok": boundary_ok,
                "start_anchor": start_anchor,
                "end_anchor": end_anchor,
            }
        )
    return rows


def _quote_snippet(text: str, max_len: int = 140) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def analyze_assembly_gaps(inbox_text: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Разделить расхождения склейки: пробелы на стыках vs пропущенные фрагменты."""
    inbox = str(inbox_text or "")
    valid = [
        s
        for s in spans
        if s.get("boundary_ok") and s.get("start_idx") is not None and s.get("end_exclusive") is not None
    ]
    valid.sort(key=lambda s: int(s["start_idx"]))

    boundary_whitespace: list[dict[str, Any]] = []
    missing_fragments: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    boundary_whitespace_chars = 0
    missing_fragment_chars = 0

    def _append_gap(
        gap_start: int,
        gap_end: int,
        *,
        after_block_id: str | None,
        before_block_id: str | None,
        position: str,
    ) -> None:
        nonlocal boundary_whitespace_chars, missing_fragment_chars
        if gap_end <= gap_start:
            return
        gap_text = inbox[gap_start:gap_end]
        char_count = len(gap_text)
        entry: dict[str, Any] = {
            "chars": char_count,
            "quote": _quote_snippet(gap_text),
            "after_block_id": after_block_id or "",
            "before_block_id": before_block_id or "",
            "position": position,
            "start_idx": gap_start,
            "end_idx": gap_end,
        }
        if gap_text.strip() == "":
            boundary_whitespace_chars += char_count
            boundary_whitespace.append(entry)
        else:
            missing_fragment_chars += char_count
            missing_fragments.append(entry)

    if not valid:
        if inbox:
            _append_gap(0, len(inbox), after_block_id=None, before_block_id=None, position="all")
        return {
            "boundary_whitespace_chars": boundary_whitespace_chars,
            "missing_fragment_chars": missing_fragment_chars,
            "boundary_whitespace": boundary_whitespace,
            "missing_fragments": missing_fragments,
            "overlaps": overlaps,
        }

    if int(valid[0]["start_idx"]) > 0:
        _append_gap(
            0,
            int(valid[0]["start_idx"]),
            after_block_id=None,
            before_block_id=str(valid[0].get("block_id") or ""),
            position="before_first",
        )

    for i in range(len(valid) - 1):
        prev = valid[i]
        nxt = valid[i + 1]
        prev_end = int(prev["end_exclusive"])
        next_start = int(nxt["start_idx"])
        if next_start < prev_end:
            overlaps.append(
                {
                    "after_block_id": str(prev.get("block_id") or ""),
                    "before_block_id": str(nxt.get("block_id") or ""),
                    "overlap_chars": prev_end - next_start,
                    "quote": _quote_snippet(inbox[next_start:prev_end]),
                }
            )
            continue
        if next_start > prev_end:
            _append_gap(
                prev_end,
                next_start,
                after_block_id=str(prev.get("block_id") or ""),
                before_block_id=str(nxt.get("block_id") or ""),
                position="between_blocks",
            )

    last_end = int(valid[-1]["end_exclusive"])
    if last_end < len(inbox):
        _append_gap(
            last_end,
            len(inbox),
            after_block_id=str(valid[-1].get("block_id") or ""),
            before_block_id=None,
            position="after_last",
        )

    return {
        "boundary_whitespace_chars": boundary_whitespace_chars,
        "missing_fragment_chars": missing_fragment_chars,
        "boundary_whitespace": boundary_whitespace,
        "missing_fragments": missing_fragments,
        "overlaps": overlaps,
    }


def build_result_as(*, result: str, inbox: str) -> tuple[str, dict[str, Any]]:
    """Собрать Result AS: в каждом блоке вместо start/end — текст из Inbox."""
    payload, parse_error = parse_macromap_payload(result)
    if payload is None:
        return "", {
            "ok": False,
            "parse_error": parse_error or "Ошибка разбора",
            "blocks_info": [],
        }

    blocks = _macro_blocks(payload)
    spans = extract_block_spans(inbox=inbox, blocks=blocks)
    out_blocks: list[dict[str, Any]] = []

    for block, span in zip(blocks, spans):
        out_block = {k: v for k, v in block.items() if k not in ("start_text", "end_text")}
        out_block["text"] = span["text"]
        out_blocks.append(out_block)

    result_as_obj = {
        "macro_map": out_blocks,
        "global_structure_summary": payload.get("global_structure_summary") or {},
    }
    return json.dumps(result_as_obj, ensure_ascii=False, indent=2), {
        "ok": True,
        "parse_error": "",
        "blocks_info": spans,
    }


def _block_row_ok(block: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for field in REQUIRED_BLOCK_FIELDS:
        val = block.get(field)
        if val is None or not str(val).strip():
            issues.append(f"нет {field}")
    mtype = str(block.get("macro_block_type") or "").strip()
    if mtype and mtype not in ALLOWED_MACRO_BLOCK_TYPES:
        issues.append(f"тип {mtype!r}")
    imp = str(block.get("importance") or "").strip().lower()
    if imp and imp not in ALLOWED_IMPORTANCE:
        issues.append(f"importance {imp!r}")
    return (len(issues) == 0, issues)


def validate_macromap(*, result: str, inbox: str) -> dict[str, Any]:
    """Проверка JSON Result: схема, границы в Inbox, global summary."""
    inbox_text = str(inbox or "")
    input_chars = len(inbox_text)

    empty_summary = {
        "ok": False,
        "json_ok": False,
        "blocks": 0,
        "boundaries_ok": 0,
        "schema_ok": 0,
        "global_ok": False,
        "input_chars": input_chars,
        "recommended_blocks_ok": False,
        "parse_error": "",
    }

    payload, parse_error = parse_macromap_payload(result)
    if payload is None:
        return {
            "summary": {**empty_summary, "parse_error": parse_error or "Ошибка разбора"},
            "blocks_info": [],
        }

    blocks = _macro_blocks(payload)
    global_summary = payload.get("global_structure_summary")
    global_ok = isinstance(global_summary, dict) and all(
        str(global_summary.get(field) or "").strip() for field in REQUIRED_GLOBAL_FIELDS
    )

    spans = extract_block_spans(inbox=inbox_text, blocks=blocks)
    blocks_info: list[dict[str, Any]] = []
    boundaries_ok = 0
    schema_ok = 0

    for block, span in zip(blocks, spans):
        schema_block_ok, schema_issues = _block_row_ok(block)
        if schema_block_ok:
            schema_ok += 1
        if span["boundary_ok"]:
            boundaries_ok += 1
        blocks_info.append(
            {
                "index": span["index"],
                "block_id": span["block_id"],
                "macro_block_type": span["macro_block_type"],
                "title": span["title"],
                "schema_ok": schema_block_ok,
                "boundary_ok": span["boundary_ok"],
                "ok": schema_block_ok and span["boundary_ok"],
                "issues": schema_issues,
            }
        )

    block_count = len(blocks)
    recommended_ok = RECOMMENDED_BLOCK_MIN <= block_count <= RECOMMENDED_BLOCK_MAX
    all_ok = (
        block_count > 0
        and schema_ok == block_count
        and boundaries_ok == block_count
        and global_ok
    )

    return {
        "summary": {
            "ok": all_ok,
            "json_ok": True,
            "blocks": block_count,
            "boundaries_ok": boundaries_ok,
            "schema_ok": schema_ok,
            "global_ok": global_ok,
            "input_chars": input_chars,
            "recommended_blocks_ok": recommended_ok,
            "parse_error": "",
        },
        "blocks_info": blocks_info,
    }


def validate_result_as_assembly(*, result: str, inbox: str, result_as: str = "") -> dict[str, Any]:
    """Проверка Result AS: склейка = Inbox, порядок, без пересечений."""
    inbox_text = str(inbox or "")
    input_chars = len(inbox_text)

    empty = {
        "ok": False,
        "input_chars": input_chars,
        "output_chars": 0,
        "delta_chars": -input_chars,
        "blocks": 0,
        "order_ok": False,
        "overlap_ok": False,
        "join_ok": False,
        "parse_error": "",
    }

    payload, parse_error = parse_macromap_payload(result)
    if payload is None:
        return {"summary": {**empty, "parse_error": parse_error or "Ошибка разбора Result"}, "blocks_info": []}

    blocks = _macro_blocks(payload)
    spans = extract_block_spans(inbox=inbox_text, blocks=blocks)

    if not spans:
        return {"summary": {**empty, "parse_error": "Нет блоков"}, "blocks_info": []}

    joined = "".join(str(s.get("text") or "") for s in spans)
    output_chars = len(joined)
    join_ok = joined == inbox_text

    order_ok = True
    overlap_ok = True
    prev_end: int | None = None

    for span in spans:
        start_idx = span.get("start_idx")
        end_exclusive = span.get("end_exclusive")
        if not span.get("boundary_ok") or start_idx is None or end_exclusive is None:
            order_ok = False
            overlap_ok = False
            continue
        if prev_end is not None:
            if start_idx < prev_end:
                overlap_ok = False
            if start_idx < prev_end:
                order_ok = False
        prev_end = end_exclusive

    blocks_info = [
        {
            "index": s["index"],
            "block_id": s["block_id"],
            "macro_block_type": s["macro_block_type"],
            "chars": s["chars"],
            "start_idx": s.get("start_idx"),
            "end_exclusive": s.get("end_exclusive"),
            "boundary_ok": s["boundary_ok"],
            "ok": s["boundary_ok"],
        }
        for s in spans
    ]

    boundaries_ok = sum(1 for s in spans if s["boundary_ok"])
    gaps = analyze_assembly_gaps(inbox_text, spans)
    all_ok = (
        boundaries_ok == len(spans)
        and join_ok
        and order_ok
        and overlap_ok
        and len(spans) > 0
        and gaps["missing_fragment_chars"] == 0
        and not gaps["overlaps"]
    )

    return {
        "summary": {
            "ok": all_ok,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "delta_chars": output_chars - input_chars,
            "blocks": len(spans),
            "boundaries_ok": boundaries_ok,
            "order_ok": order_ok,
            "overlap_ok": overlap_ok,
            "join_ok": join_ok,
            "boundary_whitespace_chars": gaps["boundary_whitespace_chars"],
            "missing_fragment_chars": gaps["missing_fragment_chars"],
            "parse_error": "",
        },
        "blocks_info": blocks_info,
        "gaps": gaps,
    }


SPEECH_CHARS_PER_SEC = 14.0


def compute_block_scene_stats(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Статистика по сценам блока: min/max/avg длина, разбивка visual_source."""
    char_lens: list[int] = []
    visual_counts: dict[str, int] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        text = str(scene.get("text") or scene.get("hero_text") or "")
        if text.strip():
            char_lens.append(len(text))
        vkey = str(scene.get("visual_source") or "").strip() or "—"
        visual_counts[vkey] = visual_counts.get(vkey, 0) + 1

    total = len(scenes)
    avg_chars = round(sum(char_lens) / len(char_lens), 1) if char_lens else 0.0
    avg_duration_sec = round(avg_chars / SPEECH_CHARS_PER_SEC, 1) if avg_chars else 0.0
    return {
        "scene_count": total,
        "min_chars": min(char_lens) if char_lens else 0,
        "max_chars": max(char_lens) if char_lens else 0,
        "avg_chars": avg_chars,
        "total_chars": sum(char_lens),
        "avg_duration_sec": avg_duration_sec,
        "visual_source_counts": dict(sorted(visual_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def match_scenes_in_source(source: str, scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Найти тексты сцен в исходном block.text по порядку."""
    src = str(source or "")
    pos = 0
    rows: list[dict[str, Any]] = []

    for i, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        raw = str(scene.get("text") or scene.get("hero_text") or "")
        needle = raw.strip()
        sid = str(scene.get("scene_id") or f"scene_{i}")
        if not needle:
            rows.append({
                "scene_id": sid,
                "index": i,
                "start_idx": None,
                "end_exclusive": None,
                "chars": 0,
                "match_ok": False,
                "error": "пустой text",
            })
            continue
        idx = src.find(needle, pos)
        if idx < 0:
            rows.append({
                "scene_id": sid,
                "index": i,
                "start_idx": None,
                "end_exclusive": None,
                "chars": len(needle),
                "match_ok": False,
                "error": "не найден в source",
            })
            continue
        end = idx + len(needle)
        rows.append({
            "scene_id": sid,
            "index": i,
            "start_idx": idx,
            "end_exclusive": end,
            "chars": len(needle),
            "match_ok": True,
            "error": "",
        })
        pos = end
    return rows


def analyze_scene_stitch_gaps(source: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Пробелы/пропуски между сценами внутри block.text."""
    src = str(source or "")
    valid = [s for s in spans if s.get("match_ok") and s.get("start_idx") is not None and s.get("end_exclusive") is not None]
    valid.sort(key=lambda s: int(s["start_idx"]))

    boundary_whitespace: list[dict[str, Any]] = []
    missing_fragments: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    boundary_whitespace_chars = 0
    missing_fragment_chars = 0

    def _append_gap(gap_start: int, gap_end: int, *, after_scene_id: str, before_scene_id: str, position: str) -> None:
        nonlocal boundary_whitespace_chars, missing_fragment_chars
        if gap_end <= gap_start:
            return
        gap_text = src[gap_start:gap_end]
        char_count = len(gap_text)
        entry: dict[str, Any] = {
            "chars": char_count,
            "quote": _quote_snippet(gap_text),
            "after_scene_id": after_scene_id or "",
            "before_scene_id": before_scene_id or "",
            "position": position,
        }
        if gap_text.strip() == "":
            boundary_whitespace_chars += char_count
            boundary_whitespace.append(entry)
        else:
            missing_fragment_chars += char_count
            missing_fragments.append(entry)

    if not valid:
        if src.strip():
            _append_gap(0, len(src), after_scene_id="", before_scene_id="", position="all")
        return {
            "boundary_whitespace_chars": boundary_whitespace_chars,
            "missing_fragment_chars": missing_fragment_chars,
            "boundary_whitespace": boundary_whitespace,
            "missing_fragments": missing_fragments,
            "overlaps": overlaps,
        }

    if int(valid[0]["start_idx"]) > 0:
        _append_gap(
            0,
            int(valid[0]["start_idx"]),
            after_scene_id="",
            before_scene_id=str(valid[0].get("scene_id") or ""),
            position="before_first",
        )

    pos_after = int(valid[0]["end_exclusive"])
    for i in range(len(valid) - 1):
        prev = valid[i]
        nxt = valid[i + 1]
        prev_end = int(prev["end_exclusive"])
        next_start = int(nxt["start_idx"])
        if next_start < prev_end:
            overlaps.append({
                "after_scene_id": str(prev.get("scene_id") or ""),
                "before_scene_id": str(nxt.get("scene_id") or ""),
                "overlap_chars": prev_end - next_start,
                "quote": _quote_snippet(src[next_start:prev_end]),
            })
        elif next_start > prev_end:
            _append_gap(
                prev_end,
                next_start,
                after_scene_id=str(prev.get("scene_id") or ""),
                before_scene_id=str(nxt.get("scene_id") or ""),
                position="between_scenes",
            )
        pos_after = int(nxt["end_exclusive"])

    if pos_after < len(src):
        _append_gap(
            pos_after,
            len(src),
            after_scene_id=str(valid[-1].get("scene_id") or ""),
            before_scene_id="",
            position="after_last",
        )

    return {
        "boundary_whitespace_chars": boundary_whitespace_chars,
        "missing_fragment_chars": missing_fragment_chars,
        "boundary_whitespace": boundary_whitespace,
        "missing_fragments": missing_fragments,
        "overlaps": overlaps,
    }


def validate_block_scene_coverage(*, source_text: str, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Покрывает ли склейка сцен весь block.text, отданный модели."""
    src = str(source_text or "")
    spans = match_scenes_in_source(src, scenes)
    gaps = analyze_scene_stitch_gaps(src, spans)
    matched = [s for s in spans if s.get("match_ok")]
    stitched_chars = sum(int(s.get("chars") or 0) for s in matched)
    all_matched = len(matched) == len([s for s in scenes if isinstance(s, dict)]) and len(scenes) > 0
    coverage_ok = (
        all_matched
        and gaps["missing_fragment_chars"] == 0
        and not gaps["overlaps"]
        and (not src.strip() or len(matched) > 0)
    )

    return {
        "ok": coverage_ok,
        "source_chars": len(src),
        "stitched_chars": stitched_chars,
        "delta_chars": stitched_chars - len(src),
        "scenes_matched": len(matched),
        "scenes_total": len(scenes),
        "boundary_whitespace_chars": gaps["boundary_whitespace_chars"],
        "missing_fragment_chars": gaps["missing_fragment_chars"],
        "gaps": gaps,
        "scenes_info": spans,
    }


def summarize_scenemap_progress(
    *,
    blocks: list[dict[str, Any]],
    block_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Сводка по всем блокам: total scenes, avg duration, full text coverage."""
    total_scenes = 0
    total_scene_chars = 0
    block_infos: list[dict[str, Any]] = []
    source_chars = 0
    missing_total = 0
    ws_total = 0
    all_ok = True

    by_index = {int(r.get("block_index", -1)): r for r in block_rows if isinstance(r, dict)}

    for bi, block in enumerate(blocks):
        row = by_index.get(bi) or {}
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        coverage = row.get("coverage") if isinstance(row.get("coverage"), dict) else {}
        sc = int(stats.get("scene_count") or row.get("scene_count") or 0)
        total_scenes += sc
        total_scene_chars += int(stats.get("total_chars") or 0)
        src_len = int(coverage.get("source_chars") or len(str(block.get("text") or "")))
        source_chars += src_len
        miss = int(coverage.get("missing_fragment_chars") or 0)
        ws = int(coverage.get("boundary_whitespace_chars") or 0)
        missing_total += miss
        ws_total += ws
        block_ok = bool(coverage.get("ok")) if coverage else False
        if sc > 0 and not block_ok:
            all_ok = False
        block_infos.append({
            "block_index": bi,
            "block_id": str(block.get("block_id") or row.get("block_id") or ""),
            "macro_block_type": str(block.get("macro_block_type") or row.get("macro_block_type") or ""),
            "stats": stats,
            "coverage": coverage,
            "ok": block_ok if sc > 0 else None,
        })

    avg_chars = round(total_scene_chars / total_scenes, 1) if total_scenes else 0.0

    return {
        "total_scenes": total_scenes,
        "avg_chars": avg_chars,
        "avg_duration_sec": round(avg_chars / SPEECH_CHARS_PER_SEC, 1) if avg_chars else 0.0,
        "source_chars": source_chars,
        "missing_fragment_chars": missing_total,
        "boundary_whitespace_chars": ws_total,
        "coverage_ok": all_ok and total_scenes > 0,
        "blocks": block_infos,
    }
