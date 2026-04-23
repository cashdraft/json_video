"""
ReWrite Master — цепочка этапов: Analysis → Architect → Block Writer → Draft2 Retention Editor → Draft3 → Final.
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
        f"Целевой объём текста: ~{target} символов.",
        "Применяй следующие правила длины итогового текста (JSON):",
        "```json",
        json.dumps(length_spec, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines)


def build_duration_length_spec_payload(
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> dict[str, Any]:
    """JSON payload для блока Duration (для user-сообщения)."""
    if duration_minutes is None or chars_per_minute is None:
        return {}
    dm = max(1, min(30, int(duration_minutes)))
    cpm = max(1, min(2000, int(chars_per_minute)))
    target = dm * cpm
    target_min = max(1, target - 1000)
    target_max = target + 1000
    return {
        "length_spec": {
            "target_chars_min": target_min,
            "target_chars_ideal": target,
            "target_chars_max": target_max,
            "hard_limit": True,
        }
    }


def _json_user_message(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_rewrite_system_prompt(
    master_prompt: str,
    stage_prompt: str,
    source_text: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """System-сообщение для этапа: Master → промпт этапа (Duration уходит в user)."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    p = (stage_prompt or "").strip()
    if p:
        parts.append(p)
    return "\n\n".join(parts)


def build_structure_user_message(analysis_last_result: str, structure_user_prompt: str) -> str:
    """User для этапа Architect: JSON с analysis.json + User Promt."""
    ar = (analysis_last_result or "").strip()
    up = (structure_user_prompt or "").strip()
    return _json_user_message(
        {
            "architect_user_promt": up or "",
            "analysis.json": ar or "(пусто)",
        }
    )


def build_analysis_user_message(source_text: str, analysis_user_prompt: str) -> str:
    """User для этапа Analysis: JSON с User Promt + Input text."""
    up = (analysis_user_prompt or "").strip()
    return _json_user_message(
        {
            "analysis_user_promt": up or "",
            "input_text": (source_text or "").strip() or "(пусто)",
        }
    )


