"""
ReWrite Master — вызов OpenAI Chat Completions.

Единое тело запроса (OpenAI): ``{"model", "temperature", "messages"}`` с ролями
``system`` и ``user``; см. ``openai_chat_completions_request_dict``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from collections.abc import Callable, Iterator
from typing import Any

import requests

from claude_kie import (
    CLAUDE_MODELS,
    claude_messages_wire_payload,
    is_claude_model,
    iter_claude_completion,
    iter_claude_completion_stream,
    post_claude_messages_sync,
)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

REWRITE_MODELS: list[dict[str, str]] = [
    {"id": "gpt-4.1", "label": "GPT-4.1"},
    {"id": "gpt-5.4", "label": "GPT-5.4"},
    *CLAUDE_MODELS,
]

REWRITE_MODEL_IDS = {m["id"] for m in REWRITE_MODELS}

REWRITE_DEFAULT_MODEL = "gpt-4.1"

# Единый формат POST /v1/chat/completions (OpenAI): model, temperature, messages[system,user].
REWRITE_CHAT_TEMPERATURE = 0.7

# Маркер конца ответа в тексте ассистента (Rewrite): System Promt просит модель
# завершать каждый фрагмент или весь ответ этим токеном; HTTP-стрим обрываем
# сразу после первого вхождения, в Result уходит текст без маркеров, сегменты
# между маркерами склеиваются подряд.
REWRITE_STREAM_USER_TERMINATOR = "[END]"

# Маркер в ответе модели может отличаться пробелами/регистром: [END], [ END ], [end].
_RW_END_TOKEN_RE = re.compile(r"\[\s*END\s*\]", re.IGNORECASE)
_RW_END_LINE_ONLY_RE = re.compile(r"(?m)^\s*\[\s*END\s*\]\s*$", re.IGNORECASE)
_RW_END_TAIL_RE = re.compile(r"\[\s*END\s*\]\s*$", re.IGNORECASE)


def accumulated_matches_rewrite_stream_terminator(acc: str, terminator: str | None) -> bool:
    if not terminator:
        return False
    if terminator == REWRITE_STREAM_USER_TERMINATOR or terminator.strip().upper() == "[END]":
        return _RW_END_TOKEN_RE.search(acc or "") is not None
    return terminator in (acc or "")


def scrub_rewrite_end_markers(text: str) -> str:
    """Удаляет типичные варианты маркера ``[END]`` (строка целиком, хвост, fullwidth-скобки)."""
    s = text or ""
    s = s.replace("\uff3bEND\uff3d", "").replace("\uff3b END \uff3d", "")
    while True:
        t = s
        s = _RW_END_LINE_ONLY_RE.sub("", s)
        s = _RW_END_TAIL_RE.sub("", s.rstrip("\n\r \t"))
        if s == t:
            break
    s = _RW_END_TOKEN_RE.sub("", s)
    return s.rstrip("\n\r \t")


def finalize_rewrite_stream_join_segments(raw: str, terminator: str) -> str:
    """Склеивает части ответа между маркерами `terminator` подряд; маркеры удаляются.

    Если `terminator` ни разу не встретился — возвращаем весь `raw` (как раньше
    для стримов без протокола завершения).
    """
    if not raw:
        return ""
    if not terminator:
        return raw
    if terminator == REWRITE_STREAM_USER_TERMINATOR or terminator.strip().upper() == "[END]":
        if not _RW_END_TOKEN_RE.search(raw):
            return raw
        parts = _RW_END_TOKEN_RE.split(raw)
    elif terminator in raw:
        parts = raw.split(terminator)
    else:
        return raw
    while parts and parts[-1] == "":
        parts.pop()
    return "".join(parts)


def strip_rewrite_stream_terminator_suffix(text: str, marker: str) -> str:
    """Убирает хвостовой маркер (например ``[END]``) с конца текста: суффиксом или отдельной строкой."""
    if not marker:
        return scrub_rewrite_end_markers(text or "")
    s = text or ""
    m = marker.strip()
    while True:
        t = s.rstrip("\n\r \t")
        lines = t.splitlines()
        if lines and lines[-1].strip() == m:
            s = "\n".join(lines[:-1])
            continue
        if t.endswith(marker):
            s = t[: -len(marker)].rstrip("\n\r \t")
            continue
        break
    return scrub_rewrite_end_markers(s)


def clamp_chat_temperature(val: Any) -> float:
    """Диапазон 0…2 (шаг не фиксируем — UI может слать 0.05). Невалидное → дефолт конвейера."""
    try:
        x = float(val)
    except (TypeError, ValueError):
        return float(REWRITE_CHAT_TEMPERATURE)
    if x < 0.0:
        return 0.0
    if x > 2.0:
        return 2.0
    return round(x, 4)


def normalize_rewrite_model(model: str) -> str:
    m = (model or "").strip()
    return m if m in REWRITE_MODEL_IDS else REWRITE_DEFAULT_MODEL


def _chat_timeout_seconds() -> int:
    raw = (os.getenv("OPENAI_CHAT_TIMEOUT") or "").strip()
    if not raw:
        return 600
    try:
        v = int(raw)
        return max(60, min(v, 3600))
    except ValueError:
        return 600


def _sanitize_for_openai_json(s: str) -> str:
    """Убирает проблемные символы (в т.ч. lone surrogates), чтобы JSON-тело всегда кодировалось корректно."""
    t = (s or "").replace("\x00", " ")
    return t.encode("utf-8", "replace").decode("utf-8", "replace")


def _openai_chat_json_body_bytes(payload: dict[str, Any]) -> bytes:
    """Тело POST: строгий JSON (ASCII \\uXXXX для не-ASCII) + UTF-8 bytes — надёжнее, чем requests json= для некоторых прокси/шлюзов."""
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _openai_chat_request_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _openai_error_message(r: requests.Response) -> str:
    err_body = (r.text or "")[:2000]
    try:
        err_json = r.json()
        em = err_json.get("error") or {}
        if isinstance(em, dict) and em.get("message"):
            return str(em.get("message"))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return err_body or (r.reason or str(r.status_code))


def openai_chat_completions_request_dict(
    model: str,
    system_content: str,
    user_content: str,
    *,
    sanitize: bool = True,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Строго формат Chat Completions для OpenAI: model, temperature, messages (system + user).

    При stream добавляется ключ ``stream`` снаружи (см. ``iter_rewrite_completion_stream``).
    """
    mid = normalize_rewrite_model(model)
    if sanitize:
        sy = _sanitize_for_openai_json((system_content or "").strip())
        us = _sanitize_for_openai_json((user_content or "").strip())
    else:
        sy = system_content or ""
        us = user_content or ""
    t = float(REWRITE_CHAT_TEMPERATURE) if temperature is None else clamp_chat_temperature(temperature)
    return {
        "model": mid,
        "temperature": t,
        "messages": [
            {"role": "system", "content": sy},
            {"role": "user", "content": us},
        ],
    }


