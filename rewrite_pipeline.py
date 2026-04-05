"""
ReWrite Master — цепочка этапов: Analysis → Structure → Draft1–3 → Final.
"""

from __future__ import annotations

from typing import Any

from rewrite_openai import REWRITE_DEFAULT_MODEL, normalize_rewrite_model


def combine_system_prompt(stage_prompt: str, master_prompt: str) -> str:
    """Системное сообщение: промпт этапа + Master Prompt (через пустую строку)."""
    p = (stage_prompt or "").strip()
    m = (master_prompt or "").strip()
    if p and m:
        return f"{p}\n\n{m}"
    return p or m

# (ключ в JSON, заголовок в UI)
REWRITE_STAGES: list[tuple[str, str]] = [
    ("analysis", "Analysis"),
    ("structure", "Structure"),
    ("draft1", "Draft1"),
    ("draft2", "Draft2"),
    ("draft3", "Draft3"),
    ("final", "Final"),
]

REWRITE_STAGE_KEYS: frozenset[str] = frozenset(k for k, _ in REWRITE_STAGES)

_STAGE_ORDER_INDEX: dict[str, int] = {k: i for i, (k, _) in enumerate(REWRITE_STAGES)}


def default_stage_entry() -> dict[str, Any]:
    return {
        "prompt": "",
        "model": REWRITE_DEFAULT_MODEL,
        "last_result": "",
        "prompt_locked": False,
    }


def new_stages_dict() -> dict[str, dict[str, Any]]:
    return {k: default_stage_entry() for k in REWRITE_STAGE_KEYS}


def normalize_rewrite_job_data(job: dict[str, Any]) -> dict[str, Any]:
    """Приводит job к схеме с source_text и stages; миграция со старых полей."""
    job.setdefault("source_text", "")
    if not (job.get("source_text") or "").strip():
        legacy = (job.get("last_text") or "").strip()
        if legacy:
            job["source_text"] = legacy

    stages = job.get("stages")
    if not isinstance(stages, dict):
        stages = new_stages_dict()
        job["stages"] = stages

    for key in REWRITE_STAGE_KEYS:
        if key not in stages or not isinstance(stages[key], dict):
            stages[key] = default_stage_entry()
            continue
        e = stages[key]
        e.setdefault("prompt", "")
        e.setdefault("last_result", "")
        e.setdefault("model", REWRITE_DEFAULT_MODEL)
        e.setdefault("prompt_locked", False)
        e["model"] = normalize_rewrite_model(str(e.get("model", "")))
        e["prompt_locked"] = bool(e.get("prompt_locked"))

    # Старый формат: один промпт и один ответ → первый этап и Final
    if not any(str((stages[k].get("last_result") or "")).strip() for k in REWRITE_STAGE_KEYS):
        legacy_r = str(job.get("last_result") or "").strip()
        if legacy_r:
            stages["final"]["last_result"] = legacy_r
    if not any(str((stages[k].get("prompt") or "")).strip() for k in REWRITE_STAGE_KEYS):
        legacy_p = str(job.get("last_prompt") or "").strip()
        if legacy_p:
            stages["analysis"]["prompt"] = legacy_p

    job.setdefault("source_locked", False)
    job["source_locked"] = bool(job.get("source_locked"))
    job.setdefault("master_prompt", "")
    job.setdefault("master_prompt_locked", False)
    job["master_prompt_locked"] = bool(job.get("master_prompt_locked"))

    try:
        dm = int(job.get("duration_minutes", 5))
    except (TypeError, ValueError):
        dm = 5
    job["duration_minutes"] = max(1, min(30, dm))

    return job


def any_stage_has_result(job: dict[str, Any]) -> bool:
    st = job.get("stages")
    if isinstance(st, dict):
        for k in REWRITE_STAGE_KEYS:
            if ((st.get(k) or {}).get("last_result") or "").strip():
                return True
    return bool((job.get("last_result") or "").strip())


def merge_stages_from_request(rw: dict[str, Any], body_stages: Any) -> None:
    if not isinstance(body_stages, dict):
        return
    rw.setdefault("stages", new_stages_dict())
    for sk, sv in body_stages.items():
        if sk not in REWRITE_STAGE_KEYS or not isinstance(sv, dict):
            continue
        rw["stages"].setdefault(sk, default_stage_entry())
        e = rw["stages"][sk]
        e.setdefault("prompt_locked", False)
        locked_in_body = sv.get("prompt_locked") if "prompt_locked" in sv else None
        if "prompt" in sv:
            if not e.get("prompt_locked") or locked_in_body is False:
                e["prompt"] = str(sv.get("prompt") or "")
        if locked_in_body is not None:
            e["prompt_locked"] = bool(locked_in_body)
        if "model" in sv:
            e["model"] = normalize_rewrite_model(str(sv.get("model") or ""))
        if "last_result" in sv:
            e["last_result"] = str(sv.get("last_result") or "")


def validate_prerequisites(stage_key: str, stages: dict[str, Any]) -> str | None:
    """None если ок, иначе текст ошибки для пользователя."""
    if stage_key not in REWRITE_STAGE_KEYS:
        return "Неизвестный этап."
    idx = _STAGE_ORDER_INDEX[stage_key]
    if idx == 0:
        return None
    for i in range(idx):
        pk, plabel = REWRITE_STAGES[i]
        prev = stages.get(pk) or {}
        if not str(prev.get("last_result") or "").strip():
            return f"Сначала выполните этап «{plabel}» — нет сохранённого результата."
    return None


def build_stage_user_message(source_text: str, stage_key: str, stages: dict[str, Any]) -> str:
    """User-сообщение: исходный текст + результаты всех предыдущих этапов."""
    lines: list[str] = [
        "Данные для текущего этапа конвейера ReWrite (исходник и результаты предыдущих шагов).",
        "",
        "--- Исходный текст пользователя ---",
        (source_text or "").strip() or "(пусто)",
    ]
    idx = _STAGE_ORDER_INDEX[stage_key]
    for i in range(idx):
        pk, plabel = REWRITE_STAGES[i]
        block = (stages.get(pk) or {}).get("last_result") or ""
        lines.append("")
        lines.append(f"--- Результат этапа «{plabel}» ({pk}) ---")
        lines.append(block.strip() or "(пусто)")
    return "\n".join(lines)


def snapshot_stages_from_body(body: dict[str, Any]) -> tuple[str, dict[str, dict[str, str]]]:
    """Из тела запроса run: source_text и stages с дефолтами."""
    source_text = str(body.get("source_text") or "")
    raw = body.get("stages")
    stages: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        raw = {}
    for key in REWRITE_STAGE_KEYS:
        cell = raw.get(key)
        if not isinstance(cell, dict):
            cell = {}
        stages[key] = {
            "prompt": str(cell.get("prompt") or ""),
            "model": normalize_rewrite_model(str(cell.get("model") or "")),
            "last_result": str(cell.get("last_result") or ""),
        }
    return source_text, stages


def snapshot_master_prompt_from_body(body: dict[str, Any]) -> str:
    return str(body.get("master_prompt") or "")
