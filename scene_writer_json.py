"""Починка JSON-строк Scene Writer (построчные объекты для parse_scene_blocks)."""

from __future__ import annotations

import json


def repair_scene_json_line(line: str) -> tuple[str, bool]:
    """
    Одна строка = один JSON-объект сцены.
    Частая ошибка LLM: {"start":{"prompt":"…"} — не хватает закрывающей `}`.
    """
    original = line
    s = (line or "").strip()
    if not s or not s.startswith("{"):
        return original, False

    def _canonical(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            canon = _canonical(obj)
            return canon, canon != s
        return original, False
    except json.JSONDecodeError:
        pass

    fixed = s
    for _ in range(8):
        try:
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                return _canonical(obj), True
        except json.JSONDecodeError:
            fixed += "}"
    return original, False


def normalize_scene_writer_result(raw: str) -> tuple[str, bool]:
    """Нормализует весь Result Scene Writer: каждая непустая строка — валидный JSON."""
    if not (raw or "").strip():
        return raw or "", False
    lines = (raw or "").splitlines()
    out: list[str] = []
    changed = False
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        fixed, ch = repair_scene_json_line(line)
        out.append(fixed)
        changed = changed or ch
    return "\n".join(out), changed


def normalize_scene_blocks_raw_text(raw_text: str) -> tuple[str, bool]:
    """То же для поля «JSON-код сцен» перед parse_scene_blocks."""
    return normalize_scene_writer_result(raw_text)
