"""
ReWrite Master — цепочка этапов: Analysis → Architect → Block Writer → редакторы
(Retention → Hook → Flow → Persona → Voiceover → Title Strategist / Structure Splitter → Scene Writer).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rewrite_openai import (
    REWRITE_CHAT_TEMPERATURE,
    REWRITE_DEFAULT_MODEL,
    clamp_chat_temperature,
    normalize_rewrite_model,
    openai_chat_completions_request_dict,
)

from locked_prompts import get_locked_prompt

from prompt_placeholders import apply_prompt_placeholders

TARGET_CHARS_MIN = 500
TARGET_CHARS_MAX = 40_000
TARGET_CHARS_STEP = 500


def _rewrite_system_rules_text(cell: dict[str, Any]) -> str:
    """Доп. system-текст для этапа rewrite: сначала locked_prompts, иначе legacy из cell."""
    try:
        locked = str(get_locked_prompt("rewrite_system_rules") or "").strip()
    except KeyError:
        locked = ""
    if locked:
        return locked
    return str((cell or {}).get("rewrite_system_rules") or "").strip()


def _stage_user_prompt_text(stage_key: str, cell: dict[str, Any]) -> str:
    """User Promt этапа: сначала locked `user_prompt_<stage_key>`, иначе legacy из cell."""
    name = f"user_prompt_{stage_key}"
    try:
        locked = str(get_locked_prompt(name) or "").strip()
    except KeyError:
        locked = ""
    if locked:
        return locked
    return str((cell or {}).get("user_prompt") or "").strip()


def _stage_system_prompt_text(stage_key: str, cell: dict[str, Any]) -> str:
    """System Promt этапа: locked `system_prompt_<stage_key>`, иначе legacy из cell.prompt."""
    name = f"system_prompt_{stage_key}"
    try:
        locked = str(get_locked_prompt(name) or "").strip()
    except KeyError:
        locked = ""
    if locked:
        return locked
    return str((cell or {}).get("prompt") or "").strip()


def _voiceover_editor_system_rules_text(cell: dict[str, Any]) -> str:
    """System Rules для Voiceover Editor: locked_prompts, иначе legacy из cell."""
    try:
        locked = str(get_locked_prompt("voiceover_editor_system_rules") or "").strip()
    except KeyError:
        locked = ""
    if locked:
        return locked
    return str((cell or {}).get("voiceover_system_rules") or "").strip()


def _editor_stage_system_rules_text(stage_key: str, cell: dict[str, Any]) -> str:
    """Locked ``{stage}_system_rules`` или legacy из ячейки этапа."""
    name = f"{stage_key}_system_rules"
    try:
        locked = str(get_locked_prompt(name) or "").strip()
    except KeyError:
        locked = ""
    if locked:
        return locked
    return str((cell or {}).get(name) or "").strip()


def build_editor_stage_user_json(
    user_prompt_plain: str,
    edited_text_upstream: str,
    *,
    hero_plain: str | None = None,
) -> str:
    """User для редакторов конвейера: JSON {user_promt, edited_text} (+ hero_promt для Persona)."""
    up = (user_prompt_plain or "").strip()
    et = _extract_edited_text(str(edited_text_upstream or ""))
    payload: dict[str, Any] = {"user_promt": up, "edited_text": et}
    if hero_plain is not None and str(hero_plain).strip():
        payload["hero_promt"] = str(hero_plain).strip()
    return json.dumps(payload, ensure_ascii=False, indent=2)


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


def _join_user_sections(*parts: str) -> str:
    """Склеивает непустые куски в одно user-сообщение (OpenAI: content — одна строка)."""
    xs = [str(p).strip() for p in parts if p is not None and str(p).strip()]
    return "\n\n".join(xs)


def _format_duration_user_preamble(dur_payload: dict[str, Any]) -> str:
    """Краткий текстовый блок с ориентиром длины (раньше вкладывали JSON length_spec в user)."""
    if not isinstance(dur_payload, dict):
        return ""
    spec = dur_payload.get("length_spec")
    if not isinstance(spec, dict):
        return ""
    try:
        t = int(spec.get("target_chars_ideal") or 0)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    return (
        f"Ориентир длины озвучки: примерно {t} символов "
        f"(ориентир по длительности; придерживайся по возможности)."
    )


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
            if isinstance(obj.get("text"), str) and str(obj.get("text") or "").strip():
                return _normalize_edited_text(str(obj.get("text") or ""))
            return _normalize_edited_text(str(obj.get("edited_text") or ""))
    except json.JSONDecodeError:
        pass
    return _normalize_edited_text(raw)


def parse_voiceover_editor_payload(raw_payload: str) -> tuple[str, list[Any]]:
    """Разбор ответа Voiceover Editor: plain text или JSON {text, changes}."""
    raw = str(raw_payload or "").strip()
    if not raw:
        return "", []
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            text = ""
            if isinstance(obj.get("text"), str):
                text = _normalize_edited_text(obj.get("text") or "")
            elif isinstance(obj.get("edited_text"), str):
                text = _normalize_edited_text(obj.get("edited_text") or "")
            changes = obj.get("changes")
            if not isinstance(changes, list):
                changes = []
            return text, changes
    except json.JSONDecodeError:
        pass
    return _normalize_edited_text(raw), []


def build_rewrite_system_prompt(
    master_prompt: str,
    stage_prompt: str,
) -> str:
    """System-сообщение для этапа: Master → промпт этапа (Duration и исходник — в user)."""
    parts: list[str] = []
    m = (master_prompt or "").strip()
    if m:
        parts.append(m)
    p = (stage_prompt or "").strip()
    if p:
        parts.append(p)
    return "\n\n".join(parts)


def build_structure_user_message(analysis_last_result: str, structure_user_prompt: str) -> str:
    """User для этапа Architect: User Promt и результат Analysis (plain text)."""
    ar = (analysis_last_result or "").strip() or "(пусто)"
    up = (structure_user_prompt or "").strip()
    return build_rewrite_stage_user_message(up, ar)


def build_analysis_user_message(source_text: str, analysis_user_prompt: str) -> str:
    """User для этапа Analysis: User Promt + исходный текст (plain text)."""
    up = (analysis_user_prompt or "").strip()
    body = (source_text or "").strip() or "(пусто)"
    return build_rewrite_stage_user_message(up, body)


def build_draft1_rewriter_user_message(
    analysis_last_result: str,
    structure_last_result: str,
    draft1_user_prompt: str,
) -> str:
    """User для draft1 (диагностическое compose-тело до blockwise): User Promt, Analysis, Architect — plain."""
    ar = (analysis_last_result or "").strip() or "(пусто)"
    sr = (structure_last_result or "").strip() or "(пусто)"
    up = (draft1_user_prompt or "").strip()
    return _join_user_sections(up, ar, sr)


def build_retention_editor_system_prompt(
    retention_editor_prompt: str,
    retention_rules: str = "",
) -> str:
    """Retention Editor: system promt + необязательный блок system rules."""
    return build_voiceover_editor_system_prompt(retention_editor_prompt, retention_rules)


def build_retention_editor_user_message(
    retention_editor_user_prompt: str,
    block_writer_full_text: str = "",
) -> str:
    """Legacy-обёртка: JSON как у Voiceover."""
    return build_editor_stage_user_json(retention_editor_user_prompt, block_writer_full_text)


def build_hook_editor_system_prompt(
    hook_editor_prompt: str,
    hook_rules: str = "",
) -> str:
    return build_voiceover_editor_system_prompt(hook_editor_prompt, hook_rules)


def build_hook_editor_user_message(
    hook_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    return build_editor_stage_user_json(hook_editor_user_prompt, edited_text)


def build_flow_editor_system_prompt(
    flow_editor_prompt: str,
    flow_rules: str = "",
) -> str:
    return build_voiceover_editor_system_prompt(flow_editor_prompt, flow_rules)


def build_flow_editor_user_message(
    flow_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    return build_editor_stage_user_json(flow_editor_user_prompt, edited_text)


def build_persona_editor_system_prompt(
    persona_editor_prompt: str,
    persona_rules: str = "",
) -> str:
    return build_voiceover_editor_system_prompt(persona_editor_prompt, persona_rules)


def build_persona_editor_user_message(
    persona_editor_user_prompt: str,
    hero_prompt: str = "",
    edited_text: str = "",
) -> str:
    return build_editor_stage_user_json(
        persona_editor_user_prompt,
        edited_text,
        hero_plain=hero_prompt,
    )


def build_rewrite_stage_system_prompt(
    rewrite_prompt: str,
    rewrite_system_rules: str = "",
) -> str:
    """Этап rewrite: в system — Rewrite System Promt + необязательный блок System Rules."""
    rp = (rewrite_prompt or "").strip()
    rs = (rewrite_system_rules or "").strip()
    if rp and rs:
        return f"{rp}\n\n{rs}"
    return rp or rs


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
    voiceover_system_rules: str = "",
) -> str:
    """Этап voiceover_editor: System Promt + необязательный блок System Rules."""
    vp = (voiceover_editor_prompt or "").strip()
    vr = (voiceover_system_rules or "").strip()
    if vp and vr:
        return f"{vp}\n\n{vr}"
    return vp or vr


def build_voiceover_editor_user_message(
    voiceover_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    """User для voiceover_editor: JSON {user_promt, edited_text}."""
    return build_editor_stage_user_json(voiceover_editor_user_prompt, edited_text)


_ELEVENLABS_INSERT_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_ELEVENLABS_INSERT_XML_RE = re.compile(r"<[^>]*/>", re.IGNORECASE)


def count_elevenlabs_inserts(text: str) -> int:
    """Теги voice-direction: [whispers] и самозакрывающиеся <break … />."""
    s = str(text or "")
    return len(_ELEVENLABS_INSERT_BRACKET_RE.findall(s)) + len(
        _ELEVENLABS_INSERT_XML_RE.findall(s)
    )


def strip_elevenlabs_inserts(text: str) -> str:
    s = str(text or "")
    s = _ELEVENLABS_INSERT_BRACKET_RE.sub("", s)
    return _ELEVENLABS_INSERT_XML_RE.sub("", s)


def downstream_script_input_text(
    preset: str,
    stages_snap: dict[str, Any],
    *,
    voiceover_editor_text: str = "",
    source_text: str = "",
) -> str:
    """Plain-текст для downstream-этапов: Voiceover (если есть) → Inbox / Source."""
    vo = str(voiceover_editor_text or "").strip()
    if vo:
        return vo
    preset_n = normalize_rewrite_preset(preset)
    if preset_n == REWRITE_PRESET_PREWRITTEN:
        ibx = str((stages_snap.get("inbox") or {}).get("last_result") or "").strip()
        if ibx:
            return _extract_edited_text(ibx)
        return ""
    if preset_n == REWRITE_PRESET_SOFT:
        rw_res = str((stages_snap.get("rewrite") or {}).get("last_result") or "").strip()
        if rw_res:
            return _extract_edited_text(rw_res)
        return ""
    return ""


def _elevenlabs_check_norm_text(text: str) -> str:
    """Схлопывает пробелы: после вырезания тегов модель часто оставляет \\n/пробелы на их месте."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def build_elevenlabs_editor_system_prompt(
    elevenlabs_editor_prompt: str,
) -> str:
    return (elevenlabs_editor_prompt or "").strip()