def rewrite_chat_completion_wire_payload(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    chat_temperature: float | None = None,
) -> dict[str, Any]:
    """Тело POST для текущего движка (OpenAI или Claude через Kie.ai) — как в iter_rewrite_completion.

    Для OpenAI — POST /v1/chat/completions: ``openai_chat_completions_request_dict`` (model, temperature, messages).
    Для Claude — POST /claude/v1/messages (system отдельным полем, messages: только user, max_tokens).
    """
    model = normalize_rewrite_model(model)
    if is_claude_model(model):
        return claude_messages_wire_payload(model, system_prompt, user_content)
    return openai_chat_completions_request_dict(
        model, system_prompt, user_content, sanitize=True, temperature=chat_temperature
    )


def _draft1_wire_payload_for_block(
    model: str,
    system_prompt_sanitized: str,
    block_writer_user_prompt_sanitized: str,
    b: dict[str, Any],
    short_summaries_before: list[list[str]],
    *,
    chat_temperature: float | None = None,
) -> dict[str, Any]:
    """Один POST Block Writer для блока b; short_summaries_before — уже завершённые блоки (до 3 последних в user).

    Возвращает payload в формате текущего движка: OpenAI Chat Completions либо Claude messages (Kie.ai).
    """
    model = normalize_rewrite_model(model)
    idx = int(b["index"])
    name = str(b["block_name"])
    tideal = int(b["target_chars_ideal"])
    raw_block = b.get("raw_block") if isinstance(b.get("raw_block"), dict) else {}
    architect_block_payload: dict[str, Any] = dict(raw_block)
    architect_block_payload["index"] = idx
    architect_block_payload["block_name"] = name
    architect_block_payload["target_chars"] = tideal
    architect_block_payload["must_cover"] = b["must_cover"]
    architect_block_payload["must_not_cover"] = b["must_not_cover"]
    prev_short = short_summaries_before[-3:]
    user_payload: dict[str, Any] = {
        "block_writer_user_promt": block_writer_user_prompt_sanitized,
        "architect_block": architect_block_payload,
        "short_summary_context": [
            {"from_block_offset": i + 1, "short_summary": s}
            for i, s in enumerate(reversed(prev_short))
        ],
    }
    user_msg = _sanitize_for_openai_json(json.dumps(user_payload, ensure_ascii=False, indent=2))
    if is_claude_model(model):
        return claude_messages_wire_payload(model, system_prompt_sanitized, user_msg)
    return openai_chat_completions_request_dict(
        model, system_prompt_sanitized, user_msg, sanitize=False, temperature=chat_temperature
    )


