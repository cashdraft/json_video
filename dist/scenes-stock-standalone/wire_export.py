"""Pretty JSON export for OpenAI/Claude wire payloads (button J)."""

from __future__ import annotations

import copy
import json
from typing import Any

_MAX_OPENAI_EXPORT_JSON_DEPTH = 32


def _json_loads_fully(s: str) -> Any | None:
    t = (s or "").lstrip("\ufeff")
    if not t or not t.strip():
        return None
    try:
        return json.loads(t)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _wrap_plaintext_for_export(s: str) -> Any:
    s = s or ""
    if "\n" not in s and "\r" not in s and "\u2028" not in s and "\u2029" not in s:
        return s
    lines = s.splitlines()
    return {"_export": "text_lines", "lines": lines if lines else [""]}


def _expand_value_for_openai_export(val: Any, depth: int = 0) -> Any:
    if depth > _MAX_OPENAI_EXPORT_JSON_DEPTH:
        return val
    if isinstance(val, str):
        s = val or ""
        t = s.strip()
        if not t:
            return _wrap_plaintext_for_export(s)
        p = _json_loads_fully(s)
        if p is not None:
            return _expand_value_for_openai_export(p, depth + 1)
        return _wrap_plaintext_for_export(s)
    if isinstance(val, dict):
        return {str(k): _expand_value_for_openai_export(v, depth + 1) for k, v in val.items()}
    if isinstance(val, list):
        return [_expand_value_for_openai_export(v, depth + 1) for v in val]
    return val


def _message_content_for_openai_export(c: str) -> Any:
    if not isinstance(c, str):
        return c
    p = _json_loads_fully(c)
    if p is not None:
        return _expand_value_for_openai_export(p, 0)
    return _wrap_plaintext_for_export(c)


def _body_for_pretty_openai_export(body: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(body)
    sy = out.get("system")
    if isinstance(sy, str):
        out["system"] = _message_content_for_openai_export(sy)
    elif isinstance(sy, (dict, list)):
        out["system"] = _expand_value_for_openai_export(sy, 0)
    msgs = out.get("messages")
    if not isinstance(msgs, list):
        return out
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = _message_content_for_openai_export(c)
    return out


def format_openai_wire_payloads_txt(
    bodies: list[dict[str, Any]],
    *,
    header_lines: list[str] | None = None,
    about: str | None = None,
) -> str:
    if about is None:
        about = "Wire payloads for LLM API (readable export)."
    pretty = [_body_for_pretty_openai_export(b) for b in bodies]
    env: dict[str, Any] = {"about": about, "requests": pretty}
    if header_lines:
        notes = [str(ln) for ln in header_lines if str(ln).strip()]
        if notes:
            env["notes"] = notes
    return json.dumps(env, ensure_ascii=False, indent=2) + "\n"