def build_elevenlabs_editor_user_message(
    elevenlabs_editor_user_prompt: str,
    edited_text: str = "",
) -> str:
    """User для elevenlabs_editor: User Promt + plain text из Voiceover Editor Result."""
    up = (elevenlabs_editor_user_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    return _join_user_sections(up, et)


def build_elevenlabs_editor_check(input_text: str, editor_result_text: str) -> dict[str, Any]:
    """Проверка: IN = Voiceover plain; OUT = Result без insert-тегов.

    OK — совпадение текста после схлопывания пробелов (модель вставляет \\n/пробелы
    вокруг тегов; в сырой дельте это даёт +N при insert_count = числу тегов, не символов).
    """
    inp = str(input_text or "")
    raw_out = str(editor_result_text or "")
    out_stripped = strip_elevenlabs_inserts(raw_out)
    inp_norm = _elevenlabs_check_norm_text(inp)
    out_norm = _elevenlabs_check_norm_text(out_stripped)
    input_chars = len(inp)
    output_chars = len(out_stripped)
    delta_chars = output_chars - input_chars
    input_chars_norm = len(inp_norm)
    output_chars_norm = len(out_norm)
    delta_chars_norm = output_chars_norm - input_chars_norm
    insert_count = count_elevenlabs_inserts(raw_out)
    ok = bool(inp_norm) and inp_norm == out_norm
    return {
        "type": "elevenlabs_editor_check",
        "summary": {
            "input_chars": input_chars,
            "output_chars": output_chars,
            "delta_chars": delta_chars,
            "input_chars_norm": input_chars_norm,
            "output_chars_norm": output_chars_norm,
            "delta_chars_norm": delta_chars_norm,
            "insert_count": insert_count,
            "ok": ok,
        },
    }


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
    """User для title_strategist: исходное название, User Promt, текст после Voiceover (plain text).

    Плейсхолдер {{ORIGINAL_TITLE}} в user-promt подставляется здесь; дополнительно см.
    ``apply_title_strategist_original_title_to_user_json`` после compose.
    """
    up = (title_strategist_user_prompt or "").strip()
    et = _extract_edited_text(edited_text)
    tit = (original_title or "").strip()
    repl = tit if tit else "(пусто)"
    up = up.replace("{{ORIGINAL_TITLE}}", repl).replace("{{ ORIGINAL_TITLE }}", repl)
    head = f"Исходное название видео (YouTube): {repl}"
    return _join_user_sections(head, up, et)


def apply_title_strategist_original_title_to_user_json(user_text: str, original_title: str) -> str:
    """Подставляет {{ORIGINAL_TITLE}} и синхронизирует первую строку про исходное название (plain text).

    Имя функции историческое (раньше user был JSON). Вызывается из app после compose.
    """
    val_raw = (original_title or "").strip()
    val_disp = val_raw if val_raw else "(пусто)"
    s = str(user_text or "")
    s = s.replace("{{ORIGINAL_TITLE}}", val_disp).replace("{{ ORIGINAL_TITLE }}", val_disp)
    head = f"Исходное название видео (YouTube): {val_disp}"
    marker = "Исходное название видео (YouTube):"
    t = s.lstrip("\ufeff")
    if t.startswith(marker):
        nl = s.find("\n")
        if nl == -1:
            return head
        return (head + s[nl:]).strip()
    return s


def build_structure_splitter_system_prompt(
    structure_splitter_prompt: str,
) -> str:
    """Этап structure_splitter: в system только Structure Splitter System Promt."""
    return (structure_splitter_prompt or "").strip()


def build_structure_splitter_user_message(
    structure_splitter_user_prompt: str,
    voiceover_full_text: str = "",
) -> str:
    """User для structure_splitter: User Promt + полный текст озвучки (plain text)."""
    up = (structure_splitter_user_prompt or "").strip()
    raw = str(voiceover_full_text or "").strip()
    return build_rewrite_stage_user_message(up, raw)


def build_youtube_packaging_system_prompt(packaging_prompt: str) -> str:
    """Этап youtube_packaging: в system только YouTube packaging System Promt."""
    return (packaging_prompt or "").strip()


def build_youtube_packaging_user_message(
    user_prompt: str,
    title_strategist_result: str = "",
) -> str:
    """User: YouTube packaging User Promt + результат Title Strategist (plain text)."""
    up = (user_prompt or "").strip()
    raw = str(title_strategist_result or "").strip()
    return build_rewrite_stage_user_message(up, raw or "(пусто)")


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
    ("elevenlabs_editor", "ElevenLabs Editor"),
    ("title_strategist", "Title Strategist"),
    ("structure_splitter", "Structure Splitter"),
    ("scene_writer", "Scene Writer"),
    ("youtube_packaging", "YouTube packaging engine"),
]