def list_draft1_wire_chat_payloads_for_export(
    model: str,
    system_prompt: str,
    structure_result: str,
    block_writer_user_prompt: str,
    saved_short_summaries: list[list[str]] | None,
    *,
    chat_temperature: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Список тел POST по блокам. bool: True если контекст short_summary для всех блоков совпадает с сохранёнными данными."""
    model = normalize_rewrite_model(model)
    system_prompt_sanitized = _sanitize_for_openai_json((system_prompt or "").strip())
    structure_result_sanitized = _sanitize_for_openai_json((structure_result or "").strip())
    bw_san = _sanitize_for_openai_json((block_writer_user_prompt or "").strip())
    blocks = _extract_structure_blocks(structure_result_sanitized)
    if not blocks:
        return [], True
    short_summaries: list[list[str]] = []
    out: list[dict[str, Any]] = []
    context_exact = True
    for bi, b in enumerate(blocks):
        out.append(
            _draft1_wire_payload_for_block(
                model,
                system_prompt_sanitized,
                bw_san,
                b,
                short_summaries,
                chat_temperature=chat_temperature,
            )
        )
        if saved_short_summaries is not None and bi < len(saved_short_summaries):
            short_summaries.append(list(saved_short_summaries[bi]))
        else:
            short_summaries.append([])
            if bi < len(blocks) - 1:
                context_exact = False
    return out, context_exact


def _post_chat_completion(api_key: str, payload: dict[str, Any], timeout: int, out: queue.Queue) -> None:
    """POST в фоновом потоке. Если payload — для Claude (модель в CLAUDE_MODEL_IDS),
    результат складывается как ('claude_text', text|err_msg, ok_bool); иначе как ('ok', requests.Response)
    или ('err', exception).
    """
    if is_claude_model(str(payload.get("model") or "")):
        text, err = post_claude_messages_sync(payload, timeout)
        if err:
            out.put(("claude_err", err))
        else:
            out.put(("claude_ok", text or ""))
        return
    try:
        body = _openai_chat_json_body_bytes(payload)
        r = requests.post(
            OPENAI_CHAT_URL,
            headers=_openai_chat_request_headers(api_key),
            data=body,
            timeout=timeout,
        )
        out.put(("ok", r))
    except Exception as e:
        out.put(("err", e))


def iter_rewrite_completion(
    api_key: str,
    model: str,
    prompt: str,
    text: str,
    *,
    timeout: int | None = None,
    chat_temperature: float | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Yields события для NDJSON-стрима:
    {"type": "status", "message": "..."}
    {"type": "error", "message": "..."} — последнее при ошибке
    {"type": "result", "content": "..."} — успех
    """
    model = normalize_rewrite_model(model)
    if timeout is None:
        timeout = _chat_timeout_seconds()
    prompt_st = (prompt or "").strip()
    text_st = (text or "").strip()

    if is_claude_model(model):
        yield from iter_claude_completion(model, prompt_st, text_st, timeout=timeout)
        return

    yield {"type": "status", "message": "Проверка ввода…"}
    if not api_key.strip():
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY в .env"}
        return
    if not prompt_st:
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return
    if not text_st:
        yield {"type": "error", "message": "Введите текст для обработки."}
        return

    yield {"type": "status", "message": f"Модель: {model}"}
    yield {"type": "status", "message": "Формирование запроса к OpenAI (system + user)…"}

    payload = rewrite_chat_completion_wire_payload(model, prompt_st, text_st, chat_temperature=chat_temperature)

    yield {"type": "status", "message": "Отправка chat/completions на api.openai.com…"}

    out: queue.Queue = queue.Queue(maxsize=1)
    th = threading.Thread(
        target=_post_chat_completion,
        args=(api_key, payload, timeout, out),
        daemon=True,
    )
    th.start()

    wait_round = 0
    kind: str
    data: Any
    while True:
        try:
            kind, data = out.get(timeout=8.0)
            break
        except queue.Empty:
            if not th.is_alive():
                try:
                    kind, data = out.get_nowait()
                    break
                except queue.Empty:
                    yield {"type": "error", "message": "Соединение с OpenAI завершилось без ответа."}
                    return
            wait_round += 1
            yield {
                "type": "status",
                "message": "Ожидание ответа OpenAI…",
            }

    if kind == "err":
        yield {"type": "error", "message": f"Сеть / таймаут: {data}"}
        return

    r = data
    yield {"type": "status", "message": f"Ответ HTTP {r.status_code}"}

    if not r.ok:
        yield {"type": "error", "message": _openai_error_message(r)}
        return

    yield {"type": "status", "message": "Разбор JSON ответа…"}

    try:
        body = r.json()
        choice0 = (body.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        content = msg.get("content")
        if content is None:
            yield {"type": "error", "message": "В ответе нет choices[0].message.content"}
            return
        if not isinstance(content, str):
            content = str(content)
    except (KeyError, IndexError, TypeError) as e:
        yield {"type": "error", "message": f"Неожиданная структура ответа: {e}"}
        return

    yield {"type": "status", "message": "Готово."}
    yield {"type": "result", "content": content}


def iter_rewrite_completion_stream(
    api_key: str,
    model: str,
    prompt: str,
    text: str,
    *,
    timeout: int | None = None,
    chat_temperature: float | None = None,
    content_stream_terminator: str | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Поток chat/completions (stream=true). События для NDJSON:
    {"type": "status", "message": "..."}
    {"type": "delta", "content": "..."} — фрагмент ответа (подряд склеиваются в полный текст)
    {"type": "error", "message": "..."}
    {"type": "result", "content": "..."} — полный накопленный ответ в конце

    Если задан ``content_stream_terminator`` (для Rewrite: ``[END]``), после первого
    вхождения маркера в накопленный текст HTTP-стрим читается дальше не обязателен —
    соединение закрывается; в ``result`` уходит текст без маркеров, сегменты между
    маркерами склеиваются подряд (см. ``finalize_rewrite_stream_join_segments``).
    """
    model = normalize_rewrite_model(model)
    if timeout is None:
        timeout = _chat_timeout_seconds()
    prompt_st = (prompt or "").strip()
    text_st = (text or "").strip()

    if is_claude_model(model):
        yield from iter_claude_completion_stream(
            model,
            prompt_st,
            text_st,
            timeout=timeout,
            content_stream_terminator=content_stream_terminator,
        )
        return

    yield {"type": "status", "message": "Проверка ввода…"}
    if not api_key.strip():
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY в .env"}
        return
    if not prompt_st:
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return
    if not text_st:
        yield {"type": "error", "message": "Введите текст для обработки."}
        return

    yield {"type": "status", "message": f"Модель: {model}"}
    yield {"type": "status", "message": "Потоковый запрос к OpenAI (stream)…"}

    payload = rewrite_chat_completion_wire_payload(model, prompt_st, text_st, chat_temperature=chat_temperature)
    payload["stream"] = True

    acc = ""
    try:
        body = _openai_chat_json_body_bytes(payload)
        with requests.post(
            OPENAI_CHAT_URL,
            headers=_openai_chat_request_headers(api_key),
            data=body,
            timeout=(30, timeout),
            stream=True,
        ) as r:
            if not r.ok:
                yield {"type": "error", "message": _openai_error_message(r)}
                return

            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk: dict[str, Any] = json.loads(data)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    piece = delta.get("content")
                    if piece is None:
                        continue
                    if not isinstance(piece, str):
                        piece = str(piece)
                    if not piece:
                        continue
                    acc += piece
                    yield {"type": "delta", "content": piece}
                if content_stream_terminator and accumulated_matches_rewrite_stream_terminator(
                    acc, content_stream_terminator
                ):
                    break
    except requests.RequestException as e:
        yield {"type": "error", "message": f"Сеть / таймаут: {e}"}
        return

    out = (
        finalize_rewrite_stream_join_segments(acc, content_stream_terminator)
        if content_stream_terminator
        else acc
    )
    if content_stream_terminator:
        out = strip_rewrite_stream_terminator_suffix(out, content_stream_terminator)
    if not (out or "").strip():
        yield {"type": "error", "message": "Пустой ответ в потоке (нет текста от модели)."}
        return

    yield {"type": "status", "message": "Готово."}
    yield {"type": "result", "content": out}


def _post_chat_completion_sync(
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[str | None, str | None]:
    """Sync POST → (content_text, err) для текущего движка.

    Тип движка определяется по содержимому payload: если есть top-level "system"
    и "max_tokens" — это Claude через Kie.ai, иначе OpenAI Chat Completions.
    """
    if is_claude_model(str(payload.get("model") or "")):
        return post_claude_messages_sync(payload, timeout)
    try:
        body = _openai_chat_json_body_bytes(payload)
        r = requests.post(
            OPENAI_CHAT_URL,
            headers=_openai_chat_request_headers(api_key),
            data=body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        return None, f"Сеть / таймаут: {e}"
    if not r.ok:
        return None, _openai_error_message(r)
    try:
        body = r.json()
        choice0 = (body.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        content = msg.get("content")
        if content is None:
            return None, "В ответе нет choices[0].message.content"
        if not isinstance(content, str):
            content = str(content)
        return content, None
    except (KeyError, IndexError, TypeError):
        return None, "Неожиданная структура ответа OpenAI."


def _extract_structure_blocks(structure_result: str) -> list[dict[str, Any]] | None:
    try:
        obj = json.loads((structure_result or "").strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    blocks = obj.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    out: list[dict[str, Any]] = []
    for i, b in enumerate(blocks, start=1):
        if not isinstance(b, dict):
            return None
        name = str(b.get("block_name") or "").strip()
        if not name:
            return None
        # Поддерживаем оба варианта architect.json:
        # 1) старый: target_chars_min/ideal/max
        # 2) новый: target_chars (или target_chars_ideal) без min/max
        try:
            if b.get("target_chars_ideal") is not None:
                tideal = int(b.get("target_chars_ideal"))
            else:
                tideal = int(b.get("target_chars"))
        except (TypeError, ValueError):
            return None
        try:
            tmin = int(b.get("target_chars_min")) if b.get("target_chars_min") is not None else max(1, int(round(tideal * 0.8)))
            tmax = int(b.get("target_chars_max")) if b.get("target_chars_max") is not None else max(tideal, int(round(tideal * 1.2)))
        except (TypeError, ValueError):
            return None
        if tmin < 1 or tideal < 1 or tmax < 1 or tmin > tmax:
            return None
        out.append(
            {
                "index": i,
                "block_name": name,
                "target_chars_min": tmin,
                "target_chars_ideal": tideal,
                "target_chars_max": tmax,
                "must_cover": b.get("must_cover") if isinstance(b.get("must_cover"), list) else [],
                "must_not_cover": b.get("must_not_cover") if isinstance(b.get("must_not_cover"), list) else [],
                "raw_block": dict(b),
            }
        )
    return out


def _parse_single_block(raw: str, expected_idx: int, expected_name: str) -> tuple[str | None, str | None]:
    txt = (raw or "").strip()
    m = re.match(
        rf"^BLOCK_START:\s*{expected_idx}\s*\nBLOCK_NAME:\s*(.*?)\n(.*?)\nBLOCK_END:\s*{expected_idx}\s*$",
        txt,
        flags=re.DOTALL,
    )
    if not m:
        return None, "Неверный формат блока (ожидались BLOCK_START/BLOCK_NAME/BLOCK_END)."
    got_name = m.group(1).strip()
    if got_name != expected_name:
        return None, f"BLOCK_NAME не совпадает. Ожидалось: {expected_name}"
    body = m.group(2).strip()
    if not body:
        return None, "Пустой текст блока."
    return body, None


def _extract_json_object_from_text(raw: str) -> str | None:
    """Достаёт JSON-объект из ответа модели, если он завёрнут в markdown
    или окружён пояснительным текстом. Возвращает строку JSON или None.

    Пробуем по очереди:
      1) ```json ... ``` (или ``` ... ```);
      2) подстрока от первой '{' до последней '}'.
    """
    if not raw:
        return None
    s = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        return s[first : last + 1].strip()
    return None


def _parse_block_writer_json_response(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    txt = (raw or "").strip()
    if not txt:
        return None, "Пустой ответ модели."
    obj: Any = None
    try:
        obj = json.loads(txt)
    except (json.JSONDecodeError, TypeError, ValueError):
        # Fallback: модели (особенно Claude) часто оборачивают JSON в ```json ... ```
        # или добавляют пояснительный текст до/после. Пробуем извлечь JSON-объект
        # и распарсить ещё раз.
        candidate = _extract_json_object_from_text(txt)
        if candidate:
            try:
                obj = json.loads(candidate)
            except (json.JSONDecodeError, TypeError, ValueError):
                obj = None
        if obj is None:
            return None, "Block Writer должен вернуть JSON."
    if not isinstance(obj, dict):
        return None, "Block Writer должен вернуть JSON-объект."
    block_text = str(obj.get("text") or obj.get("block_text") or "").strip()
    short_summary_raw = obj.get("short_summary")
    short_summary_items: list[str] = []
    if isinstance(short_summary_raw, list):
        for it in short_summary_raw:
            s = str(it or "").strip()
            if s:
                short_summary_items.append(s)
    elif short_summary_raw is not None:
        s = str(short_summary_raw or "").strip()
        if s:
            short_summary_items.append(s)
    if not block_text:
        return None, "В ответе нет text."
    if not short_summary_items:
        # Fallback: если модель не вернула summary, берём начало текста.
        short_summary_items = [(block_text[:220] + "...") if len(block_text) > 220 else block_text]
    try:
        chars_val = int(obj.get("chars"))
        if chars_val < 0:
            chars_val = len(block_text)
    except (TypeError, ValueError):
        chars_val = len(block_text)
    obj["text"] = block_text
    obj["block_text"] = block_text
    obj["chars"] = chars_val
    obj["short_summary"] = short_summary_items
    return obj, None


def iter_draft1_blockwise_completion(
    api_key: str,
    model: str,
    system_prompt: str,
    analysis_result: str,
    structure_result: str,
    *,
    block_writer_user_prompt: str = "",
    on_block_completed: Callable[[dict[str, Any]], None] | None = None,
    on_all_completed: Callable[[dict[str, Any]], None] | None = None,
    timeout: int | None = None,
    chat_temperature: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Block Writer: один API вызов на каждый блок из architect.json."""
    model = normalize_rewrite_model(model)
    system_prompt = _sanitize_for_openai_json((system_prompt or "").strip())
    structure_result = _sanitize_for_openai_json((structure_result or "").strip())
    block_writer_user_prompt = _sanitize_for_openai_json((block_writer_user_prompt or "").strip())
    if timeout is None:
        timeout = _chat_timeout_seconds()
    if not api_key.strip():
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY в .env"}
        return
    if not system_prompt:
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return

    blocks = _extract_structure_blocks(structure_result)
    if not blocks:
        yield {"type": "error", "message": "Architect Result должен быть JSON с массивом blocks и target_chars (или target_chars_ideal)."}
        return

    yield {"type": "status", "message": f"Block Writer loop: блоков {len(blocks)}."}

    completed_blocks: list[dict[str, Any]] = []
    short_summaries: list[list[str]] = []
    all_block_texts: list[str] = []

    for b in blocks:
        idx = int(b["index"])
        name = str(b["block_name"])
        tmin = int(b["target_chars_min"])
        tideal = int(b["target_chars_ideal"])
        tmax = int(b["target_chars_max"])

        prev_short = short_summaries[-3:]
        prev_short_meta = [
            {
                "offset_from_current": i + 1,
                "items": len(s),
                "chars_total": sum(len(x) for x in s),
            }
            for i, s in enumerate(reversed(prev_short))
        ]
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] Блок {idx}/{len(blocks)} «{name}»: подготовка запроса. "
                f"Цель длины min/ideal/max = {tmin}/{tideal}/{tmax}."
            ),
        }
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] short_summary_context: {len(prev_short)} шт. "
                + (
                    "Мета: "
                    + ", ".join(
                        f"-{m['offset_from_current']} (items={m['items']}, chars={m['chars_total']})"
                        for m in prev_short_meta
                    )
                    if prev_short_meta
                    else "Для первого блока контекст пуст."
                )
            ),
        }
        payload = _draft1_wire_payload_for_block(
            model,
            system_prompt,
            block_writer_user_prompt,
            b,
            short_summaries,
            chat_temperature=chat_temperature,
        )
        # Длина user-сообщения для статус-логов; payload может быть OpenAI-формата
        # (messages=[system,user]) либо Claude-формата (messages=[user] + top-level system).
        _msgs_for_log = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        user_msg = ""
        for _m in _msgs_for_log:
            if isinstance(_m, dict) and str(_m.get("role") or "").lower() == "user":
                user_msg = str(_m.get("content") or "")
                break
        architect_block_json = json.dumps(
            {
                "index": idx,
                "block_name": name,
                "target_chars_min": tmin,
                "target_chars_ideal": tideal,
                "target_chars_max": tmax,
                "must_cover": b["must_cover"],
                "must_not_cover": b["must_not_cover"],
            },
            ensure_ascii=False,
        )
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] payload: user_promt chars={len(block_writer_user_prompt)}, "
                f"architect_block chars={len(architect_block_json)}."
            ),
        }
        provider_label = "Kie.ai (Claude)" if is_claude_model(model) else "OpenAI"
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] Блок {idx}/{len(blocks)}: отправка POST в {provider_label} "
                f"(user JSON chars={len(user_msg)})."
            ),
        }
        content, err = _post_chat_completion_sync(api_key, payload, timeout)
        if err:
            yield {"type": "error", "message": f"Блок {idx}: {err}"}
            return
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] Блок {idx}: ответ получен, raw chars={len(content or '')}. "
                "Пробуем распарсить JSON."
            ),
        }
        parsed, perr = _parse_block_writer_json_response(content or "")
        if perr:
            yield {"type": "error", "message": f"Блок {idx}: {perr}"}
            return
        block_text = str(parsed.get("text") or parsed.get("block_text") or "")
        short_summary = parsed.get("short_summary") if isinstance(parsed.get("short_summary"), list) else []
        short_summary = [str(x or "").strip() for x in short_summary if str(x or "").strip()]
        try:
            chars = int(parsed.get("chars"))
        except (TypeError, ValueError):
            chars = len(block_text)
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] Блок {idx}: JSON валиден. "
                f"text chars={chars}, short_summary items={len(short_summary)}."
            ),
        }
        block_record = {
            "block_index": idx,
            "block_name": name,
            "target_chars_min": tmin,
            "target_chars_ideal": tideal,
            "target_chars_max": tmax,
            "actual_chars": chars,
            "short_summary": short_summary,
            "block_text": block_text,
        }
        completed_blocks.append(block_record)
        short_summaries.append(short_summary)
        all_block_texts.append(block_text)
        if on_block_completed:
            try:
                on_block_completed(block_record)
            except Exception:
                pass
        within = tmin <= chars <= tmax
        yield {
            "type": "status",
            "message": (
                f"[Block Writer] Блок {idx} сохранён в файл block_{idx:03d}.json. "
                f"Длина {chars} симв. (диапазон {tmin}–{tmax}: {'OK' if within else 'OUT'})."
            ),
        }

    final_text = "\n\n".join(all_block_texts).strip()
    yield {
        "type": "status",
        "message": (
            f"[Block Writer] Все блоки готовы: {len(completed_blocks)} шт. "
            f"Склейка full_text chars={len(final_text)} и сохранение all_blocks.json/full_text.txt."
        ),
    }
    if on_all_completed:
        try:
            on_all_completed({"blocks": completed_blocks, "full_text": final_text})
        except Exception:
            pass
    yield {"type": "result", "content": final_text}


