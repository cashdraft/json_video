"""Сверка ключевых слов dino_prompt (LLM) с label из Grounding DINO."""

from __future__ import annotations

import re
from typing import Any

from overlay_text_dino_draw import parse_dino_result_payload
from overlay_text_grounding_dino import extract_dino_prompt


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def split_prompt_terms(dino_prompt: str) -> list[str]:
    raw = _norm(dino_prompt)
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[.\n;,]+", raw) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = _norm(p)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def unique_dino_labels(detections: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in detections:
        if not isinstance(item, dict):
            continue
        lab = _norm(item.get("label") or "")
        if not lab or lab in seen:
            continue
        seen.add(lab)
        out.append(lab)
    return out


def term_matches_label(term: str, label: str) -> bool:
    term = _norm(term)
    label = _norm(label)
    if not term or not label:
        return False
    if term == label:
        return True
    if term in label or label in term:
        return True
    term_words = term.split()
    label_words = label.split()
    if term_words and all(w in label_words or w in label for w in term_words):
        return True
    if label_words and all(w in term_words or w in term for w in label_words):
        return True
    if term.endswith("s") and term[:-1] == label:
        return True
    if label.endswith("s") and label[:-1] == term:
        return True
    return bool(re.search(r"\b" + re.escape(term) + r"\b", label))


def compare_dino_prompt_to_detections(
    dino_prompt: str,
    detections: list[Any],
) -> dict[str, Any]:
    llm_terms = split_prompt_terms(dino_prompt)
    labels = unique_dino_labels(detections)

    matched: list[str] = []
    llm_only: list[str] = []
    for term in llm_terms:
        if any(term_matches_label(term, lab) for lab in labels):
            matched.append(term)
        else:
            llm_only.append(term)

    dino_only: list[str] = []
    for lab in labels:
        if not any(term_matches_label(term, lab) for term in llm_terms):
            dino_only.append(lab)

    return {
        "ok": len(llm_only) == 0 and len(llm_terms) > 0,
        "llm_term_count": len(llm_terms),
        "dino_label_count": len(labels),
        "matched": matched,
        "llm_only": llm_only,
        "dino_only": dino_only,
    }


def check_remotion_dino_keywords(
    *,
    rp_result: str,
    rp_dino_result: str,
) -> tuple[dict[str, Any] | None, str | None]:
    prompt, perr = extract_dino_prompt(rp_result, "")
    if perr:
        return None, perr

    payload, derr = parse_dino_result_payload(rp_dino_result)
    if derr or not payload:
        return None, derr or "Result from DINO пуст."

    detections = payload.get("detections") or []
    if not isinstance(detections, list):
        return None, "В Result from DINO нет массива detections."

    check = compare_dino_prompt_to_detections(prompt, detections)
    check["dino_prompt"] = prompt
    return check, None