REWRITE_STAGE_KEYS: frozenset[str] = frozenset(k for k, _ in REWRITE_STAGES)

# Совпадает с `rewrite-stage-card--no-index` в шаблоне: у карточки скрыт бейдж
# номера этапа (CSS). YouTube packaging может входить в «сворачиваемую линейку»
# вместе с Rewrite…Structure Splitter, но в подпись «Этапы 1–N» его не считают —
# иначе N не совпадает с видимыми номерами (Мягкий / prewritten: 4 vs 5).
REWRITE_STAGE_CARD_NO_INDEX_KEYS: frozenset[str] = frozenset(
    ("scene_writer", "youtube_packaging")
)

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
#
# Scene Writer и YouTube packaging — отдельные модули: всегда на странице, не входят
# в списки пресетов (см. REWRITE_STAGE_KEYS_ALWAYS_VISIBLE).
REWRITE_STAGE_KEYS_ALWAYS_VISIBLE: frozenset[str] = frozenset(
    {"scene_writer", "youtube_packaging"}
)
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
        "elevenlabs_editor",
        "title_strategist",
        "structure_splitter",
    ],
    # «Я уже ЗАrewriteИЛ»: Inbox → Voiceover Editor (из Inbox) → ElevenLabs → Title /
    # Structure → … Без этапа Rewrite.
    REWRITE_PRESET_PREWRITTEN: [
        "inbox",
        "voiceover_editor",
        "elevenlabs_editor",
        "title_strategist",
        "structure_splitter",
    ],
    # «Мягкий Rewrite» — как prewritten, но без Inbox: исходник в поле Source.
    REWRITE_PRESET_SOFT: [
        "rewrite",
        "voiceover_editor",
        "elevenlabs_editor",
        "title_strategist",
        "structure_splitter",
    ],
}