def iter_draft1_blockwise_completion_legacy(
    api_key: str,
    model: str,
    system_prompt: str,
    analysis_result: str,
    structure_result: str,
    *,
    timeout: int | None = None,
    max_attempts_per_block: int = 3,
    chat_temperature: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Draft1: по одному блоку. Принятие строго при target_chars_min <= len <= target_chars_max.

    После исчерпания попыток: из всех успешно распарсенных вариантов берём тот, чья длина ближе всего к
    target_chars_ideal, и помечаем принудительный приём.
    """
    model = normalize_rewrite_model(model)
    system_prompt = _sanitize_for_openai_json((system_prompt or "").strip())
    analysis_result = _sanitize_for_openai_json((analysis_result or "").strip())
    structure_result = _sanitize_for_openai_json((structure_result or "").strip())
    if timeout is None:
        timeout = _chat_timeout_seconds()
    if not api_key.strip():
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY в .env"}
        return
    if not system_prompt:
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return

    blocks = _extract_structure_blocks(structure_result)
    if not blocks:
        yield {"type": "error", "message": "Architect Result должен быть JSON с массивом blocks и target_chars (или target_chars_ideal)."}
        return

    analysis_compact = analysis_result
    if len(analysis_compact) > 40000:
        analysis_compact = analysis_compact[:40000].rstrip() + "\n...[analysis truncated]..."
    blocks_overview_lines = []
    for bb in blocks:
        blocks_overview_lines.append(
            f"{bb['index']}. {bb['block_name']} "
            f"(min={bb['target_chars_min']}, ideal={bb['target_chars_ideal']}, max={bb['target_chars_max']})"
        )
    blocks_overview = "\n".join(blocks_overview_lines)

    accepted: list[str] = []
    yield {"type": "status", "message": f"Draft1 block-loop: блоков {len(blocks)}."}

    for b in blocks:
        idx = int(b["index"])
        name = str(b["block_name"])
        tmin = int(b["target_chars_min"])
        tideal = int(b["target_chars_ideal"])
        tmax = int(b["target_chars_max"])
        must_cover = "\n".join(f"- {x}" for x in b["must_cover"]) or "- (нет)"
        must_not_cover = "\n".join(f"- {x}" for x in b["must_not_cover"]) or "- (нет)"
        accepted_indexes = [str(i + 1) for i in range(len(accepted))]
        accepted_overview = ", ".join(accepted_indexes) if accepted_indexes else "(пока пусто)"
        feedback = ""
        accepted_this_block = False
        parsed_candidates: list[tuple[str, int]] = []
        for attempt in range(1, max_attempts_per_block + 1):
            yield {"type": "status", "message": f"Блок {idx}/{len(blocks)}: попытка {attempt}…"}
            user_msg = json.dumps(
                {
                    "analysis.json": analysis_compact or "(пусто)",
                    "blocks_overview": blocks_overview,
                    "current_block_spec": {
                        "index": idx,
                        "block_name": name,
                        "target_chars_min": tmin,
                        "target_chars_ideal": tideal,
                        "target_chars_max": tmax,
                        "must_cover": b["must_cover"],
                        "must_not_cover": b["must_not_cover"],
                    },
                    "already_accepted_block_indexes": accepted_overview,
                    "task": {
                        "instruction": f"Write ONLY block {idx} in exact BLOCK_START/BLOCK_NAME/BLOCK_END format.",
                        "format": [
                            f"BLOCK_START: {idx}",
                            f"BLOCK_NAME: {name}",
                            "<block_text>",
                            f"BLOCK_END: {idx}",
                        ],
                        "rules": [
                            f"target_chars_min={tmin}",
                            f"target_chars_ideal={tideal}",
                            f"target_chars_max={tmax}",
                            "Do not output SCRIPT_END.",
                            "Do not output other blocks.",
                            "Do not include any text before BLOCK_START or after BLOCK_END.",
                        ],
                        "must_cover": b["must_cover"],
                        "must_not_cover": b["must_not_cover"],
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            if feedback:
                user_msg += "\n\n" + json.dumps(
                    {"rewrite_request": feedback},
                    ensure_ascii=False,
                    indent=2,
                )
            user_msg = _sanitize_for_openai_json(user_msg)
            payload = openai_chat_completions_request_dict(
                model, system_prompt, user_msg, sanitize=False, temperature=chat_temperature
            )
            out: queue.Queue = queue.Queue(maxsize=1)
            th = threading.Thread(
                target=_post_chat_completion,
                args=(api_key, payload, timeout, out),
                daemon=True,
            )
            th.start()
            wait_round = 0
            kind: str
            data: Any
            while True:
                try:
                    kind, data = out.get(timeout=8.0)
                    break
                except queue.Empty:
                    if not th.is_alive():
                        try:
                            kind, data = out.get_nowait()
                            break
                        except queue.Empty:
                            yield {"type": "error", "message": "Соединение с OpenAI завершилось без ответа."}
                            return
                    wait_round += 1
                    yield {
                        "type": "status",
                        "message": (
                            f"Блок {idx}: ожидание ответа OpenAI… "
                            f"(попытка {attempt}/{max_attempts_per_block})."
                        ),
                    }
            if kind == "err":
                yield {"type": "error", "message": f"Сеть / таймаут: {data}"}
                return
            r = data
            if not r.ok:
                yield {"type": "error", "message": _openai_error_message(r)}
                return
            try:
                body_json = r.json()
                choice0 = (body_json.get("choices") or [{}])[0]
                msg = choice0.get("message") or {}
                content = msg.get("content")
                if content is None:
                    yield {"type": "error", "message": "В ответе нет choices[0].message.content"}
                    return
                if not isinstance(content, str):
                    content = str(content)
            except (KeyError, IndexError, TypeError):
                yield {"type": "error", "message": "Неожиданная структура ответа OpenAI."}
                return
            body, perr = _parse_single_block(content or "", idx, name)
            if perr:
                feedback = perr
                continue
            body = (body or "").strip()
            chars = len(body)
            parsed_candidates.append((body, chars))
            if chars < tmin:
                feedback = f"Блок слишком короткий: {chars} символов, минимум {tmin}. Перепиши этот же блок."
                continue
            if chars > tmax:
                feedback = f"Блок слишком длинный: {chars} символов, максимум {tmax}. Перепиши этот же блок."
                continue
            block_full = f"BLOCK_START: {idx}\nBLOCK_NAME: {name}\n{body}\nBLOCK_END: {idx}"
            accepted.append(block_full)
            yield {"type": "status", "message": f"Блок {idx} принят ({chars} симв., идеал {tideal})."}
            accepted_this_block = True
            break
        if not accepted_this_block:
            if not parsed_candidates:
                yield {
                    "type": "error",
                    "message": (
                        f"Блок {idx}: за {max_attempts_per_block} попыток не удалось получить ответ в нужном формате."
                    ),
                }
                return
            best_body, best_chars = min(parsed_candidates, key=lambda bc: abs(bc[1] - tideal))
            dist = abs(best_chars - tideal)
            if best_chars < tmin:
                forced_status = "below_min"
            elif best_chars > tmax:
                forced_status = "above_max"
            else:
                forced_status = "unexpected"
            note = (
                "DRAFT1_FORCED_LENGTH: "
                f"status={forced_status}; "
                f"selection=closest_to_ideal; "
                f"actual_chars={best_chars}; "
                f"distance_to_ideal={dist}; "
                f"candidates_parsed={len(parsed_candidates)}; "
                f"target_min={tmin}; target_ideal={tideal}; target_max={tmax}; "
                f"attempts_used={max_attempts_per_block}"
            )
            body_out = note + "\n\n" + best_body
            block_full = f"BLOCK_START: {idx}\nBLOCK_NAME: {name}\n{body_out}\nBLOCK_END: {idx}"
            accepted.append(block_full)
            yield {
                "type": "status",
                "message": (
                    f"Блок {idx}: после {max_attempts_per_block} попыток выбран вариант ближе всего к идеалу "
                    f"({best_chars} симв., идеал {tideal}, диапазон {tmin}–{tmax}). См. DRAFT1_FORCED_LENGTH в тексте."
                ),
            }

    final = ("\n\n".join(accepted) + "\n\nSCRIPT_END").strip()
    yield {"type": "result", "content": final}
