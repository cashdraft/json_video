"""
ReWrite Master — цепочка этапов: Analysis → Architect → Block Writer → редакторы
(Retention → Hook → Flow → Persona → Voiceover → Title Strategist / Structure Splitter → Scene Writer).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rewrite_openai import REWRITE_CHAT_TEMPERATURE, REWRITE_DEFAULT_MODEL, normalize_rewrite_model

TARGET_CHARS_MIN = 500
TARGET_CHARS_MAX = 40_000
TARGET_CHARS_STEP = 500


def clamp_target_chars(n: int | None) -> int:
    """500–40 000 симв., шаг 500."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = 1500
    stepped = int(round(v / TARGET_CHARS_STEP)) * TARGET_CHARS_STEP
    return max(TARGET_CHARS_MIN, min(TARGET_CHARS_MAX, stepped))


def format_duration_length_spec_block(
    *,
    target_chars: int | None = None,
) -> str:
    """Блок Duration для system: ориентир + JSON length_spec (без поля mode)."""
    if target_chars is None:
        return ""
    target = clamp_target_chars(int(target_chars))
    length_spec = {
        "length_spec": {
            "target_chars_ideal": target,
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
    target_chars: int | None = None,
) -> dict[str, Any]:
    """JSON payload для блока Duration (для user-сообщения)."""
    if target_chars is None:
        return {}
    target = clamp_target_chars(int(target_chars))
    return {
        "length_spec": {
            "target_chars_ideal": target,
            "hard_limit": True,
        }
    }


def _json_user_message(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_edited_text(raw_text: str) -> str:
    """Normalize escaped newlines coming from model JSON fields."""
    txt = str(raw_text or "")
    # Some model outputs include literal escaped sequences, sometimes double-escaped
    # (e.g. "\\n", "\\\\n", "\\\\\\n"). Collapse any count of backslashes.
    txt = re.sub(r"\\+r\\+n", "\n", txt)
    txt = re.sub(r"\\+n", "\n", txt)
    txt = re.sub(r"\\+r", "\n", txt)
    txt = txt.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    # Keep paragraphing readable but avoid runaway empty lines for TTS stages.
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _extract_edited_text(raw_payload: str) -> str:
    raw = str(raw_payload or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return _normalize_edited_text(str(obj.get("edited_text") or ""))
    except json.JSONDecodeError:
        pass
    return _normalize_edited_text(raw)


def build_rewrite_system_prompt(
    master_prompt: str,
    stage_prompt: str,
    source_text: str,
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


def build_retention_editor_system_prompt(
    retention_editor_prompt: str,
) -> str:
    """Этап retention_editor: в system только Retention Editor System Promt."""
    return (retention_editor_prompt or "").strip()


def build_retention_editor_user_message(
    retention_editor_user_prompt: str,
    block_writer_full_text: str = "",
) -> str:
    """User для retention_editor: User Promt + full_text.txt из Block Writer."""
    up = (retention_editor_user_prompt or "").strip()
    ft = str(block_writer_full_text or "")
    payload: dict[str, Any] = {
        "retention_editor_user_promt": up or "",
        "full_text.txt": ft,
    }
    return _json_user_message(payload)


def build_hook_editor_system_prompt(
    hook_editor_prompt: str,
) -> str:
    """Этап hook_editor: в system только Hook Editor System Promt."""
    return (hook_editor_prompt or "").strip()


def build_hook_editor_user_message(
    hook_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    """User для hook_editor: User Promt + edited_text."""
    up = (hook_editor_user_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    payload: dict[str, Any] = {
        "hook_editor_user_promt": up or "",
        "edited_text": et,
    }
    return _json_user_message(payload)


def build_flow_editor_system_prompt(
    flow_editor_prompt: str,
) -> str:
    """Этап flow_editor: в system только Flow Editor System Promt."""
    return (flow_editor_prompt or "").strip()


def build_flow_editor_user_message(
    flow_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    """User для flow_editor: User Promt + edited_text."""
    up = (flow_editor_user_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    payload: dict[str, Any] = {
        "flow_editor_user_promt": up or "",
        "edited_text": et,
    }
    return _json_user_message(payload)


def build_persona_editor_system_prompt(
    persona_editor_prompt: str,
) -> str:
    """Этап persona_editor: в system только Persona Editor System Promt."""
    return (persona_editor_prompt or "").strip()


def build_persona_editor_user_message(
    persona_editor_user_prompt: str,
    hero_prompt: str = "",
    edited_text: str = "",
) -> str:
    """User для persona_editor: User Promt + Hero Prompt + edited_text."""
    up = (persona_editor_user_prompt or "").strip()
    hp = (hero_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    payload: dict[str, Any] = {
        "persona_editor_user_promt": up or "",
        "hero_prompt": hp,
        "edited_text": et,
    }
    return _json_user_message(payload)


def build_rewrite_stage_system_prompt(rewrite_prompt: str) -> str:
    """Этап rewrite (пресет «Я уже ЗАrewriteИЛ»): в system только Rewrite System Promt.

    Имя `build_rewrite_stage_system_prompt`, а не `build_rewrite_system_prompt`, чтобы не
    конфликтовать с одноимённой функцией для глобального master/analysis-промпта выше.
    """
    return (rewrite_prompt or "").strip()


def build_rewrite_stage_user_message(
    rewrite_user_prompt: str,
    inbox_text: str = "",
) -> str:
    """Plain-text user для rewrite: User Promt + пустая строка + Inbox.Result (без JSON).

    Пустые куски аккуратно опускаются, чтобы не отправлять одинокие переводы строк.
    """
    up = (rewrite_user_prompt or "").strip()
    body = (inbox_text or "").strip()
    if up and body:
        return f"{up}\n\n{body}"
    return up or body


def build_voiceover_editor_system_prompt(
    voiceover_editor_prompt: str,
) -> str:
    """Этап voiceover_editor: в system только Voiceover Editor System Promt."""
    return (voiceover_editor_prompt or "").strip()


def build_voiceover_editor_user_message(
    voiceover_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    """User для voiceover_editor: User Promt + edited_text (без Hero Prompt)."""
    up = (voiceover_editor_user_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    payload: dict[str, Any] = {
        "voiceover_editor_user_promt": up or "",
        "edited_text": et,
    }
    return _json_user_message(payload)


def build_title_strategist_system_prompt(
    title_strategist_prompt: str,
) -> str:
    """Этап title_strategist: в system только Title Strategist System Promt."""
    return (title_strategist_prompt or "").strip()


def build_title_strategist_user_message(
    title_strategist_user_prompt: str,
    edited_text: str = "",
    *,
    original_title: str = "",
) -> str:
    """User для title_strategist: original_title + User Promt + edited_text из Voiceover Editor.

    В user JSON поле original_title идёт первым (удобно в экспорте). В тексте user-promt подставляется
    плейсхолдер {{ORIGINAL_TITLE}}, если он есть в шаблоне.
    """
    up = (title_strategist_user_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    tit = (original_title or "").strip()
    repl = tit if tit else "(пусто)"
    up = up.replace("{{ORIGINAL_TITLE}}", repl)
    payload: dict[str, Any] = {
        "original_title": repl,
        "title_strategist_user_promt": up or "",
        "edited_text": et,
    }
    return _json_user_message(payload)


def apply_title_strategist_original_title_to_user_json(user_json_str: str, original_title: str) -> str:
    """Всегда добавляет/обновляет original_title в user JSON Title Strategist и подставляет {{ORIGINAL_TITLE}} в промпт.

    Вызывается из app после compose — чтобы в экспорте и в реальном POST поле не терялось при рассинхроне кода.
    """
    val_raw = (original_title or "").strip()
    val_disp = val_raw if val_raw else "(пусто)"
    try:
        obj = json.loads(user_json_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return user_json_str
    if not isinstance(obj, dict):
        return user_json_str
    tsp = obj.get("title_strategist_user_promt")
    if isinstance(tsp, str):
        s = tsp.replace("{{ORIGINAL_TITLE}}", val_disp)
        s = s.replace("{{ ORIGINAL_TITLE }}", val_disp)
        obj["title_strategist_user_promt"] = s
    obj["original_title"] = val_disp
    ordered: dict[str, Any] = {"original_title": val_disp}
    for k, v in obj.items():
        if k == "original_title":
            continue
        ordered[k] = v
    return json.dumps(ordered, ensure_ascii=False, indent=2)


def build_structure_splitter_system_prompt(
    structure_splitter_prompt: str,
) -> str:
    """Этап structure_splitter: в system только Structure Splitter System Promt."""
    return (structure_splitter_prompt or "").strip()


def build_structure_splitter_user_message(
    structure_splitter_user_prompt: str,
    voiceover_full_text: str = "",
) -> str:
    """User для structure_splitter: User Promt + full_text.txt из Voiceover Editor."""
    up = (structure_splitter_user_prompt or "").strip()
    raw = str(voiceover_full_text or "").strip()
    payload: dict[str, Any] = {
        "structure_splitter_user_promt": up or "",
        "full_text.txt": raw,
    }
    return _json_user_message(payload)


def build_youtube_packaging_system_prompt(packaging_prompt: str) -> str:
    """Этап youtube_packaging: в system только YouTube packaging System Promt."""
    return (packaging_prompt or "").strip()


def build_youtube_packaging_user_message(
    user_prompt: str,
    title_strategist_result: str = "",
) -> str:
    """User: YouTube packaging User Promt + Result этапа Title Strategist."""
    up = (user_prompt or "").strip()
    raw = str(title_strategist_result or "").strip()
    payload: dict[str, Any] = {
        "youtube_packaging_user_promt": up or "",
        "title_strategist_result": raw or "(пусто)",
    }
    return _json_user_message(payload)


# (ключ в JSON, заголовок в UI)
REWRITE_STAGES: list[tuple[str, str]] = [
    ("inbox", "Inbox"),
    ("rewrite", "Rewrite"),
    ("analysis", "Analysis"),
    ("structure", "Architect"),
    ("draft1", "Block Writer"),
    ("retention_editor", "Retention Editor"),
    ("hook_editor", "Hook Editor"),
    ("flow_editor", "Flow Editor"),
    ("persona_editor", "Persona Editor"),
    ("voiceover_editor", "Voiceover Editor"),
    ("title_strategist", "Title Strategist"),
    ("structure_splitter", "Structure Splitter"),
    ("scene_writer", "Scene Writer"),
    ("scene_writer_live", "Scene Writer Live"),
    ("youtube_packaging", "YouTube packaging engine"),
]

REWRITE_STAGE_KEYS: frozenset[str] = frozenset(k for k, _ in REWRITE_STAGES)

_STAGE_ORDER_INDEX: dict[str, int] = {k: i for i, (k, _) in enumerate(REWRITE_STAGES)}


# --- Presets: «Глубокий Rewrite», «Я уже ЗАrewriteИЛ», «Мягкий Rewrite» -----
#
# Глубокий       = текущий пайплайн (Analysis → Architect → Block Writer → редакторы …).
# Я уже ЗАrewriteИЛ = текст уже готов, его вставляют в Inbox, далее опциональный
#                     Rewrite → Voiceover Editor / Title Strategist / Structure Splitter.
# Мягкий Rewrite  = то же, что «Я уже ЗАrewriteИЛ», но без карточки Inbox: исходник
#                   берётся из поля Source (верх страницы); дальше Rewrite → …
#
# Эти пресеты используются:
#   - валидатором предусловий (`validate_prerequisites`) — чтобы preset_X не
#     требовал результат «чужого» этапа из другого пресета;
#   - UI (скрывает карточки не из текущего пресета и определяет порядок «Run pipeline»);
#   - runner-ом запуска этапов.
REWRITE_PRESET_DEEP = "deep"
REWRITE_PRESET_PREWRITTEN = "prewritten"
REWRITE_PRESET_SOFT = "soft"
REWRITE_PRESET_KEYS: frozenset[str] = frozenset(
    {REWRITE_PRESET_DEEP, REWRITE_PRESET_PREWRITTEN, REWRITE_PRESET_SOFT}
)
REWRITE_PRESET_DEFAULT = REWRITE_PRESET_DEEP

REWRITE_PRESET_LABELS: dict[str, str] = {
    REWRITE_PRESET_DEEP: "Глубокий Rewrite",
    REWRITE_PRESET_PREWRITTEN: "Я уже ЗАrewriteИЛ",
    REWRITE_PRESET_SOFT: "Мягкий Rewrite",
}

REWRITE_PRESET_STAGE_KEYS: dict[str, list[str]] = {
    REWRITE_PRESET_DEEP: [
        "analysis",
        "structure",
        "draft1",
        "retention_editor",
        "hook_editor",
        "flow_editor",
        "persona_editor",
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "scene_writer_live",
        "youtube_packaging",
    ],
    # «Я уже ЗАrewriteИЛ»: текст уже готов, его просто вставляют в Inbox
    # (Result-only этап без модели/промпта). Voiceover Editor / Title Strategist /
    # Structure Splitter читают исходник прямо из Inbox (или из Rewrite.Result,
    # если запускали опциональный Rewrite). Scene Writer / SWL /
    # YouTube packaging работают как в остальных пресетах — от Structure Splitter
    # / Scene Writer / Title Strategist соответственно.
    REWRITE_PRESET_PREWRITTEN: [
        "inbox",
        "rewrite",
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "scene_writer_live",
        "youtube_packaging",
    ],
    # «Мягкий Rewrite» — как prewritten, но без Inbox: исходник в поле Source.
    REWRITE_PRESET_SOFT: [
        "rewrite",
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "scene_writer_live",
        "youtube_packaging",
    ],
}


def normalize_rewrite_preset(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in REWRITE_PRESET_KEYS:
        return v
    return REWRITE_PRESET_DEFAULT


def normalize_rewrite_pipeline_language(value: Any) -> str:
    """Язык конвейера (UI): ru | en. Значение хранится в project.json как `rewrite_pipeline_language`."""
    v = str(value or "").strip().lower()
    if v in ("en", "english", "англ"):
        return "en"
    return "ru"


def stages_for_preset(preset: str) -> list[str]:
    return list(REWRITE_PRESET_STAGE_KEYS.get(normalize_rewrite_preset(preset), []))

# Подписи под заголовком этапа в UI.
REWRITE_STAGE_SEND_HINTS: dict[str, str] = {
    "rewrite": (
        "Пресет «Я уже ЗАrewriteИЛ» — Rewrite. В System: Rewrite System Promt. "
        "В User (по порядку): Rewrite User Promt и Result этапа Inbox (вставленный готовый текст). "
        "Если Result Rewrite заполнен — следующие Voiceover Editor / Title Strategist / Structure Splitter "
        "берут текст уже из Rewrite, иначе продолжают читать Inbox.Result. "
        "Пресет «Мягкий Rewrite» — то же без Inbox: в User подставляется текст из поля Source (верх страницы); "
        "три следующих агента берут Rewrite.Result, если он есть, иначе тот же Source."
    ),
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
    "retention_editor": (
        "Отправляем. В System: Retention Editor System Promt. "
        "В User (по порядку): Retention Editor User Promt, full_text.txt из Block Writer."
    ),
    "hook_editor": (
        "Отправляем. В System: Hook Editor System Promt. "
        "В User (по порядку): Hook Editor User Promt, edited_text."
    ),
    "flow_editor": (
        "Отправляем. В System: Flow Editor System Promt. "
        "В User (по порядку): Flow Editor User Promt, edited_text."
    ),
    "persona_editor": (
        "Отправляем. В System: Persona Editor System Promt. "
        "В User (по порядку): Persona Editor User Promt, Hero Prompt, edited_text."
    ),
    "voiceover_editor": (
        "Отправляем. В System: Voiceover Editor System Promt. "
        "В User (по порядку): Voiceover Editor User Promt, edited_text."
    ),
    "title_strategist": (
        "Отправляем. В System: Title Strategist System Promt. "
        "В User (по порядку): original_title (из «Исходное название»), Title Strategist User Promt "
        "(в тексте {{ORIGINAL_TITLE}} заменяется на это значение), edited_text из Voiceover Editor."
    ),
    "structure_splitter": (
        "Отправляем. В System: Structure Splitter System Promt. "
        "В User (по порядку): Structure Splitter User Promt, full_text.txt из Voiceover Editor."
    ),
    "scene_writer": (
        "Отправляем block-by-block. В System: Scene Writer System Promt. "
        "В User: Scene Writer User Promt, Scene Writer Style Promt, параметры длины сцены и текущий block."
    ),
    "scene_writer_live": (
        "Отправляем batch-by-batch по 50 сцен. В System: Scene Writer Live System Promt. "
        "В User: Scene Writer Live User Promt, content_type, target_percent, Result этапа Scene Writer и текущий batch."
    ),
    "youtube_packaging": (
        "Отправляем один POST. В System: YouTube packaging System Promt. "
        "В User: YouTube packaging User Promt и поле title_strategist_result (Result этапа Title Strategist). "
        "В превью блок заголовков — только top_5_titles. Плюс final_description, hashtags, thumbnail_options по схеме в System Promt."
    ),
}

# Краткие подзаголовки под названием этапа (мутный серый под заголовком в карточке).
REWRITE_STAGE_SUBTITLES: dict[str, str] = {
    "inbox": "Готовый текст: вставьте сюда — дальше пойдёт Rewrite → Voiceover Editor → Title Strategist → Structure Splitter",
    "rewrite": "Агент-доработчик готового текста (Inbox → Rewrite)",
    "analysis": "Агент-аналитик YouTube-сценариев",
    "structure": "Агент-архитектор структуры YouTube-сценария",
    "draft1": "Агент-сценарист одного блока",
    "retention_editor": "Агент-редактор удержания",
    "hook_editor": "Агент-редактор хуков",
    "flow_editor": "Агент-редактор потока",
    "persona_editor": "Агент-редактор персонажа",
    "voiceover_editor": "Агент-редактор войсовера",
    "title_strategist": "Агент-стратег заголовков",
    "structure_splitter": "Агент-сплиттер текста",
    "scene_writer": "",
    "scene_writer_live": "",
    "youtube_packaging": "",
}

REWRITE_STAGE_HELP_HINTS: dict[str, str] = {
    "inbox": (
        "Inbox — это не агент, а просто вход для уже готового текста сценария. "
        "Вставьте сюда финальный текст в поле Result и нажмите Сгенерировать — "
        "пайплайн пойдёт через Rewrite (если запустите) и далее Voiceover Editor, минуя все стадии написания."
    ),
    "rewrite": (
        "Пресет «Я уже ЗАrewriteИЛ». Rewrite — лёгкая правка готового текста из Inbox: "
        "в System кладётся Rewrite System Promt, в User — Rewrite User Promt и текст из Inbox.Result "
        "(простой склейкой через пустую строку, без обёртки в JSON). "
        "Когда Result Rewrite заполнен, следующие Voiceover Editor / Title Strategist / Structure Splitter "
        "берут текст уже из Rewrite. Если Rewrite не запускали — они продолжают читать Inbox.Result."
    ),
    "analysis": (
        "Этот агент глубоко разбирает исходный текст YouTube-ролика и превращает его "
        "в структурированную аналитическую основу для написания нового сценария. "
        "Он не пересказывает и не переписывает — он извлекает главный тезис, ключевые идеи, "
        "факты и числа, логику аргументации, а также выявляет слабые места, чужой голос "
        "и возможности для удержания внимания. На выходе — детальный JSON-отчёт "
        "с классификацией каждого элемента текста по принципу "
        "«сохранить точно / сохранить смысл / адаптировать / переписать»."
    ),
    "structure": (
        "Этот агент принимает на вход готовый analysis.json от первого агента и строит "
        "из него детальный структурный план будущего длинного ролика. Он не пишет текст — "
        "только проектирует: определяет общую нарративную дугу, оптимальное количество "
        "смысловых блоков, роль и цель каждого блока, лимиты символов, что обязательно "
        "покрыть, а что запрещено, и где нужны хуки или re-hooks. "
        "На выходе — JSON-скелет, по которому следующий агент сможет писать сценарий поблочно."
    ),
    "draft1": (
        "Этот агент получает задание на конкретный блок из архитектурного плана и пишет "
        "только его — ничего лишнего. Он учитывает предыдущий контекст, строго соблюдает "
        "лимиты символов, цель блока и ограничения на содержание, пишет живым разговорным "
        "языком под войсовер. На выходе — JSON с готовым текстом блока, его длиной "
        "и кратким смысловым резюме, которое помогает следующему агенту не повторяться."
    ),
    "retention_editor": (
        "Этот агент берёт уже логически выверенный сценарий и точечно усиливает удержание "
        "внимания — находит от 3 до 10 зон, где зритель рискует отвалиться, и исправляет "
        "только их: вставляет re-hooks, усиливает stakes, добавляет контраст и смысловое "
        "движение вперёд. Глобальную логику, стиль героя и continuity он не трогает. "
        "На выходе — полный обновлённый текст и список конкретных правок с объяснением, "
        "почему каждая из них удерживает внимание лучше."
    ),
    "hook_editor": (
        "Этот агент получает сценарий после логической и retention-редактуры и точечно "
        "усиливает только хуки — opening hook, curiosity, contrast, stakes, re-hooks, "
        "open loops и payoff bridges. Он не переписывает стиль, не трогает логику "
        "и не перегружает текст манипуляциями — только находит реально слабые hook-зоны "
        "(обычно 3–8 мест) и исправляет их так, чтобы хук усиливал смысл, а не заменял его. "
        "На выходе — полный обновлённый текст и список конкретных hook-правок."
    ),
    "flow_editor": (
        "Этот агент берёт сценарий после всех предыдущих редакторов и устраняет "
        "исключительно flow-проблемы: слабые переходы между блоками, резкие смысловые "
        "скачки, сломанные мосты между абзацами и ощущение склейки отдельных кусков. "
        "Он не трогает логику, хуки, стиль или voiceover — только добавляет точечные "
        "bridge-фразы или сглаживает переходы там, где текст ощущается как набор "
        "несвязанных фрагментов. На выходе — полный текст с естественным движением "
        "и список конкретных flow-правок."
    ),
    "persona_editor": (
        "Этот агент финально приводит сценарий к чистому и стабильному голосу героя — "
        "в данном случае Naomi, аналитичного финансового рассказчика с умным, спокойным "
        "и слегка сухим стилем. Он вычищает чужой голос, блогерские вставки, лишнюю "
        "эмоциональность и «нейросеточную стерильность» — но не трогает смысл, структуру "
        "и логику. На выходе — полный текст, звучащий как живой человек с характером, "
        "и список точечных правок с объяснением, почему каждая из них усиливает соответствие герою."
    ),
    "voiceover_editor": (
        "Этот агент берёт финально отредактированный сценарий и адаптирует его под живую "
        "озвучку: разбивает слишком длинные предложения, убирает перегруженные конструкции, "
        "добавляет паузы и выстраивает ритм по принципу «короткое → среднее → короткое → удар». "
        "Смысл, структуру и стиль героя он не трогает — только делает так, чтобы текст "
        "легко ложился в дыхание и естественно звучал вслух. На выходе — полный текст, "
        "готовый к записи, и список точечных правок."
    ),
    "title_strategist": (
        "Этот агент анализирует готовый сценарий и извлекает из него структурированную "
        "стратегию для создания сильных YouTube-заголовков — но сами заголовки не генерирует. "
        "Он определяет core topic, боль зрителя, эмоциональное напряжение, парадокс, "
        "сильнейшие углы подачи, curiosity gaps и SEO-ключи, а также разбирает оригинальный "
        "заголовок по формуле: что в нём работает, что сохранить, что не копировать. "
        "На выходе — JSON-стратегия, которую следующий агент использует для генерации заголовков."
    ),
    "structure_splitter": (
        "Этот агент берёт длинный готовый текст и разбивает его на логические блоки — "
        "без единого изменения формулировок. Он ориентируется на смысловые сдвиги: смену "
        "темы, новый аргумент, переход нарратива или изменение тона. Каждый блок содержит "
        "точный оригинальный текст без купюр, получает короткое название и описание своей "
        "роли в повествовании. На выходе — JSON-массив блоков, покрывающий весь исходный "
        "текст без пропусков и перекрытий."
    ),
    "scene_writer": "Идёт по блокам из Structure Splitter и переписывает каждый блок отдельно, затем склеивает.",
    "scene_writer_live": "Берёт Result из Scene Writer и пакетно (по 50 сцен) дополняет сцены media-полями.",
    "youtube_packaging": (
        "Собирает заголовки, описание, хештеги и идеи превью для YouTube из готового вывода Scene Writer."
    ),
}


def default_stage_entry() -> dict[str, Any]:
    return {
        "prompt": "",
        "user_prompt": "",
        "style_prompt": "",
        "past_prompt": "",
        "scene_writer_live_check": None,
        "scene_writer_check": None,
        "structure_splitter_check": None,
        "block_writer_check": None,
        "model": REWRITE_DEFAULT_MODEL,
        "last_result": "",
        "prompt_locked": False,
        "user_prompt_locked": False,
        "style_prompt_locked": False,
        "past_prompt_locked": True,
    }


def new_stages_dict() -> dict[str, dict[str, Any]]:
    return {k: default_stage_entry() for k in REWRITE_STAGE_KEYS}


def normalize_rewrite_job_data(job: dict[str, Any]) -> dict[str, Any]:
    """Приводит job к схеме с source_text и stages; миграция со старых полей."""
    job["rewrite_preset"] = normalize_rewrite_preset(job.get("rewrite_preset"))
    job["rewrite_pipeline_language"] = normalize_rewrite_pipeline_language(
        job.get("rewrite_pipeline_language")
    )
    job.setdefault("source_text", "")
    job.setdefault("source_text_ru", "")
    job["source_text_ru"] = str(job.get("source_text_ru") or "")
    job.setdefault("source_text_ru_locked", False)
    job["source_text_ru_locked"] = bool(job.get("source_text_ru_locked"))
    job.setdefault("voiceover_final_text", "")
    job["voiceover_final_text"] = str(job.get("voiceover_final_text") or "")
    job.setdefault("voiceover_final_locked", True)
    job["voiceover_final_locked"] = bool(job.get("voiceover_final_locked", True))
    job.setdefault("voiceover_final_text_ru", "")
    job["voiceover_final_text_ru"] = str(job.get("voiceover_final_text_ru") or "")
    job.setdefault("voiceover_final_text_ru_locked", False)
    job["voiceover_final_text_ru_locked"] = bool(job.get("voiceover_final_text_ru_locked"))
    if not (job.get("source_text") or "").strip():
        legacy = (job.get("last_text") or "").strip()
        if legacy:
            job["source_text"] = legacy

    stages = job.get("stages")
    if not isinstance(stages, dict):
        stages = new_stages_dict()
        job["stages"] = stages

    # Миграция: старый ключ voice_flow_editor_2 → title_strategist.
    if "voice_flow_editor_2" in stages:
        old_v2 = stages.pop("voice_flow_editor_2")
        if isinstance(old_v2, dict):
            cur = stages.get("title_strategist")
            if isinstance(cur, dict):
                cur.update(old_v2)
            else:
                stages["title_strategist"] = old_v2

    # Миграция: scene_media_planner → scene_writer_live.
    if "scene_media_planner" in stages:
        old_swl = stages.pop("scene_media_planner")
        if isinstance(old_swl, dict):
            cur = stages.get("scene_writer_live")
            if isinstance(cur, dict):
                cur.update(old_swl)
            else:
                stages["scene_writer_live"] = old_swl

    for key in REWRITE_STAGE_KEYS:
        if key not in stages or not isinstance(stages[key], dict):
            stages[key] = default_stage_entry()
            continue
        e = stages[key]
        e.setdefault("prompt", "")
        e.setdefault("user_prompt", "")
        e.setdefault("style_prompt", "")
        e.setdefault("past_prompt", "")
        e.setdefault("scene_writer_check", None)
        e.setdefault("structure_splitter_check", None)
        e.setdefault("block_writer_check", None)
        e.setdefault("last_result", "")
        e.setdefault("model", REWRITE_DEFAULT_MODEL)
        e.setdefault("prompt_locked", False)
        e.setdefault("user_prompt_locked", False)
        e.setdefault("style_prompt_locked", False)
        e.setdefault("past_prompt_locked", True)
        e["model"] = normalize_rewrite_model(str(e.get("model", "")))
        e["prompt_locked"] = bool(e.get("prompt_locked"))
        e["user_prompt_locked"] = bool(e.get("user_prompt_locked"))
        e["style_prompt_locked"] = bool(e.get("style_prompt_locked"))
        e["past_prompt_locked"] = bool(e.get("past_prompt_locked"))
        # UX rule: Past in Promt should be collapsed on page load.
        if key == "scene_writer":
            e["past_prompt_locked"] = True
        if not isinstance(e.get("scene_writer_check"), dict):
            e["scene_writer_check"] = None
        if not isinstance(e.get("scene_writer_live_check"), dict):
            e["scene_writer_live_check"] = None
        e.pop("animation_settings", None)
        if isinstance(e.get("scene_media_check"), dict) and not isinstance(e.get("scene_writer_live_check"), dict):
            e["scene_writer_live_check"] = e.get("scene_media_check")
        e.pop("scene_media_check", None)
        if not isinstance(e.get("structure_splitter_check"), dict):
            e["structure_splitter_check"] = None
        if not isinstance(e.get("block_writer_check"), dict):
            e["block_writer_check"] = None

    voe = stages.get("voiceover_editor") if isinstance(stages.get("voiceover_editor"), dict) else None
    ts_st = stages.get("title_strategist") if isinstance(stages.get("title_strategist"), dict) else None
    if isinstance(ts_st, dict) and isinstance(voe, dict):
        if not str(ts_st.get("prompt") or "").strip() and str(voe.get("prompt") or "").strip():
            ts_st["prompt"] = str(voe.get("prompt") or "")
        if not str(ts_st.get("user_prompt") or "").strip() and str(voe.get("user_prompt") or "").strip():
            ts_st["user_prompt"] = str(voe.get("user_prompt") or "")

    for dead in list(stages.keys()):
        if dead not in REWRITE_STAGE_KEYS:
            del stages[dead]

    # Старый формат: один промпт и один ответ → первый этап и последний этап
    if not any(str((stages[k].get("last_result") or "")).strip() for k in REWRITE_STAGE_KEYS):
        legacy_r = str(job.get("last_result") or "").strip()
        if legacy_r:
            last_stage_key = REWRITE_STAGES[-1][0]
            stages[last_stage_key]["last_result"] = legacy_r
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
    if "target_chars" in job and job.get("target_chars") is not None and str(job.get("target_chars", "")).strip() != "":
        try:
            job["target_chars"] = clamp_target_chars(int(job["target_chars"]))
        except (TypeError, ValueError):
            job["target_chars"] = clamp_target_chars(job["duration_minutes"] * job["chars_per_minute"])
    else:
        job["target_chars"] = clamp_target_chars(
            int(job["duration_minutes"]) * int(job["chars_per_minute"])
        )
    job.setdefault("rewrite_template", "")
    job["rewrite_template"] = str(job.get("rewrite_template") or "")
    # Папка дефолтного шаблона переименована: baseline → Base Template
    if job["rewrite_template"] == "baseline":
        base_tpl = Path(__file__).resolve().parent / "rewrite_templates" / "Base Template"
        if base_tpl.is_dir():
            job["rewrite_template"] = "Base Template"

    job.setdefault("hero_prompt_locked", False)
    job["hero_prompt_locked"] = bool(job.get("hero_prompt_locked"))
    job.setdefault("audio_timing_locked", False)
    job["audio_timing_locked"] = bool(job.get("audio_timing_locked"))

    job.setdefault("source_title", "")
    job["source_title"] = str(job.get("source_title") or "")

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
        if "style_prompt" in sv:
            e["style_prompt"] = str(sv.get("style_prompt") or "")
        if "past_prompt" in sv:
            e["past_prompt"] = str(sv.get("past_prompt") or "")
        if "scene_writer_check" in sv:
            v = sv.get("scene_writer_check")
            e["scene_writer_check"] = v if isinstance(v, dict) else None
        if "scene_writer_live_check" in sv:
            v = sv.get("scene_writer_live_check")
            e["scene_writer_live_check"] = v if isinstance(v, dict) else None
        if "scene_media_check" in sv and "scene_writer_live_check" not in sv:
            v = sv.get("scene_media_check")
            e["scene_writer_live_check"] = v if isinstance(v, dict) else None
        if "structure_splitter_check" in sv:
            v = sv.get("structure_splitter_check")
            e["structure_splitter_check"] = v if isinstance(v, dict) else None
        if "block_writer_check" in sv:
            v = sv.get("block_writer_check")
            e["block_writer_check"] = v if isinstance(v, dict) else None
        if locked_in_body is not None:
            e["prompt_locked"] = bool(locked_in_body)
        user_locked_in_body = sv.get("user_prompt_locked") if "user_prompt_locked" in sv else None
        if user_locked_in_body is not None:
            e["user_prompt_locked"] = bool(user_locked_in_body)
        style_locked_in_body = sv.get("style_prompt_locked") if "style_prompt_locked" in sv else None
        if style_locked_in_body is not None:
            e["style_prompt_locked"] = bool(style_locked_in_body)
        past_locked_in_body = sv.get("past_prompt_locked") if "past_prompt_locked" in sv else None
        if past_locked_in_body is not None:
            e["past_prompt_locked"] = bool(past_locked_in_body)
        if "model" in sv:
            e["model"] = normalize_rewrite_model(str(sv.get("model") or ""))
        if "last_result" in sv:
            e["last_result"] = str(sv.get("last_result") or "")


_STAGE_LABEL_BY_KEY: dict[str, str] = {k: lbl for k, lbl in REWRITE_STAGES}


def validate_prerequisites(
    stage_key: str,
    stages: dict[str, Any],
    *,
    preset: str = REWRITE_PRESET_DEFAULT,
    source_text: str = "",
) -> str | None:
    """None если ок, иначе текст ошибки для пользователя.

    Учёт preset: цепочка зависимостей строится **в пределах выбранного пресета**.
    Этапы, не входящие в текущий preset, не считаются обязательными
    предками (Distiller/Author не блокируют Глубокий, и Analysis/Architect/
    Block Writer не блокируют Мягкий).
    """
    if stage_key not in REWRITE_STAGE_KEYS:
        return "Неизвестный этап."
    if stage_key == "inbox":
        # Inbox — Result-only, не запускается моделью. Предусловий нет.
        return None
    preset = normalize_rewrite_preset(preset)
    preset_order = REWRITE_PRESET_STAGE_KEYS.get(preset, [])
    if stage_key not in preset_order:
        # Этап выпадает из выбранного preset (например, "analysis" в Мягком) — нет
        # смысла валидировать его предусловия для запуска именно этого пресета.
        # Возвращаем None: запуск всё равно возможен (отдельной кнопкой), и его
        # результат не влияет на пайплайн другого пресета.
        return None
    idx = preset_order.index(stage_key)
    # «Мягкий Rewrite»: первый этап — Rewrite, ему нужен непустой Source (без Inbox).
    if preset == REWRITE_PRESET_SOFT and stage_key == "rewrite":
        if not str(source_text or "").strip():
            return "Сначала вставьте исходный текст в поле Source (верх страницы)."
        return None
    if idx == 0:
        return None
    # В пресете «Я уже ЗАrewriteИЛ» Rewrite — отдельный лёгкий агент после Inbox,
    # которому нужен только заполненный Inbox.Result. Сам Rewrite не зависит ни от чего,
    # кроме Inbox.
    if preset == REWRITE_PRESET_PREWRITTEN and stage_key == "rewrite":
        ibx = stages.get("inbox") or {}
        if not str(ibx.get("last_result") or "").strip():
            return "Сначала вставьте готовый текст в Inbox (Result)."
        return None
    # В пресете «Я уже ЗАrewriteИЛ» Voiceover Editor / Title Strategist /
    # Structure Splitter все три читают исходник из Rewrite.Result (если он есть)
    # либо из Inbox.Result (фолбэк). Им нужен любой из этих двух источников,
    # между собой они не зависят.
    if preset == REWRITE_PRESET_PREWRITTEN and stage_key in (
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
    ):
        rw_res = str((stages.get("rewrite") or {}).get("last_result") or "").strip()
        if rw_res:
            return None
        ibx = stages.get("inbox") or {}
        if not str(ibx.get("last_result") or "").strip():
            return "Сначала вставьте готовый текст в Inbox (Result) или прогоните Rewrite."
        return None
    # «Мягкий Rewrite» — те же три агента, но фолбэк к Inbox заменён на Source.
    if preset == REWRITE_PRESET_SOFT and stage_key in (
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
    ):
        rw_res = str((stages.get("rewrite") or {}).get("last_result") or "").strip()
        if rw_res:
            return None
        if str(source_text or "").strip():
            return None
        return "Сначала вставьте текст в Source или прогоните Rewrite."
    for i in range(idx):
        pk = preset_order[i]
        plabel = _STAGE_LABEL_BY_KEY.get(pk, pk)
        if stage_key in ("structure_splitter", "scene_writer") and pk == "title_strategist":
            continue
        if stage_key == "youtube_packaging" and pk == "scene_writer_live":
            continue
        prev = stages.get(pk) or {}
        if not str(prev.get("last_result") or "").strip():
            if pk == "inbox":
                return "Сначала вставьте готовый текст в Inbox (Result)."
            return f"Сначала выполните этап «{plabel}» — нет сохранённого результата."
    return None


def stage_run_prerequisites_met(
    stage_key: str,
    stages: dict[str, Any],
    *,
    preset: str = REWRITE_PRESET_DEFAULT,
    source_text: str = "",
) -> bool:
    """True, если для этапа можно запускать генерацию (у предыдущих этапов есть сохранённый Result)."""
    return validate_prerequisites(stage_key, stages, preset=preset, source_text=source_text) is None


def compose_rewrite_openai_request_body(
    stage_key: str,
    *,
    source_text: str,
    stages_snap: dict[str, Any],
    master_prompt: str,
    hero_prompt: str,
    target_chars: int,
    duration_minutes: int = 5,
    chars_per_minute: int = 344,
    block_writer_full_text: str = "",
    retention_editor_text: str = "",
    hook_editor_text: str = "",
    flow_editor_text: str = "",
    persona_editor_text: str = "",
    voiceover_editor_text: str = "",
    structure_splitter_text: str = "",
    title_strategist_result_text: str = "",
    scene_writer_result_text: str = "",
    original_title: str = "",
    preset: str = REWRITE_PRESET_DEFAULT,
) -> tuple[dict[str, Any] | None, str | None]:
    """Тело POST к OpenAI chat/completions — то же, что при запуске этапа. Ошибка → (None, текст)."""
    if stage_key not in REWRITE_STAGE_KEYS:
        return None, "Неизвестный этап."
    if stage_key == "inbox":
        # Inbox — это «вставь готовый текст в Result», его нечего запускать у модели.
        return None, "Inbox — это вход для готового текста, не этап для запуска. Просто вставьте текст в Result."
    if stage_key not in (
        "structure",
        "retention_editor",
        "hook_editor",
        "flow_editor",
        "persona_editor",
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "scene_writer_live",
        "youtube_packaging",
        "rewrite",
    ) and not (source_text or "").strip():
        return None, "Введите исходный текст в верхнем поле."
    pre_err = validate_prerequisites(stage_key, stages_snap, preset=preset, source_text=source_text)
    if pre_err:
        return None, pre_err
    cell = stages_snap.get(stage_key) or {}
    model = normalize_rewrite_model(str(cell.get("model") or ""))
    if stage_key == "structure":
        analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
        prompt = build_structure_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
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
    elif stage_key == "retention_editor":
        prompt = build_retention_editor_system_prompt(
            str(cell.get("prompt") or ""),
        )
        user_text = build_retention_editor_user_message(
            str(cell.get("user_prompt") or ""),
            block_writer_full_text,
        )
    elif stage_key == "hook_editor":
        prompt = build_hook_editor_system_prompt(
            str(cell.get("prompt") or ""),
        )
        user_text = build_hook_editor_user_message(
            str(cell.get("user_prompt") or ""),
            retention_editor_text,
        )
    elif stage_key == "flow_editor":
        prompt = build_flow_editor_system_prompt(
            str(cell.get("prompt") or ""),
        )
        user_text = build_flow_editor_user_message(
            str(cell.get("user_prompt") or ""),
            hook_editor_text,
        )
    elif stage_key == "persona_editor":
        prompt = build_persona_editor_system_prompt(
            str(cell.get("prompt") or ""),
        )
        user_text = build_persona_editor_user_message(
            str(cell.get("user_prompt") or ""),
            hero_prompt,
            flow_editor_text,
        )
    elif stage_key == "rewrite":
        preset_n = normalize_rewrite_preset(preset)
        if preset_n == REWRITE_PRESET_SOFT:
            inbox_text = str(source_text or "").strip()
            if not inbox_text:
                return None, "Сначала вставьте исходный текст в поле Source."
        elif preset_n == REWRITE_PRESET_PREWRITTEN:
            inbox_text = str((stages_snap.get("inbox") or {}).get("last_result") or "")
            if not inbox_text.strip():
                return None, "Сначала вставьте готовый текст в Inbox (Result)."
        else:
            return None, "Этап Rewrite в этом пресете не используется."
        prompt = build_rewrite_stage_system_prompt(str(cell.get("prompt") or ""))
        user_text = build_rewrite_stage_user_message(
            str(cell.get("user_prompt") or ""),
            inbox_text,
        )
    elif stage_key == "voiceover_editor":
        prompt = build_voiceover_editor_system_prompt(
            str(cell.get("prompt") or ""),
        )
        # В пресете «Я уже ЗАrewriteИЛ» (prewritten) Voiceover Editor запускается
        # после Inbox/Rewrite: на вход подаём Result Rewrite (если запускали Rewrite),
        # иначе — Inbox.Result.
        ve_input_text = persona_editor_text
        preset_n = normalize_rewrite_preset(preset)
        if preset_n == REWRITE_PRESET_PREWRITTEN:
            rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            ve_input_text = rw_res if rw_res.strip() else str(
                (stages_snap.get("inbox") or {}).get("last_result") or ""
            )
            if not ve_input_text.strip():
                return None, "Сначала вставьте готовый текст в Inbox (Result) или прогоните Rewrite."
        elif preset_n == REWRITE_PRESET_SOFT:
            rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            ve_input_text = rw_res if rw_res.strip() else str(source_text or "")
            if not ve_input_text.strip():
                return None, "Сначала вставьте текст в Source или прогоните Rewrite."
        user_text = build_voiceover_editor_user_message(
            str(cell.get("user_prompt") or ""),
            ve_input_text,
        )
    elif stage_key == "title_strategist":
        prompt = build_title_strategist_system_prompt(
            str(cell.get("prompt") or ""),
        )
        # В пресете «Я уже ЗАrewriteИЛ» Title Strategist берёт исходный текст
        # из Rewrite.Result (если есть) или, как фолбэк, из Inbox.Result —
        # чтобы все три финальных агента работали с одним и тем же исходником
        # уже после (опционального) прогонa через Rewrite.
        ts_input_text = voiceover_editor_text
        preset_n = normalize_rewrite_preset(preset)
        if preset_n == REWRITE_PRESET_PREWRITTEN:
            rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            ts_input_text = rw_res if rw_res.strip() else str(
                (stages_snap.get("inbox") or {}).get("last_result") or ""
            )
            if not ts_input_text.strip():
                return None, "Сначала вставьте готовый текст в Inbox (Result) или прогоните Rewrite."
        elif preset_n == REWRITE_PRESET_SOFT:
            rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            ts_input_text = rw_res if rw_res.strip() else str(source_text or "")
            if not ts_input_text.strip():
                return None, "Сначала вставьте текст в Source или прогоните Rewrite."
        user_text = build_title_strategist_user_message(
            str(cell.get("user_prompt") or ""),
            ts_input_text,
            original_title=original_title,
        )
    elif stage_key == "structure_splitter":
        prompt = build_structure_splitter_system_prompt(
            str(cell.get("prompt") or ""),
        )
        # В пресете «Я уже ЗАrewriteИЛ» Structure Splitter берёт исходный текст
        # из Rewrite.Result (если есть) или из Inbox.Result.
        ss_input_text = voiceover_editor_text
        preset_n = normalize_rewrite_preset(preset)
        if preset_n == REWRITE_PRESET_PREWRITTEN:
            rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            ss_input_text = rw_res if rw_res.strip() else str(
                (stages_snap.get("inbox") or {}).get("last_result") or ""
            )
            if not ss_input_text.strip():
                return None, "Сначала вставьте готовый текст в Inbox (Result) или прогоните Rewrite."
        elif preset_n == REWRITE_PRESET_SOFT:
            rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            ss_input_text = rw_res if rw_res.strip() else str(source_text or "")
            if not ss_input_text.strip():
                return None, "Сначала вставьте текст в Source или прогоните Rewrite."
        user_text = build_structure_splitter_user_message(
            str(cell.get("user_prompt") or ""),
            ss_input_text,
        )
    elif stage_key == "scene_writer":
        prompt = (str(cell.get("prompt") or "") or "").strip()
        style_prompt = str(cell.get("style_prompt") or "").strip()
        up = str(cell.get("user_prompt") or "").strip()
        payload = {
            "scene_writer_user_promt": up,
            "style_promt": style_prompt,
        }
        user_text = _json_user_message(payload)
    elif stage_key == "youtube_packaging":
        ts = (title_strategist_result_text or "").strip()
        if not ts:
            return None, "Нет результата Title Strategist — выполните этап Title Strategist и сохраните проект."
        prompt = build_youtube_packaging_system_prompt(str(cell.get("prompt") or ""))
        user_text = build_youtube_packaging_user_message(
            str(cell.get("user_prompt") or ""),
            ts,
        )
    elif stage_key == "scene_writer_live":
        prompt = (str(cell.get("prompt") or "") or "").strip()
        up = str(cell.get("user_prompt") or "").strip()
        sw = str(scene_writer_result_text or "").strip()
        if not sw:
            return None, "Нет результата Scene Writer — выполните этап Scene Writer и сохраните проект."
        user_text = _json_user_message(
            {
                "scene_writer_live_user_promt": up,
                "scene_writer_result": sw,
            }
        )
    else:
        prompt = build_rewrite_system_prompt(
            master_prompt,
            str(cell.get("prompt") or ""),
            source_text,
        )
        user_text = build_stage_user_message(
            source_text,
            stage_key,
            stages_snap,
            hero_prompt=hero_prompt,
        )
    prompt = (prompt or "").strip()
    user_text = (user_text or "").strip()
    if stage_key not in (
        "retention_editor",
        "hook_editor",
        "flow_editor",
        "persona_editor",
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "scene_writer_live",
        "youtube_packaging",
        "rewrite",
    ):
        dur_payload = build_duration_length_spec_payload(
            target_chars=target_chars,
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


def snapshot_stages_from_body(body: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Из тела запроса run: source_text и stages с дефолтами."""
    source_text = str(body.get("source_text") or "")
    raw = body.get("stages")
    stages: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        raw = {}
    for key in REWRITE_STAGE_KEYS:
        cell = raw.get(key)
        if not isinstance(cell, dict):
            cell = {}
        stages[key] = {
            "prompt": str(cell.get("prompt") or ""),
            "user_prompt": str(cell.get("user_prompt") or ""),
            "style_prompt": str(cell.get("style_prompt") or ""),
            "past_prompt": str(cell.get("past_prompt") or ""),
            "scene_writer_check": cell.get("scene_writer_check") if isinstance(cell.get("scene_writer_check"), dict) else None,
            "scene_writer_live_check": cell.get("scene_writer_live_check") if isinstance(cell.get("scene_writer_live_check"), dict) else None,
            "structure_splitter_check": cell.get("structure_splitter_check") if isinstance(cell.get("structure_splitter_check"), dict) else None,
            "block_writer_check": cell.get("block_writer_check") if isinstance(cell.get("block_writer_check"), dict) else None,
            "model": normalize_rewrite_model(str(cell.get("model") or "")),
            "last_result": str(cell.get("last_result") or ""),
            "style_prompt_locked": bool(cell.get("style_prompt_locked")),
            "past_prompt_locked": bool(cell.get("past_prompt_locked")),
        }
    return source_text, stages


def snapshot_original_title_from_body(body: dict[str, Any], job: dict[str, Any]) -> str:
    """Поле «Исходное название» для user этапа title_strategist: снимок с формы или из project.json."""
    if isinstance(body, dict) and "source_title" in body:
        return str(body.get("source_title") or "").strip()
    return str((job or {}).get("source_title") or "").strip()


def snapshot_master_prompt_from_body(body: dict[str, Any]) -> str:
    return str(body.get("master_prompt") or "")


def snapshot_rewrite_preset_from_body(body: dict[str, Any], job: dict[str, Any] | None = None) -> str:
    """Снимок текущего пресета: берём из body (если передан), иначе из job, иначе дефолт."""
    if isinstance(body, dict) and "rewrite_preset" in body:
        return normalize_rewrite_preset(body.get("rewrite_preset"))
    return normalize_rewrite_preset((job or {}).get("rewrite_preset"))


def snapshot_pipeline_extras_from_body(body: dict[str, Any]) -> tuple[str, int, int, int]:
    """hero_prompt, target_chars (500–40 000), duration_minutes, chars_per_minute."""
    hero = str(body.get("hero_prompt") or "")
    try:
        dm = int(body.get("duration_minutes", 5))
        dm = max(1, min(30, dm))
    except (TypeError, ValueError):
        dm = 5
    try:
        cpm = int(body.get("chars_per_minute", 344))
        cpm = max(1, min(2000, cpm))
    except (TypeError, ValueError):
        cpm = 344
    if "target_chars" in body and body.get("target_chars") is not None and str(body.get("target_chars", "")).strip() != "":
        try:
            return hero, clamp_target_chars(int(body["target_chars"])), dm, cpm
        except (TypeError, ValueError):
            pass
    return hero, clamp_target_chars(dm * cpm), dm, cpm