def normalize_rewrite_preset(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in REWRITE_PRESET_KEYS:
        return v
    return REWRITE_PRESET_DEFAULT


def normalize_rewrite_pipeline_language(value: Any) -> str:
    """Язык конвейера (UI): ru | en | es | ja. Значение хранится в project.json как `rewrite_pipeline_language`."""
    v = str(value or "").strip().lower()
    if v in ("en", "english", "англ"):
        return "en"
    if v in ("es", "spa", "spanish", "espanol", "español"):
        return "es"
    if v in ("ja", "jp", "japanese"):
        return "ja"
    if v in ("ru", "en", "es", "ja"):
        return v
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
        "Отправляем. В System: только Analysis System Promt (общий Master — через плейсхолдер "
        "{{MASTER_PROMT}}, если нужен). В User: Analysis User Promt и текст из поля Source (верх страницы)."
    ),
    "structure": (
        "Отправляем. В System: только Architect System Promt (Master — через {{MASTER_PROMT}}, если нужен). "
        "В User: Architect User Promt и результат Analysis (plain text / analysis.json)."
    ),
    "draft1": (
        "Поблочно. В System: только Block Writer System Promt (при нужде Master/Hero через "
        "{{MASTER_PROMT}}/{{HERO_PROMT}} в тексте промпта). "
        "В JSON каждого POST-user: block_writer_user_promt, architect_block, short_summary_context "
        "(без отдельного поля Hero). В compose до цикла (образец J): User Promt, Analysis Result, Structure Result "
        "; в начале User может быть текст ориентира длины (Duration)."
    ),
    "retention_editor": (
        "Отправляем. В System: Retention Editor System Promt + необязательный блок System Rules (под пин-кодом). "
        "В User — один JSON: user_promt (из locked User Promt), edited_text (текст предыдущего этапа — Block Writer)."
    ),
    "hook_editor": (
        "Отправляем. В System: Hook Editor System Promt + System Rules (locked). "
        "В User — JSON: user_promt, edited_text (результат Retention Editor)."
    ),
    "flow_editor": (
        "Отправляем. В System: Flow Editor System Promt + System Rules (locked). "
        "В User — JSON: user_promt, edited_text (результат Hook Editor)."
    ),
    "persona_editor": (
        "Отправляем. В System: Persona Editor System Promt + System Rules (locked). "
        "В User — JSON: user_promt, hero_promt (Hero после плейсхолдеров), edited_text (результат Flow Editor)."
    ),
    "voiceover_editor": (
        "Отправляем. В System: Voiceover Editor System Promt + System Rules (locked). "
        "В User — JSON: user_promt, edited_text (из Persona / Inbox / Rewrite в зависимости от пресета). "
        "Ответ модели: JSON с text или edited_text и массивом changes."
    ),
    "elevenlabs_editor": (
        "Отправляем. В System: ElevenLabs Editor System Promt. "
        "В User (по порядку): ElevenLabs Editor User Promt, edited_text из Voiceover Editor."
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
    "youtube_packaging": (
        "Отправляем один POST. В System: YouTube packaging System Promt. "
        "В User: YouTube packaging User Promt и поле title_strategist_result (Result этапа Title Strategist). "
        "В превью блок заголовков — только top_5_titles. Плюс final_description, hashtags, thumbnail_options по схеме в System Promt."
    ),
}

