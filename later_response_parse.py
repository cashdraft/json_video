"""Разбор и валидация трёхблочного ответа Later… (SVG + JSON анимации + пояснение)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from later_anim_dictionary import allowed_anims_set

MARKER_SVG_START = "===SVG_START==="
MARKER_SVG_END = "===SVG_END==="
MARKER_ANIM_START = "===ANIM_START==="
MARKER_ANIM_END = "===ANIM_END==="
MARKER_NOTES_START = "===NOTES_START==="
MARKER_NOTES_END = "===NOTES_END==="
MARKER_FIXLOG_START = "===FIXLOG_START==="
MARKER_FIXLOG_END = "===FIXLOG_END==="

_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_-]+)?\s*\n?(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# Строка: пробелы + id="..." + атрибуты + > + текст (пропущен <text>)
_BROKEN_TEXT_EL = re.compile(
    r"(?:^|\n)(\s+)(id\s*=\s*[\"'][^\"']+[\"'][^>]*>)([^<\n]+)",
    re.MULTILINE,
)
# Сразу после закрывающего > другого тега: <g> id="t-1" x="1">TEXT
_BROKEN_TEXT_AFTER_GT = re.compile(
    r"(>)(\s+)(id\s*=\s*[\"'][^\"']+[\"'][^>]*>)([^<]+?)(?=\s*</|\s*<[^/]|\s*$)",
    re.MULTILINE | re.DOTALL,
)

_BROKEN_TEXT_LINE = re.compile(
    r"^\s*id\s*=\s*[\"']",
    re.MULTILINE,
)


def _allowed_anims() -> set[str]:
    return allowed_anims_set()


def unwrap_code_fence_block(block: str) -> str:
    """Убрать обёртку ```lang … ``` если модель положила фенс внутрь маркеров."""
    s = (block or "").strip()
    if not s:
        return ""
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?", s)
    if m:
        s = s[m.end() :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    else:
        inner = _FENCE_RE.search(s)
        if inner and inner.start() <= 2:
            s = (inner.group(2) or "").strip()
    return s.strip()


def _extract_svg_document(svg: str) -> str:
    """Оставить только документ <svg>…</svg>."""
    s = (svg or "").strip()
    i = s.find("<svg")
    if i < 0:
        i = s.find("<SVG")
    j = s.rfind("</svg>")
    if j < 0:
        j = s.rfind("</SVG>")
    if i >= 0 and j > i:
        return s[i : j + 6]
    return s


def _repair_text_fragment(
    prefix: str,
    attrs_and_gt: str,
    inner: str,
) -> str | None:
    attrs = (attrs_and_gt or "").strip()
    body = (inner or "").strip()
    if not body or "<" in body or "</text>" in body.lower():
        return None
    if attrs.lower().startswith(("text", "tspan")):
        return None
    return f"{prefix}<text {attrs}{body}</text>"


def repair_svg_text_tags(svg: str) -> tuple[str, int]:
    """Вставить пропущенные <text>…</text> (типичный баг модели). Несколько проходов."""
    total = 0
    out = svg
    for _ in range(8):
        n_pass = 0

        def repl_line(m: re.Match[str]) -> str:
            nonlocal n_pass
            fixed = _repair_text_fragment(m.group(1), m.group(2), m.group(3))
            if fixed is None:
                return m.group(0)
            n_pass += 1
            return fixed

        def repl_after_gt(m: re.Match[str]) -> str:
            nonlocal n_pass
            fixed = _repair_text_fragment(m.group(1) + m.group(2), m.group(3), m.group(4))
            if fixed is None:
                return m.group(0)
            n_pass += 1
            return fixed

        out = _BROKEN_TEXT_EL.sub(repl_line, out)
        out = _BROKEN_TEXT_AFTER_GT.sub(repl_after_gt, out)
        total += n_pass
        if n_pass == 0:
            break
    return out, total


def _normalize_svg_block(raw: str) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"fence_stripped": False, "text_tags_repaired": 0}
    s = unwrap_code_fence_block(raw)
    if s != (raw or "").strip():
        meta["fence_stripped"] = True
    s = _extract_svg_document(s)
    repaired, n = repair_svg_text_tags(s)
    if n:
        meta["text_tags_repaired"] = n
        s = repaired
    return s, meta


def _normalize_json_block(raw: str) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"fence_stripped": False}
    s = unwrap_code_fence_block(raw)
    if s != (raw or "").strip():
        meta["fence_stripped"] = True
    s = s.strip()
    if s.startswith("{") or s.startswith("["):
        return s, meta
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        s = s[i : j + 1]
    return s, meta


def _slice_markers(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i < 0:
        return None
    j = text.find(end, i + len(start))
    if j < 0:
        return None
    return text[i + len(start) : j].strip()


def _parse_by_markers(text: str) -> dict[str, str] | None:
    svg_raw = _slice_markers(text, MARKER_SVG_START, MARKER_SVG_END)
    anim_raw = _slice_markers(text, MARKER_ANIM_START, MARKER_ANIM_END)
    notes = _slice_markers(text, MARKER_NOTES_START, MARKER_NOTES_END)
    fixlog = _slice_markers(text, MARKER_FIXLOG_START, MARKER_FIXLOG_END)
    if svg_raw is None and anim_raw is None and notes is None and fixlog is None:
        return None
    return {
        "svg": svg_raw or "",
        "animation_raw": anim_raw or "",
        "notes": notes or "",
        "fixlog": fixlog or "",
    }


def _parse_by_fences(text: str) -> dict[str, str]:
    svg_parts: list[str] = []
    json_parts: list[str] = []
    for lang, body in _FENCE_RE.findall(text):
        key = (lang or "").strip().lower()
        chunk = (body or "").strip()
        if not chunk:
            continue
        if key == "svg":
            svg_parts.append(chunk)
        elif key == "json":
            json_parts.append(chunk)
    notes = _FENCE_RE.sub("", text).strip()
    return {
        "svg": svg_parts[0] if svg_parts else "",
        "animation_raw": json_parts[0] if json_parts else "",
        "notes": notes,
    }


def _parse_animation_json(anim_raw: str) -> dict[str, Any] | None:
    if not (anim_raw or "").strip():
        return None
    try:
        parsed = json.loads(anim_raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def parse_later_response(text: str) -> dict[str, Any]:
    """Разложить ответ на svg, animation (dict|None), notes, animation_raw."""
    raw = (text or "").strip()
    parts = _parse_by_markers(raw)
    used_markers = parts is not None
    if parts is None:
        parts = _parse_by_fences(raw)

    normalize_meta: dict[str, Any] = {"used_markers": used_markers}
    svg, svg_meta = _normalize_svg_block(parts.get("svg") or "")
    normalize_meta["svg"] = svg_meta

    anim_raw, anim_meta = _normalize_json_block(parts.get("animation_raw") or "")
    normalize_meta["animation"] = anim_meta

    animation = _parse_animation_json(anim_raw)

    return {
        "svg": svg,
        "animation_raw": anim_raw,
        "animation": animation,
        "notes": (parts.get("notes") or "").strip(),
        "fixlog": (parts.get("fixlog") or "").strip(),
        "normalize": normalize_meta,
    }


def _collect_svg_ids(svg: str) -> set[str]:
    root = ET.fromstring(svg)
    ids: set[str] = set()

    def walk(el: ET.Element) -> None:
        eid = el.get("id")
        if eid:
            ids.add(eid)
        for child in el:
            walk(child)

    walk(root)
    return ids


def _validate_svg_xml(svg: str) -> list[str]:
    errors: list[str] = []
    if not (svg or "").strip():
        errors.append("SVG пустой.")
        return errors
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        if _BROKEN_TEXT_LINE.search(svg):
            errors.append(
                "SVG: строки с id=\"…\" без <text> (авто-починка не справилась)."
            )
        errors.append(f"SVG не парсится как XML: {exc}")
    else:
        if _BROKEN_TEXT_LINE.search(svg):
            errors.append(
                "SVG: остались строки с id=\"…\" без <text> после авто-починки."
            )
    return errors


def _validate_animation(
    animation: dict[str, Any] | None,
    anim_raw: str,
    svg_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    if not anim_raw.strip():
        errors.append("JSON анимации пустой.")
        return errors
    if animation is None:
        errors.append(
            "JSON анимации невалиден (ожидается объект). "
            "Проверьте, что между маркерами нет ```json."
        )
        return errors

    duration_sec = animation.get("duration_sec")
    fps = animation.get("fps")
    try:
        fps_f = float(fps) if fps is not None else 30.0
    except (TypeError, ValueError):
        fps_f = 30.0
        errors.append("fps должен быть числом; использован fallback 30.")

    max_frame: int | None = None
    if duration_sec is not None:
        try:
            max_frame = int(round(float(duration_sec) * fps_f))
        except (TypeError, ValueError):
            errors.append("duration_sec должен быть числом.")

    tracks = animation.get("tracks")
    if not isinstance(tracks, list):
        errors.append("В JSON ожидается массив tracks[].")
        return errors

    for i, tr in enumerate(tracks):
        if not isinstance(tr, dict):
            errors.append(f"tracks[{i}]: ожидается объект.")
            continue
        tid = str(tr.get("id") or "").strip()
        if tid and tid not in svg_ids:
            errors.append(f'tracks[{i}]: id "{tid}" отсутствует в SVG.')
        anim = str(tr.get("anim") or "").strip()
        if anim and anim not in _allowed_anims():
            allowed_list = ", ".join(sorted(_allowed_anims()))
            errors.append(
                f'tracks[{i}]: anim "{anim}" не в словаре. '
                f"Разрешено: {allowed_list}"
            )
        for key in ("start", "end"):
            if key not in tr:
                continue
            try:
                frame = int(tr[key])
            except (TypeError, ValueError):
                errors.append(f"tracks[{i}].{key}: ожидается целое число кадров.")
                continue
            if max_frame is not None and frame > max_frame:
                errors.append(
                    f"tracks[{i}].{key}={frame} выходит за хронометраж "
                    f"(max {max_frame} кадров при {duration_sec}s × {fps_f:.0f} fps)."
                )
    return errors


def validate_later_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    """Три проверки: XML SVG, id tracks в SVG, anim + кадры."""
    errors: list[str] = []
    warnings: list[str] = []
    norm = parsed.get("normalize") if isinstance(parsed.get("normalize"), dict) else {}
    svg_norm = norm.get("svg") if isinstance(norm.get("svg"), dict) else {}
    anim_norm = norm.get("animation") if isinstance(norm.get("animation"), dict) else {}

    if svg_norm.get("fence_stripped") or anim_norm.get("fence_stripped"):
        warnings.append("Снята двойная обёртка ``` внутри маркеров (оставлен чистый SVG/JSON).")
    n_fix = int(svg_norm.get("text_tags_repaired") or 0)
    if n_fix:
        warnings.append(f"Авто-вставлено <text>…</text>: {n_fix} фрагм.")

    svg = str(parsed.get("svg") or "")
    errors.extend(_validate_svg_xml(svg))

    svg_ids: set[str] = set()
    if svg.strip() and not any(e.startswith("SVG не парсится") for e in errors):
        try:
            svg_ids = _collect_svg_ids(svg)
        except ET.ParseError:
            pass

    anim_raw = str(parsed.get("animation_raw") or "")
    if anim_raw.strip():
        errors.extend(
            _validate_animation(
                parsed.get("animation")
                if isinstance(parsed.get("animation"), dict)
                else None,
                anim_raw,
                svg_ids,
            )
        )
    else:
        warnings.append(
            "JSON анимации нет — проверяется только блок ===SVG_START=== … ===SVG_END===."
        )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "svg_id_count": len(svg_ids),
    }


def extract_animation_raw(text: str) -> str:
    """Сырой JSON анимации из ответа (маркеры, fence или голый объект)."""
    parsed = parse_later_response(text)
    anim_raw = str(parsed.get("animation_raw") or "").strip()
    if anim_raw:
        return anim_raw
    raw = (text or "").strip()
    if raw.startswith("{") or raw.startswith("["):
        norm, _ = _normalize_json_block(raw)
        return norm
    return ""


def replace_animation_in_later_text(full_text: str, new_anim_raw: str) -> tuple[str, str | None]:
    """Подставить JSON анимации в полный ответ Later… (между ANIM_START и ANIM_END)."""
    raw = full_text or ""
    new_inner, _meta = _normalize_json_block(new_anim_raw or "")
    if not new_inner:
        return raw, "JSON анимации пустой."

    start = raw.find(MARKER_ANIM_START)
    end = raw.find(MARKER_ANIM_END)
    if start >= 0 and end > start:
        before = raw[: start + len(MARKER_ANIM_START)]
        after = raw[end:]
        return f"{before}\n{new_inner}\n{after}", None

    parts = parse_later_response(raw)
    svg_raw = str(parts.get("svg") or "").strip()
    notes = str(parts.get("notes") or "").strip()
    chunks: list[str] = []
    if svg_raw:
        chunks.append(f"{MARKER_SVG_START}\n{svg_raw}\n{MARKER_SVG_END}")
    chunks.append(f"{MARKER_ANIM_START}\n{new_inner}\n{MARKER_ANIM_END}")
    if notes:
        chunks.append(f"{MARKER_NOTES_START}\n{notes}\n{MARKER_NOTES_END}")
    if not chunks:
        return raw, "В ответе нет SVG — сначала соберите кадр."
    return "\n\n".join(chunks) + "\n", None


def validate_animation_for_svg(text: str, svg: str) -> dict[str, Any]:
    """Проверка только JSON анимации против id из SVG кадра."""
    errors: list[str] = []
    warnings: list[str] = []
    anim_raw = extract_animation_raw(text)
    anim_norm, anim_meta = _normalize_json_block(anim_raw)
    if anim_meta.get("fence_stripped"):
        warnings.append("Снята обёртка ``` внутри блока анимации.")
    animation = _parse_animation_json(anim_norm)

    svg_body = (svg or "").strip()
    if not svg_body:
        errors.append("SVG кадра пустой — нельзя проверить tracks[].id.")
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
            "svg_id_count": 0,
        }

    svg_ids: set[str] = set()
    svg_errors = _validate_svg_xml(svg_body)
    if svg_errors:
        warnings.extend(svg_errors)
    else:
        try:
            svg_ids = _collect_svg_ids(svg_body)
        except ET.ParseError:
            pass

    errors.extend(
        _validate_animation(
            animation if isinstance(animation, dict) else None,
            anim_norm,
            svg_ids,
        )
    )
    parsed = {
        "animation_raw": anim_norm,
        "animation": animation,
        "normalize": {"animation": anim_meta},
    }
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "svg_id_count": len(svg_ids),
        "parsed": parsed,
    }


def process_animation_model_response(text: str, svg: str) -> dict[str, Any]:
    """Разбор ответа модели с одним блоком ANIM + валидация против SVG кадра."""
    v = validate_animation_for_svg(text, svg)
    parsed = v.pop("parsed", {})
    return {"parsed": parsed, "validation": v}


def process_later_model_response(text: str) -> dict[str, Any]:
    """Полный пайплайн: parse + validate."""
    parsed = parse_later_response(text)
    validation = validate_later_parsed(parsed)
    return {"parsed": parsed, "validation": validation}
