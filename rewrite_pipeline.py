"""
ReWrite Master — цепочка этапов: Analysis → Structure → Draft1 Rewriter → Draft2 Retention Editor → Draft3 → Final.
"""

from __future__ import annotations

import json
from typing import Any

from rewrite_openai import REWRITE_CHAT_TEMPERATURE, REWRITE_DEFAULT_MODEL, normalize_rewrite_model


def format_duration_length_spec_block(
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """Блок Duration для system: ориентир + JSON length_spec (без поля mode)."""
    if duration_minutes is None or chars_per_minute is None:
        return ""
    dm = max(1, min(30, int(duration_minutes)))
    cpm = max(1, min(2000, int(chars_per_minute)))
    target = dm * cpm
    target_min = max(1, target - 1000)
    target_max = target + 1000
    length_spec = {
        "length_spec": {
            "target_chars_min": target_min,
            "target_chars_ideal": target,
            "target_chars_max": target_max,
            "hard_limit": True,
        }
    }
    lines = [
        "--- Ориентир объёма озвучки (шаблон проекта) ---",
        (
            f"Целевая длительность: {dm} мин. Ориентир: ~{target} символов "
            f"({cpm} симв./мин)."
        ),
        "Применяй следующие правила длины итогового текста (JSON):",
        "```json",
        json.dumps(length_spec, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines)


def build_rewrite_system_prompt(
    master_prompt: str,
    stage_prompt: str,
    source_text: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """System-сообщение для этапа: строго Master → Duration → промпт этапа (через \\n\\n)."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    dur = format_duration_length_spec_block(
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
    ).strip()
    if dur:
        parts.append(dur)
    p = (stage_prompt or "").strip()
    if p:
        parts.append(p)
    return "\n\n".join(parts)


def build_structure_user_message(analysis_last_result: str) -> str:
    """User для этапа Structure: только Analysis Result."""
    ar = (analysis_last_result or "").strip()
    return "--- Analysis Result ---\n" + (ar or "(пусто)")


def build_structure_system_prompt(
    master_prompt: str,
    structure_prompt: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """Только этап Structure: в system строго Master → Duration → Structure."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    dur = format_duration_length_spec_block(
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
    ).strip()
    if dur:
        parts.append(dur)
    sp = (structure_prompt or "").strip()
    if sp:
        parts.append(sp)
    return "\n\n".join(parts)


def build_draft1_rewriter_system_prompt(
    master_prompt: str,
    draft1_rewriter_prompt: str,
    hero_prompt: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """Этап draft1: в system строго Master → Duration → Hero → Draft1 Rewriter Prompt."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    dur = format_duration_length_spec_block(
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
    ).strip()
    if dur:
        parts.append(dur)
    h = (hero_prompt or "").strip()
    if h:
        parts.append("--- Hero Prompt ---\n" + h)
    dr = (draft1_rewriter_prompt or "").strip()
    if dr:
        parts.append(dr)
    return "\n\n".join(parts)


def build_draft1_rewriter_user_message(
    analysis_last_result: str,
    structure_last_result: str,
) -> str:
    """User для draft1: Analysis Result, затем Structure Result."""
    ar = (analysis_last_result or "").strip() or "(пусто)"
    sr = (structure_last_result or "").strip() or "(пусто)"
    return (
        "--- Analysis Result ---\n"
        + ar
        + "\n\n--- Structure Result ---\n"
        + sr
    )


def build_draft2_retention_editor_system_prompt(
    master_prompt: str,
    draft2_retention_editor_prompt: str,
    hero_prompt: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """Этап draft2: в system строго Master → Duration → Hero → Draft2 Retention Editor Prompt."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    dur = format_duration_length_spec_block(
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
    ).strip()
    if dur:
        parts.append(dur)
    h = (hero_prompt or "").strip()
    if h:
        parts.append("--- Hero Prompt ---\n" + h)
    p = (draft2_retention_editor_prompt or "").strip()
    if p:
        parts.append(p)
    return "\n\n".join(parts)


def build_draft2_retention_editor_user_message(draft1_last_result: str) -> str:
    """User для draft2: только Draft1 Rewriter Result."""
    body = (draft1_last_result or "").strip() or "(пусто)"
    return "--- Draft1 Rewriter Result ---\n" + body


# (ключ в JSON, заголовок в UI)
REWRITE_STAGES: list[tuple[str, str]] = [
    ("analysis", "Analysis"),
    ("structure", "Structure"),
    ("draft1", "Draft1 Rewriter"),
    ("draft2", "Draft2 Retention Editor"),
    ("draft3", "Draft3"),
    ("final", "Final"),
]

REWRITE_STAGE_KEYS: frozenset[str] = frozenset(k for k, _ in REWRITE_STAGES)

_STAGE_ORDER_INDEX: dict[str, int] = {k: i for i, (k, _) in enumerate(REWRITE_STAGES)}

# Подписи под заголовком этапа в UI.
REWRITE_STAGE_SEND_HINTS: dict[str, str] = {
    "analysis": "Отправляем: Master Prompt, Analysis Prompt, Duration , Input text.",
    "structure": (
        "Отправляем: Master Prompt, Structure Prompt, Duration , Analysis Result. "
        "Analysis Result уходит в user, остальные блоки в system."
    ),
    "draft1": (
        "Отправляем. В system (по порядку): Master Prompt, Duration, Hero Prompt, Draft1 Rewriter Prompt. "
        "В user (по порядку): Analysis Result, Structure Result. "
        "Draft1 идёт block-by-block: каждый блок проверяется по target_chars_min/max из Structure Result "
        "и только после accept запускается следующий."
    ),
    "draft2": (
        "Отправляем. В system (по порядку): Master Prompt, Duration, Hero Prompt, "
        "Draft2 Retention Editor Prompt. В user (по порядку): Draft1 Rewriter Result."
    ),
    "draft3": (
        "Отправляем: Master Prompt, Draft3 Prompt, Duration , результаты Analysis, Structure, "
        "Draft1 Rewriter и Draft2 Retention Editor"
    ),
    "final": (
        "Отправляем: Master Prompt, Final Prompt, Duration , результаты Analysis, Structure, "
        "Draft1 Rewriter, Draft2 Retention Editor и Draft3"
    ),
}


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

    job.setdefault("hero_prompt", "")
    job["hero_prompt"] = str(job.get("hero_prompt") or "")
    try:
        cpm = int(job.get("chars_per_minute", 344))
    except (TypeError, ValueError):
        cpm = 344
    job["chars_per_minute"] = max(1, min(2000, cpm))
    job.setdefault("rewrite_template", "")
    job["rewrite_template"] = str(job.get("rewrite_template") or "")

    job.setdefault("hero_prompt_locked", False)
    job["hero_prompt_locked"] = bool(job.get("hero_prompt_locked"))
    job.setdefault("audio_timing_locked", False)
    job["audio_timing_locked"] = bool(job.get("audio_timing_locked"))

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
        # Полный snapshot с клиента: промпт всегда сохраняем, даже если этап locked.
        # Иначе «Применить шаблон» и правки в заблокированных полях не попадают в JSON проекта.
        if "prompt" in sv:
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


def stage_run_prerequisites_met(stage_key: str, stages: dict[str, Any]) -> bool:
    """True, если для этапа можно запускать генерацию (у предыдущих этапов есть сохранённый Result)."""
    return validate_prerequisites(stage_key, stages) is None


def compose_rewrite_openai_request_body(
    stage_key: str,
    *,
    source_text: str,
    stages_snap: dict[str, Any],
    master_prompt: str,
    hero_prompt: str,
    duration_minutes: int,
    chars_per_minute: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Тело POST к OpenAI chat/completions — то же, что при запуске этапа. Ошибка → (None, текст)."""
    if stage_key not in REWRITE_STAGE_KEYS:
        return None, "Неизвестный этап."
    if stage_key not in ("structure", "draft2") and not (source_text or "").strip():
        return None, "Введите исходный текст в верхнем поле."
    pre_err = validate_prerequisites(stage_key, stages_snap)
    if pre_err:
        return None, pre_err
    cell = stages_snap.get(stage_key) or {}
    model = normalize_rewrite_model(str(cell.get("model") or ""))
    if stage_key == "structure":
        analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
        prompt = build_structure_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
        )
        user_text = build_structure_user_message(analysis_res)
    elif stage_key == "draft1":
        analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
        structure_res = str((stages_snap.get("structure") or {}).get("last_result") or "")
        prompt = build_draft1_rewriter_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            hero_prompt,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
        )
        user_text = build_draft1_rewriter_user_message(analysis_res, structure_res)
    elif stage_key == "draft2":
        draft1_res = str((stages_snap.get("draft1") or {}).get("last_result") or "")
        prompt = build_draft2_retention_editor_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            hero_prompt,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
        )
        user_text = build_draft2_retention_editor_user_message(draft1_res)
    else:
        prompt = build_rewrite_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            source_text,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
        )
        user_text = build_stage_user_message(
            source_text,
            stage_key,
            stages_snap,
            hero_prompt=hero_prompt,
        )
    prompt = (prompt or "").strip()
    user_text = (user_text or "").strip()
    if not prompt:
        return None, "Введите промпт (инструкцию для модели)."
    if not user_text:
        return None, "Введите текст для обработки."
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": REWRITE_CHAT_TEMPERATURE,
    }
    return payload, None


def build_stage_user_message(
    source_text: str,
    stage_key: str,
    stages: dict[str, Any],
    *,
    hero_prompt: str = "",
) -> str:
    """User-сообщение: Hero (кроме analysis), исходный текст, результаты предыдущих этапов.

    Duration и length_spec только в system (см. build_rewrite_system_prompt).
    """
    lines: list[str] = [
        "Данные для текущего этапа конвейера ReWrite (исходник и результаты предыдущих шагов).",
        "",
    ]
    h = (hero_prompt or "").strip()
    if h and stage_key != "analysis":
        lines.append("--- Описание героя (шаблон проекта) ---")
        lines.append(h)
        lines.append("")
    lines.append("--- Исходный текст пользователя ---")
    lines.append((source_text or "").strip() or "(пусто)")
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


def snapshot_pipeline_extras_from_body(body: dict[str, Any]) -> tuple[str, int, int]:
    """hero_prompt, duration_minutes (1–30), chars_per_minute (1–2000)."""
    hero = str(body.get("hero_prompt") or "")
    try:
        dm = int(body.get("duration_minutes"))
        dm = max(1, min(30, dm))
    except (TypeError, ValueError):
        dm = 5
    try:
        cpm = int(body.get("chars_per_minute"))
        cpm = max(1, min(2000, cpm))
    except (TypeError, ValueError):
        cpm = 344
    return hero, dm, cpm