# Краткие подзаголовки под названием этапа (мутный серый под заголовком в карточке).
REWRITE_STAGE_SUBTITLES: dict[str, str] = {
    "inbox": "Готовый текст: вставьте сюда — дальше пойдёт Rewrite → Voiceover Editor → Title Strategist → Structure Splitter",
    "rewrite": "Агент-писатель текста (Inbox → Rewrite)",
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
        "scene_writer_check": None,
        "structure_splitter_check": None,
        "elevenlabs_editor_check": None,
        "block_writer_check": None,
        "model": REWRITE_DEFAULT_MODEL,
        "last_result": "",
        "prompt_locked": False,
        "user_prompt_locked": False,
        "style_prompt_locked": False,
        "past_prompt_locked": True,
        "rewrite_system_rules": "",
        "rewrite_system_rules_locked": True,
        "voiceover_changes": "",
        "retention_editor_changes": "",
        "hook_editor_changes": "",
        "flow_editor_changes": "",
        "persona_editor_changes": "",
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
    job.setdefault("voiceover_final_semantic_text_analysis", "")
    job["voiceover_final_semantic_text_analysis"] = str(
        job.get("voiceover_final_semantic_text_analysis") or ""
    )
    job.setdefault("voiceover_final_semantic_text_analysis_locked", True)
    job["voiceover_final_semantic_text_analysis_locked"] = bool(
        job.get("voiceover_final_semantic_text_analysis_locked", True)
    )
    job.setdefault("voiceover_final_semantic_text_analysis_at", "")
    job["voiceover_final_semantic_text_analysis_at"] = str(
        job.get("voiceover_final_semantic_text_analysis_at") or ""
    )
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

    # Миграция: убран этап scene_writer_live — старые данные выкидываем.
    if "scene_media_planner" in stages:
        stages.pop("scene_media_planner", None)
    stages.pop("scene_writer_live", None)

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
        e.setdefault("rewrite_system_rules", "")
        e.setdefault("rewrite_system_rules_locked", True)
        e.setdefault("voiceover_changes", "")
        e.setdefault("retention_editor_changes", "")
        e.setdefault("hook_editor_changes", "")
        e.setdefault("flow_editor_changes", "")
        e.setdefault("persona_editor_changes", "")
        e["model"] = normalize_rewrite_model(str(e.get("model", "")))
        e["prompt_locked"] = bool(e.get("prompt_locked"))
        e["user_prompt_locked"] = bool(e.get("user_prompt_locked"))
        e["style_prompt_locked"] = bool(e.get("style_prompt_locked"))
        e["past_prompt_locked"] = bool(e.get("past_prompt_locked"))
        e["rewrite_system_rules"] = str(e.get("rewrite_system_rules") or "")
        e["rewrite_system_rules_locked"] = bool(e.get("rewrite_system_rules_locked"))
        # UX rule: Past in Promt should be collapsed on page load.
        if key == "scene_writer":
            e["past_prompt_locked"] = True
        # Rewrite: User Promt всегда в режиме правки при загрузке (пресеты с ручным
        # текстом в user); закрыть можно кнопкой ✎ — тогда уйдёт в project.json.
        if key == "rewrite":
            e["user_prompt_locked"] = False
        if not isinstance(e.get("scene_writer_check"), dict):
            e["scene_writer_check"] = None
        e.pop("animation_settings", None)
        e.pop("scene_media_check", None)
        e.pop("scene_writer_live_check", None)
        if not isinstance(e.get("structure_splitter_check"), dict):
            e["structure_splitter_check"] = None
        if not isinstance(e.get("elevenlabs_editor_check"), dict):
            e["elevenlabs_editor_check"] = None
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

    job.setdefault("chat_temperature", REWRITE_CHAT_TEMPERATURE)
    job["chat_temperature"] = clamp_chat_temperature(job.get("chat_temperature"))

    job.setdefault("source_title", "")
    job["source_title"] = str(job.get("source_title") or "")

    # Итоговый текст в UI = тот же edited_text, что в Result Voiceover Editor
    # (после нормализации JSON). Убираем «осиротевший» voiceover_final_text в JSON.
    voe_final = stages.get("voiceover_editor") if isinstance(stages.get("voiceover_editor"), dict) else None
    if isinstance(voe_final, dict):
        job["voiceover_final_text"] = _extract_edited_text(str(voe_final.get("last_result") or ""))

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
        if "structure_splitter_check" in sv:
            v = sv.get("structure_splitter_check")
            e["structure_splitter_check"] = v if isinstance(v, dict) else None
        if "elevenlabs_editor_check" in sv:
            v = sv.get("elevenlabs_editor_check")
            e["elevenlabs_editor_check"] = v if isinstance(v, dict) else None
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
        if "rewrite_system_rules" in sv:
            e["rewrite_system_rules"] = str(sv.get("rewrite_system_rules") or "")
        rules_locked_in_body = (
            sv.get("rewrite_system_rules_locked")
            if "rewrite_system_rules_locked" in sv
            else None
        )
        if rules_locked_in_body is not None:
            e["rewrite_system_rules_locked"] = bool(rules_locked_in_body)
        if "model" in sv:
            e["model"] = normalize_rewrite_model(str(sv.get("model") or ""))
        if "last_result" in sv:
            e["last_result"] = str(sv.get("last_result") or "")
        if sk == "voiceover_editor" and "voiceover_changes" in sv:
            e["voiceover_changes"] = str(sv.get("voiceover_changes") or "")
        for _ek in ("retention_editor", "hook_editor", "flow_editor", "persona_editor"):
            ck = f"{_ek}_changes"
            if sk == _ek and ck in sv:
                e[ck] = str(sv.get(ck) or "")


_STAGE_LABEL_BY_KEY: dict[str, str] = {k: lbl for k, lbl in REWRITE_STAGES}


def _stage_last_result(stages: dict[str, Any], stage_key: str) -> str:
    cell = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
    return str((cell or {}).get("last_result") or "").strip()


def _voiceover_plain_from_stages(stages: dict[str, Any]) -> str:
    raw = _stage_last_result(stages, "voiceover_editor")
    if not raw:
        return ""
    return _extract_edited_text(raw).strip() or raw.strip()


def _missing_stage_result_message(source_stage_key: str) -> str:
    if source_stage_key == "inbox":
        return "Сначала вставьте готовый текст в Inbox (Result)."
    label = _STAGE_LABEL_BY_KEY.get(source_stage_key, source_stage_key)
    return f"Сначала выполните этап «{label}» — нет сохранённого результата."


def _validate_stage_input_sources(
    stage_key: str,
    preset: str,
    stages: dict[str, Any],
    *,
    source_text: str = "",
) -> str | None:
    """Проверка фактических источников данных этапа (как в compose + подписи UI).

    None — входные данные есть; иначе текст для пользователя.
    """
    preset_n = normalize_rewrite_preset(preset)
    st = stages if isinstance(stages, dict) else {}

    def need_result(source_key: str) -> str | None:
        if _stage_last_result(st, source_key):
            return None
        return _missing_stage_result_message(source_key)

    if stage_key == "analysis":
        if not str(source_text or "").strip():
            return "Сначала вставьте исходный текст в поле Source (верх страницы)."
        return None

    if stage_key == "structure":
        return need_result("analysis")

    if stage_key == "draft1":
        err = need_result("analysis")
        if err:
            return err
        return need_result("structure")

    if stage_key == "retention_editor":
        return need_result("draft1")

    if stage_key == "hook_editor":
        return need_result("retention_editor")

    if stage_key == "flow_editor":
        return need_result("hook_editor")

    if stage_key == "persona_editor":
        return need_result("flow_editor")

    if stage_key == "rewrite":
        if preset_n == REWRITE_PRESET_SOFT:
            if not str(source_text or "").strip():
                return "Сначала вставьте исходный текст в поле Source (верх страницы)."
            return None
        if preset_n == REWRITE_PRESET_PREWRITTEN:
            return need_result("inbox")
        return "Этап Rewrite недоступен в этом пресете."

    if stage_key == "voiceover_editor":
        if preset_n == REWRITE_PRESET_PREWRITTEN:
            return need_result("inbox")
        if preset_n == REWRITE_PRESET_SOFT:
            return need_result("rewrite")
        return need_result("persona_editor")

    if stage_key in ("elevenlabs_editor", "title_strategist", "structure_splitter"):
        if not _voiceover_plain_from_stages(st):
            return _missing_stage_result_message("voiceover_editor")
        return None

    if stage_key == "scene_writer":
        return need_result("structure_splitter")

    if stage_key == "youtube_packaging":
        return need_result("title_strategist")

    return None


def validate_prerequisites(
    stage_key: str,
    stages: dict[str, Any],
    *,
    preset: str = REWRITE_PRESET_DEFAULT,
    source_text: str = "",
) -> str | None:
    """None если ок, иначе текст ошибки для пользователя.

    Проверяются **источники данных** этапа (поле Source, Result предков по подписи
    «данные берутся из …»), а не произвольная цепочка «любой предыдущий в пресете».
    """
    if stage_key not in REWRITE_STAGE_KEYS:
        return "Неизвестный этап."
    if stage_key == "inbox":
        return None
    preset = normalize_rewrite_preset(preset)
    preset_order = REWRITE_PRESET_STAGE_KEYS.get(preset, [])
    if stage_key in REWRITE_STAGE_KEYS_ALWAYS_VISIBLE:
        return _validate_stage_input_sources(
            stage_key, preset, stages, source_text=source_text
        )
    if stage_key not in preset_order:
        return None
    return _validate_stage_input_sources(
        stage_key, preset, stages, source_text=source_text
    )


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
    elevenlabs_editor_text: str = "",
    structure_splitter_text: str = "",
    title_strategist_result_text: str = "",
    original_title: str = "",
    preset: str = REWRITE_PRESET_DEFAULT,
    pipeline_language: str = "ru",
    chat_temperature: float | None = None,
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
        "elevenlabs_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "youtube_packaging",
        "rewrite",
    ) and not (source_text or "").strip():
        return None, "Введите исходный текст в верхнем поле."
    pre_err = validate_prerequisites(stage_key, stages_snap, preset=preset, source_text=source_text)
    if pre_err:
        return None, pre_err
    cell = stages_snap.get(stage_key) or {}
    up_txt = _stage_user_prompt_text(stage_key, cell)
    model = normalize_rewrite_model(str(cell.get("model") or ""))
    master_raw = str(master_prompt or "")
    hero_raw = str(hero_prompt or "")
    ot = str(original_title or "").strip()

    def _ph_kw(nested: bool) -> dict[str, Any]:
        return {
            "language": pipeline_language,
            "duration_minutes": duration_minutes,
            "chars_per_minute": chars_per_minute,
            "target_chars": target_chars,
            "original_title": ot,
            "master_prompt": master_raw,
            "hero_prompt": hero_raw,
            "allow_nested_master_hero": nested,
        }

    master_use = apply_prompt_placeholders(master_raw, **_ph_kw(False))
    hero_use = apply_prompt_placeholders(hero_raw, **_ph_kw(False))

    def subp(t: str) -> str:
        return apply_prompt_placeholders(t, **_ph_kw(True))

    up_txt = subp(up_txt)
    rules_resolved = subp(_rewrite_system_rules_text(cell))
    stage_prompt_t = subp(_stage_system_prompt_text(stage_key, cell))
    if stage_key == "structure":
        analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
        # Master не дублируем в system — при необходимости {{MASTER_PROMT}} в locked Architect System Promt.
        prompt = (stage_prompt_t or "").strip()
        user_text = build_structure_user_message(
            analysis_res,
            up_txt,
        )
    elif stage_key == "analysis":
        # Master не дублируем в system — при необходимости вставьте {{MASTER_PROMT}} в locked System Promt.
        prompt = (stage_prompt_t or "").strip()
        user_text = build_analysis_user_message(
            source_text,
            up_txt,
        )
    elif stage_key == "draft1":
        analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
        structure_res = str((stages_snap.get("structure") or {}).get("last_result") or "")
        # Master не в system — при необходимости {{MASTER_PROMT}} в locked Block Writer System Promt.
        prompt = (stage_prompt_t or "").strip()
        user_text = build_draft1_rewriter_user_message(
            analysis_res,
            structure_res,
            up_txt,
        )
    elif stage_key == "retention_editor":
        re_sys = subp(_stage_system_prompt_text("retention_editor", cell))
        re_rules = subp(_editor_stage_system_rules_text("retention_editor", cell))
        prompt = build_retention_editor_system_prompt(re_sys, re_rules)
        user_text = build_retention_editor_user_message(
            up_txt,
            block_writer_full_text,
        )
    elif stage_key == "hook_editor":
        hk_sys = subp(_stage_system_prompt_text("hook_editor", cell))
        hk_rules = subp(_editor_stage_system_rules_text("hook_editor", cell))
        prompt = build_hook_editor_system_prompt(hk_sys, hk_rules)
        user_text = build_hook_editor_user_message(
            up_txt,
            retention_editor_text,
        )
    elif stage_key == "flow_editor":
        fl_sys = subp(_stage_system_prompt_text("flow_editor", cell))
        fl_rules = subp(_editor_stage_system_rules_text("flow_editor", cell))
        prompt = build_flow_editor_system_prompt(fl_sys, fl_rules)
        user_text = build_flow_editor_user_message(
            up_txt,
            hook_editor_text,
        )
    elif stage_key == "persona_editor":
        pe_sys = subp(_stage_system_prompt_text("persona_editor", cell))
        pe_rules = subp(_editor_stage_system_rules_text("persona_editor", cell))
        prompt = build_persona_editor_system_prompt(pe_sys, pe_rules)
        user_text = build_persona_editor_user_message(
            up_txt,
            hero_use,
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
        prompt = build_rewrite_stage_system_prompt(
            stage_prompt_t,
            rules_resolved,
        )
        user_text = build_rewrite_stage_user_message(
            up_txt,
            inbox_text,
        )
    elif stage_key == "voiceover_editor":
        vo_sys = subp(_stage_system_prompt_text("voiceover_editor", cell))
        vo_rules = subp(_voiceover_editor_system_rules_text(cell))
        prompt = build_voiceover_editor_system_prompt(
            vo_sys,
            vo_rules,
        )
        # В пресете «Я уже ЗАrewriteИЛ» Voiceover Editor берёт текст из Inbox.Result.
        ve_input_text = persona_editor_text
        preset_n = normalize_rewrite_preset(preset)
        if preset_n == REWRITE_PRESET_PREWRITTEN:
            ve_input_text = str((stages_snap.get("inbox") or {}).get("last_result") or "")
            if not ve_input_text.strip():
                return None, "Сначала вставьте готовый текст в Inbox (Result)."
        elif preset_n == REWRITE_PRESET_SOFT:
            ve_input_text = str((stages_snap.get("rewrite") or {}).get("last_result") or "")
            if not ve_input_text.strip():
                return None, "Сначала выполните Rewrite и дождитесь Result."
        user_text = build_voiceover_editor_user_message(
            up_txt,
            ve_input_text,
        )
    elif stage_key == "elevenlabs_editor":
        el_sys = subp(_stage_system_prompt_text("elevenlabs_editor", cell))
        prompt = build_elevenlabs_editor_system_prompt(el_sys)
        preset_n = normalize_rewrite_preset(preset)
        el_input = downstream_script_input_text(
            preset_n,
            stages_snap,
            voiceover_editor_text=voiceover_editor_text,
            source_text=source_text,
        )
        if not el_input.strip():
            if preset_n == REWRITE_PRESET_PREWRITTEN:
                return None, "Сначала вставьте готовый текст в Inbox (Result) или выполните Voiceover Editor."
            return None, "Сначала выполните Voiceover Editor — нет текста для озвучки."
        user_text = build_elevenlabs_editor_user_message(up_txt, el_input)
    elif stage_key == "title_strategist":
        ts_sys = subp(_stage_system_prompt_text("title_strategist", cell))
        prompt = build_title_strategist_system_prompt(
            ts_sys,
        )
        preset_n = normalize_rewrite_preset(preset)
        ts_input_text = downstream_script_input_text(
            preset_n,
            stages_snap,
            voiceover_editor_text=voiceover_editor_text,
            source_text=source_text,
        )
        if preset_n in (REWRITE_PRESET_PREWRITTEN, REWRITE_PRESET_SOFT) and not ts_input_text.strip():
            if preset_n == REWRITE_PRESET_PREWRITTEN:
                return None, "Сначала вставьте готовый текст в Inbox (Result) или выполните Voiceover Editor."
            return None, "Сначала выполните Rewrite (Result) или Voiceover Editor."
        user_text = build_title_strategist_user_message(
            up_txt,
            ts_input_text,
            original_title=original_title,
        )
    elif stage_key == "structure_splitter":
        ss_sys = subp(_stage_system_prompt_text("structure_splitter", cell))
        prompt = build_structure_splitter_system_prompt(
            ss_sys,
        )
        preset_n = normalize_rewrite_preset(preset)
        ss_input_text = downstream_script_input_text(
            preset_n,
            stages_snap,
            voiceover_editor_text=voiceover_editor_text,
            source_text=source_text,
        )
        if preset_n in (REWRITE_PRESET_PREWRITTEN, REWRITE_PRESET_SOFT) and not ss_input_text.strip():
            if preset_n == REWRITE_PRESET_PREWRITTEN:
                return None, "Сначала вставьте готовый текст в Inbox (Result) или выполните Voiceover Editor."
            return None, "Сначала выполните Rewrite (Result) или Voiceover Editor."
        user_text = build_structure_splitter_user_message(
            up_txt,
            ss_input_text,
        )
    elif stage_key == "scene_writer":
        prompt = stage_prompt_t.strip()
        style_prompt = subp(str(cell.get("style_prompt") or "")).strip()
        up = up_txt
        user_text = _join_user_sections(up, style_prompt)
    elif stage_key == "youtube_packaging":
        ts = (title_strategist_result_text or "").strip()
        if not ts:
            return None, "Нет результата Title Strategist — выполните этап Title Strategist и сохраните проект."
        prompt = build_youtube_packaging_system_prompt(stage_prompt_t)
        user_text = build_youtube_packaging_user_message(
            up_txt,
            ts,
        )
    prompt = (prompt or "").strip()
    user_text = (user_text or "").strip()
    if stage_key not in (
        "analysis",
        "structure",
        "retention_editor",
        "hook_editor",
        "flow_editor",
        "persona_editor",
        "voiceover_editor",
        "title_strategist",
        "structure_splitter",
        "scene_writer",
        "youtube_packaging",
        "rewrite",
    ):
        dur_payload = build_duration_length_spec_payload(
            target_chars=target_chars,
        )
        if dur_payload:
            dur_head = _format_duration_user_preamble(dur_payload)
            if dur_head:
                user_text = (dur_head + "\n\n" + user_text).strip() if user_text else dur_head
    if not prompt:
        return None, "Введите промпт (инструкцию для модели)."
    if not user_text:
        return None, "Введите текст для обработки."
    payload = openai_chat_completions_request_dict(
        model, prompt, user_text, sanitize=True, temperature=chat_temperature
    )
    return payload, None


def build_stage_user_message(
    source_text: str,
    stage_key: str,
    stages: dict[str, Any],
    *,
    hero_prompt: str = "",
) -> str:
    """User одной строкой текста: Hero (кроме analysis), исходник, результаты предыдущих этапов.

    Ориентир длины (length_spec) добавляется в ``compose_rewrite_openai_request_body``.
    """
    parts: list[str] = []
    h = (hero_prompt or "").strip()
    if h and stage_key != "analysis":
        parts.append(h)
    parts.append((source_text or "").strip() or "(пусто)")
    idx = _STAGE_ORDER_INDEX[stage_key]
    for i in range(idx):
        pk, _plabel = REWRITE_STAGES[i]
        block = (stages.get(pk) or {}).get("last_result") or ""
        body = block.strip() or "(пусто)"
        if pk == "analysis":
            parts.append(f"analysis.json\n\n{body}")
        elif pk == "structure":
            parts.append(f"architect.json\n\n{body}")
        else:
            parts.append(f"{pk}_result\n\n{body}")
    return _join_user_sections(*parts)


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
            "structure_splitter_check": cell.get("structure_splitter_check") if isinstance(cell.get("structure_splitter_check"), dict) else None,
            "elevenlabs_editor_check": cell.get("elevenlabs_editor_check") if isinstance(cell.get("elevenlabs_editor_check"), dict) else None,
            "block_writer_check": cell.get("block_writer_check") if isinstance(cell.get("block_writer_check"), dict) else None,
            "model": normalize_rewrite_model(str(cell.get("model") or "")),
            "last_result": str(cell.get("last_result") or ""),
            "voiceover_changes": str(cell.get("voiceover_changes") or "") if key == "voiceover_editor" else "",
            "retention_editor_changes": str(cell.get("retention_editor_changes") or "") if key == "retention_editor" else "",
            "hook_editor_changes": str(cell.get("hook_editor_changes") or "") if key == "hook_editor" else "",
            "flow_editor_changes": str(cell.get("flow_editor_changes") or "") if key == "flow_editor" else "",
            "persona_editor_changes": str(cell.get("persona_editor_changes") or "") if key == "persona_editor" else "",
            "style_prompt_locked": bool(cell.get("style_prompt_locked")),
            "past_prompt_locked": bool(cell.get("past_prompt_locked")),
        }
    return source_text, stages