def build_structure_system_prompt(
    master_prompt: str,
    structure_prompt: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """Только этап Architect: в system строго Master → Architect (Duration уходит в user)."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    sp = (structure_prompt or "").strip()
    if sp:
        parts.append(sp)
    return "\n\n".join(parts)


def build_draft1_rewriter_system_prompt(
    master_prompt: str,
    draft1_rewriter_prompt: str,
) -> str:
    """Этап draft1: в system строго Master → Block Writer Prompt (без Duration)."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    dr = (draft1_rewriter_prompt or "").strip()
    if dr:
        parts.append(dr)
    return "\n\n".join(parts)


def build_draft1_rewriter_user_message(
    analysis_last_result: str,
    structure_last_result: str,
    draft1_user_prompt: str,
    hero_prompt: str,
) -> str:
    """User для draft1: JSON с Hero + User Promt + analysis.json + architect.json."""
    ar = (analysis_last_result or "").strip() or "(пусто)"
    sr = (structure_last_result or "").strip() or "(пусто)"
    up = (draft1_user_prompt or "").strip()
    hp = (hero_prompt or "").strip()
    return _json_user_message(
        {
            "hero_prompt": hp or "",
            "block_writer_user_promt": up or "",
            "analysis.json": ar,
            "architect.json": sr,
        }
    )


def build_draft2_retention_editor_system_prompt(
    master_prompt: str,
    draft2_retention_editor_prompt: str,
    *,
    duration_minutes: int | None = None,
    chars_per_minute: int | None = None,
) -> str:
    """Этап draft2: в system строго Master → Draft2 Retention Editor Prompt (Duration/Hero в user)."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    p = (draft2_retention_editor_prompt or "").strip()
    if p:
        parts.append(p)
    return "\n\n".join(parts)


def build_draft2_retention_editor_user_message(draft1_last_result: str, hero_prompt: str) -> str:
    """User для draft2: JSON с Hero + Block Writer Result."""
    body = (draft1_last_result or "").strip() or "(пусто)"
    hp = (hero_prompt or "").strip()
    return _json_user_message(
        {
            "hero_prompt": hp or "",
            "block_writer_result": body,
        }
    )


# (ключ в JSON, заголовок в UI)
REWRITE_STAGES: list[tuple[str, str]] = [
    ("analysis", "Analysis"),
    ("structure", "Architect"),
    ("draft1", "Block Writer"),
    ("draft2", "Draft2 Retention Editor"),
    ("draft3", "Draft3"),
    ("final", "Final"),
]

REWRITE_STAGE_KEYS: frozenset[str] = frozenset(k for k, _ in REWRITE_STAGES)

_STAGE_ORDER_INDEX: dict[str, int] = {k: i for i, (k, _) in enumerate(REWRITE_STAGES)}

# Подписи под заголовком этапа в UI.
REWRITE_STAGE_SEND_HINTS: dict[str, str] = {
    "analysis": (
        "Отправляем. В System (по порядку): Master Prompt, Analysis Prompt. "
        "В User (по порядку): Duration, Analysis User Promt, Input text."
    ),
    "structure": (
        "Отправляем. В System (по порядку): Master Prompt, Architect Prompt. "
        "В User (по порядку): Duration, Architect User Promt, analysis.json."
    ),
    "draft1": (
        "Отправляем. В System (по порядку): Master Prompt, Block Writer Prompt. "
        "В User (по порядку): Duration, Hero Prompt, Block Writer User Promt, analysis.json, architect.json. "
        "Draft1 идёт block-by-block: каждый блок проверяется по target_chars_min/max из architect.json "
        "и только после accept запускается следующий."
    ),
    "draft2": (
        "Отправляем. В System (по порядку): Master Prompt, Draft2 Retention Editor Prompt. "
        "В User (по порядку): Duration, Hero Prompt, Block Writer Result."
    ),
    "draft3": (
        "Отправляем. В System (по порядку): Master Prompt, Draft3 Prompt. "
        "В User (по порядку): Duration, analysis.json, architect.json, "
        "Block Writer Result, Draft2 Retention Editor Result."
    ),
    "final": (
        "Отправляем. В System (по порядку): Master Prompt, Final Prompt. "
        "В User (по порядку): Duration, analysis.json, architect.json, "
        "Block Writer Result, Draft2 Retention Editor Result, Draft3 Result."
    ),
}

REWRITE_STAGE_HELP_HINTS: dict[str, str] = {
    "analysis": (
        "Этот агент не пишет сценарий. Он делает только одно: разбирает исходный текст "
        "на смысловые компоненты, чтобы дальше Architect и Block Writer могли нормально работать. "
        "Он должен понять: о чём текст на самом деле; какие там главные идеи; какие факты и цифры "
        "нельзя терять; что в тексте слабое; что можно адаптировать; что нужно переписать заново; "
        "какая логика движения у исходника."
    ),
    "structure": (
        "Да. Второй агент — это Architect, то есть агент, который не пишет текст, "
        "а разбивает материал на блоки и строит структуру ролика на основе analysis.json. "
        "Именно он превращает сырой анализ в будущий каркас сценария."
    ),
    "draft1": "Это главный агент, который уже пишет сам текст блоков.",
    "draft2": "Редактирует Draft1 для удержания внимания: усиливает подачу, ритм и переходы без потери смысла.",
    "draft3": "Полирует текст после Draft2: улучшает читаемость, связность и формулировки перед финалом.",
    "final": "Формирует финальную версию сценария: итоговая вычитка и сборка готового текста.",
}


def default_stage_entry() -> dict[str, Any]:
    return {
        "prompt": "",
        "user_prompt": "",
        "model": REWRITE_DEFAULT_MODEL,
        "last_result": "",
        "prompt_locked": False,
        "user_prompt_locked": False,
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
        e.setdefault("user_prompt", "")
        e.setdefault("last_result", "")
        e.setdefault("model", REWRITE_DEFAULT_MODEL)
        e.setdefault("prompt_locked", False)
        e.setdefault("user_prompt_locked", False)
        e["model"] = normalize_rewrite_model(str(e.get("model", "")))
        e["prompt_locked"] = bool(e.get("prompt_locked"))
        e["user_prompt_locked"] = bool(e.get("user_prompt_locked"))

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
        if "user_prompt" in sv:
            e["user_prompt"] = str(sv.get("user_prompt") or "")
        if locked_in_body is not None:
            e["prompt_locked"] = bool(locked_in_body)
        user_locked_in_body = sv.get("user_prompt_locked") if "user_prompt_locked" in sv else None
        if user_locked_in_body is not None:
            e["user_prompt_locked"] = bool(user_locked_in_body)
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
        user_text = build_structure_user_message(
            analysis_res,
            str(cell.get("user_prompt") or ""),
        )
    elif stage_key == "analysis":
        prompt = build_rewrite_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            source_text,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
        )
        user_text = build_analysis_user_message(
            source_text,
            str(cell.get("user_prompt") or ""),
        )
    elif stage_key == "draft1":
        analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
        structure_res = str((stages_snap.get("structure") or {}).get("last_result") or "")
        prompt = build_draft1_rewriter_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
        )
        user_text = build_draft1_rewriter_user_message(
            analysis_res,
            structure_res,
            str(cell.get("user_prompt") or ""),
            hero_prompt,
        )
    elif stage_key == "draft2":
        draft1_res = str((stages_snap.get("draft1") or {}).get("last_result") or "")
        prompt = build_draft2_retention_editor_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
        )
        user_text = build_draft2_retention_editor_user_message(draft1_res, hero_prompt)
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
    dur_payload = build_duration_length_spec_payload(
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
    )
    if dur_payload:
        try:
            user_obj = json.loads(user_text) if user_text else {}
        except json.JSONDecodeError:
            user_obj = {"input": user_text}
        if isinstance(user_obj, dict):
            merged = {"duration": dur_payload}
            merged.update(user_obj)
            user_text = _json_user_message(merged)
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
    """User-сообщение: JSON с Hero (кроме analysis), исходником и результатами предыдущих этапов.

    Duration и length_spec добавляется в user на этапе compose.
    """
    payload: dict[str, Any] = {}
    h = (hero_prompt or "").strip()
    if h and stage_key != "analysis":
        payload["hero_promt"] = h
    payload["input_text"] = (source_text or "").strip() or "(пусто)"
    prev_results: dict[str, str] = {}
    idx = _STAGE_ORDER_INDEX[stage_key]
    for i in range(idx):
        pk, plabel = REWRITE_STAGES[i]
        block = (stages.get(pk) or {}).get("last_result") or ""
        if pk == "analysis":
            prev_results["analysis.json"] = block.strip() or "(пусто)"
        elif pk == "structure":
            prev_results["architect.json"] = block.strip() or "(пусто)"
        else:
            prev_results[f"{pk}_result"] = block.strip() or "(пусто)"
    if prev_results:
        payload["previous_stage_results"] = prev_results
    return _json_user_message(payload)


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
            "user_prompt": str(cell.get("user_prompt") or ""),
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