def snapshot_original_title_from_body(body: dict[str, Any], job: dict[str, Any]) -> str:
    """Поле «Исходное название» для user этапа title_strategist: снимок с формы или из project.json."""
    if isinstance(body, dict) and "source_title" in body:
        return str(body.get("source_title") or "").strip()
    return str((job or {}).get("source_title") or "").strip()


def snapshot_rewrite_pipeline_language_from_body(
    body: dict[str, Any], job: dict[str, Any] | None = None
) -> str:
    if isinstance(body, dict) and "rewrite_pipeline_language" in body:
        return normalize_rewrite_pipeline_language(body.get("rewrite_pipeline_language"))
    if isinstance(job, dict):
        return normalize_rewrite_pipeline_language(job.get("rewrite_pipeline_language"))
    return normalize_rewrite_pipeline_language("ru")


def rewrite_placeholder_apply_from_request(
    text: str | None,
    body: dict[str, Any] | None,
    job: dict[str, Any] | None,
    *,
    allow_nested_master_hero: bool = True,
) -> str:
    """Подстановка {{LANGUAGE}} и др. в произвольный текст по снимку body + project.json.

    Поля из body переопределяют job (как при сохранённом проекте + форма запуска).
    """
    snap: dict[str, Any] = {**(job or {}), **(body or {})}
    master_raw = snapshot_master_prompt_from_body(snap)
    hero_raw, target_chars, duration_minutes, chars_per_minute, _chat_temp_unused = snapshot_pipeline_extras_from_body(snap)
    hero_raw = str(hero_raw or "")
    ot = snapshot_original_title_from_body(snap, job or {})
    lang = snapshot_rewrite_pipeline_language_from_body(snap, job or {})
    return apply_prompt_placeholders(
        text,
        language=lang,
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
        target_chars=target_chars,
        original_title=ot,
        master_prompt=str(master_raw or ""),
        hero_prompt=hero_raw,
        allow_nested_master_hero=allow_nested_master_hero,
    )


def snapshot_master_prompt_from_body(body: dict[str, Any]) -> str:
    return str(body.get("master_prompt") or "")


def snapshot_rewrite_preset_from_body(body: dict[str, Any], job: dict[str, Any] | None = None) -> str:
    """Снимок текущего пресета: берём из body (если передан), иначе из job, иначе дефолт."""
    if isinstance(body, dict) and "rewrite_preset" in body:
        return normalize_rewrite_preset(body.get("rewrite_preset"))
    return normalize_rewrite_preset((job or {}).get("rewrite_preset"))


def snapshot_pipeline_extras_from_body(body: dict[str, Any]) -> tuple[str, int, int, int, float]:
    """hero_prompt, target_chars (500–40 000), duration_minutes, chars_per_minute, chat_temperature (0…2)."""
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
            tc = clamp_target_chars(int(body["target_chars"]))
        except (TypeError, ValueError):
            tc = clamp_target_chars(dm * cpm)
    else:
        tc = clamp_target_chars(dm * cpm)
    chat_temp = clamp_chat_temperature(body.get("chat_temperature"))
    return hero, tc, dm, cpm, chat_temp
