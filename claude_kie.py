"""
Claude (Anthropic) через Kie.ai — обёртка для ReWrite пайплайна.

Документация:
- https://docs.kie.ai/market/claude/claude-opus-4-7
- https://docs.kie.ai/market/claude/claude-sonnet-4-6

Эндпоинт: POST https://api.kie.ai/claude/v1/messages
Авторизация: Bearer KEYAI_API_KEY (тот же ключ Kie.ai, что и для image/video).
Тело:
  {
    "model": "claude-opus-4-7" | "claude-sonnet-4-6",
    "system": "...",
    "messages": [{"role": "user", "content": "..."}],
    "max_tokens": 4096,
    "stream": false
  }
Стрим (stream=true): SSE события Anthropic — message_start / content_block_start /
content_block_delta (delta.type=text_delta, delta.text) / content_block_stop /
message_delta / message_stop.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from collections.abc import Iterator
from typing import Any

import requests

CLAUDE_API_URL = "https://api.kie.ai/claude/v1/messages"


# Regex для распознавания markdown code-fence от LLM-ответов.
# Поддерживает: ```json ... ```, ``` ... ```, ```jsonc ... ```, ```python ... ``` и т.п.
# Допускаем как многострочные, так и однострочные варианты.
_FENCE_LANG_RE = re.compile(r"^[A-Za-z0-9_+-]+$")


def strip_markdown_code_fence(text: str) -> str:
    """Если ответ модели обёрнут в markdown-код-блок (```lang\\n…\\n```), возвращает
    содержимое без обёртки. Иначе возвращает исходный текст без изменений.

    Алгоритм:
    1) Удаляем ведущие/хвостовые пробелы и переводы строк.
    2) Если строка не начинается с ``` или не заканчивается ``` — возвращаем оригинал.
    3) Срезаем по 3 backtick'а с обеих сторон.
    4) Если первая строка содержимого — короткий идентификатор языка (json, jsonc,
       text, python, …), удаляем её.
    5) Возвращаем результат с обрезанными краевыми пробелами/переводами строк.

    Для текстов без markdown-обёртки функция полностью no-op (не повреждает данные).
    """
    if not text:
        return text
    s = text.strip()
    if not s.startswith("```") or not s.endswith("```") or len(s) < 6:
        return text
    inner = s[3:-3]
    nl = inner.find("\n")
    if nl >= 0:
        first_line = inner[:nl].strip()
        if first_line and _FENCE_LANG_RE.match(first_line):
            inner = inner[nl + 1:]
    return inner.strip("\r\n").rstrip()

CLAUDE_MODELS: list[dict[str, str]] = [
    {"id": "claude-opus-4-7", "label": "Claude Opus 4.7"},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
]

CLAUDE_MODEL_IDS: set[str] = {m["id"] for m in CLAUDE_MODELS}

# Максимум токенов на ответ — у Claude max_tokens обязателен и ограничивает выход.
# Под каждую модель ставим её известный практический максимум, чтобы длинные этапы
# (Block Writer / Scene Writer / YouTube Packaging) не обрезались.
# Источник: документация Anthropic / Kie.ai по соответствующим моделям.
CLAUDE_MAX_TOKENS_PER_MODEL: dict[str, int] = {
    "claude-opus-4-7": 32000,
    "claude-sonnet-4-6": 64000,
}

# Запасной вариант для неизвестных Claude-моделей (не должен использоваться при штатной работе).
CLAUDE_DEFAULT_MAX_TOKENS = 16384


def claude_max_tokens_for_model(model: str) -> int:
    return int(CLAUDE_MAX_TOKENS_PER_MODEL.get((model or "").strip(), CLAUDE_DEFAULT_MAX_TOKENS))


def is_claude_model(model: str) -> bool:
    return (model or "").strip() in CLAUDE_MODEL_IDS


def _kie_api_key() -> str:
    return (os.getenv("KEYAI_API_KEY") or os.getenv("KIE_API_KEY") or "").strip()


def kie_api_key_present() -> bool:
    return bool(_kie_api_key())


def _sanitize_for_json(s: str) -> str:
    """Убирает проблемные символы (lone surrogates, NUL), чтобы JSON всегда кодировался корректно."""
    t = (s or "").replace("\x00", " ")
    return t.encode("utf-8", "replace").decode("utf-8", "replace")


def _claude_json_body_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _claude_request_headers(api_key: str, *, accept_sse: bool = False) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json; charset=utf-8",
    }
    h["Accept"] = "text/event-stream" if accept_sse else "application/json"
    return h


def _claude_error_message(r: requests.Response) -> str:
    err_body = (r.text or "")[:2000]
    try:
        err_json = r.json()
        if isinstance(err_json, dict):
            em = err_json.get("error")
            if isinstance(em, dict) and em.get("message"):
                return str(em.get("message"))
            if err_json.get("message"):
                return str(err_json.get("message"))
            if err_json.get("msg"):
                return str(err_json.get("msg"))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return err_body or (r.reason or str(r.status_code))


def _extract_text_from_content_blocks(content_blocks: Any) -> str:
    if not isinstance(content_blocks, list):
        return ""
    parts: list[str] = []
    for blk in content_blocks:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text" and isinstance(blk.get("text"), str):
            parts.append(blk["text"])
    return "".join(parts)


def claude_messages_wire_payload(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Тело POST /claude/v1/messages — Anthropic-style."""
    model_id = (model or "").strip()
    payload: dict[str, Any] = {
        "model": model_id,
        "system": _sanitize_for_json((system_prompt or "").strip()),
        "messages": [
            {"role": "user", "content": _sanitize_for_json((user_content or "").strip())},
        ],
        "max_tokens": int(max_tokens) if max_tokens else claude_max_tokens_for_model(model_id),
    }
    if stream:
        payload["stream"] = True
    return payload


def _post_claude_messages_thread(
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
    out: queue.Queue,
) -> None:
    try:
        body = _claude_json_body_bytes(payload)
        r = requests.post(
            CLAUDE_API_URL,
            headers=_claude_request_headers(api_key),
            data=body,
            timeout=timeout,
        )
        out.put(("ok", r))
    except Exception as e:
        out.put(("err", e))


def post_claude_messages_sync(
    payload: dict[str, Any],
    timeout: int,
) -> tuple[str | None, str | None]:
    """Синхронный POST → (text_content, err)."""
    key = _kie_api_key()
    if not key:
        return None, "Не задан KEYAI_API_KEY в .env (нужен для Claude через Kie.ai)."
    try:
        body = _claude_json_body_bytes(payload)
        r = requests.post(
            CLAUDE_API_URL,
            headers=_claude_request_headers(key),
            data=body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        return None, f"Сеть / таймаут: {e}"
    if not r.ok:
        return None, _claude_error_message(r)
    try:
        body_json = r.json()
        text = _extract_text_from_content_blocks(body_json.get("content"))
        if not text:
            return None, "В ответе Claude нет текста (content[].text пуст)."
        return strip_markdown_code_fence(text), None
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "Неожиданная структура ответа Claude."


def iter_claude_completion(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    timeout: int,
) -> Iterator[dict[str, Any]]:
    """NDJSON-события для одношагового вызова Claude (без стрима)."""
    yield {"type": "status", "message": "Проверка ввода…"}
    key = _kie_api_key()
    if not key:
        yield {
            "type": "error",
            "message": "Не задан KEYAI_API_KEY в .env (нужен для Claude через Kie.ai).",
        }
        return
    if not (system_prompt or "").strip():
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return
    if not (user_content or "").strip():
        yield {"type": "error", "message": "Введите текст для обработки."}
        return

    yield {"type": "status", "message": f"Модель: {model} (Kie.ai → Claude)"}
    yield {
        "type": "status",
        "message": "Формирование запроса к Kie.ai (POST /claude/v1/messages)…",
    }

    payload = claude_messages_wire_payload(model, system_prompt, user_content, stream=False)

    yield {"type": "status", "message": "Отправка запроса в Kie.ai…"}

    out: queue.Queue = queue.Queue(maxsize=1)
    th = threading.Thread(
        target=_post_claude_messages_thread,
        args=(key, payload, timeout, out),
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
                    yield {
                        "type": "error",
                        "message": "Соединение с Kie.ai завершилось без ответа.",
                    }
                    return
            wait_round += 1
            yield {
                "type": "status",
                "message": "Ожидание ответа Kie.ai/Claude…",
            }

    if kind == "err":
        yield {"type": "error", "message": f"Сеть / таймаут: {data}"}
        return

    r = data
    yield {"type": "status", "message": f"Ответ HTTP {r.status_code}"}

    if not r.ok:
        yield {"type": "error", "message": _claude_error_message(r)}
        return

    yield {"type": "status", "message": "Разбор JSON ответа…"}
    try:
        body_json = r.json()
        text = _extract_text_from_content_blocks(body_json.get("content"))
        if not text:
            yield {"type": "error", "message": "В ответе Claude нет текста (content[].text пуст)."}
            return
    except (KeyError, IndexError, TypeError, ValueError) as e:
        yield {"type": "error", "message": f"Неожиданная структура ответа: {e}"}
        return

    yield {"type": "status", "message": "Готово."}
    yield {"type": "result", "content": strip_markdown_code_fence(text)}


def iter_claude_completion_stream(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    timeout: int,
) -> Iterator[dict[str, Any]]:
    """NDJSON-события для потокового (SSE) вызова Claude через Kie.ai."""
    yield {"type": "status", "message": "Проверка ввода…"}
    key = _kie_api_key()
    if not key:
        yield {
            "type": "error",
            "message": "Не задан KEYAI_API_KEY в .env (нужен для Claude через Kie.ai).",
        }
        return
    if not (system_prompt or "").strip():
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return
    if not (user_content or "").strip():
        yield {"type": "error", "message": "Введите текст для обработки."}
        return

    yield {"type": "status", "message": f"Модель: {model} (Kie.ai → Claude, stream)"}
    yield {"type": "status", "message": "Потоковый запрос к Kie.ai (SSE)…"}

    payload = claude_messages_wire_payload(model, system_prompt, user_content, stream=True)

    acc = ""
    try:
        body = _claude_json_body_bytes(payload)
        with requests.post(
            CLAUDE_API_URL,
            headers=_claude_request_headers(key, accept_sse=True),
            data=body,
            timeout=(30, timeout),
            stream=True,
        ) as r:
            if not r.ok:
                yield {"type": "error", "message": _claude_error_message(r)}
                return

            event_name = ""
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw if isinstance(raw, str) else str(raw)
                if not line.strip():
                    event_name = ""
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    chunk: dict[str, Any] = json.loads(data_str)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                typ = str(chunk.get("type") or event_name or "").strip()
                if typ == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                        piece = delta.get("text")
                        if piece is None:
                            continue
                        if not isinstance(piece, str):
                            piece = str(piece)
                        if not piece:
                            continue
                        acc += piece
                        yield {"type": "delta", "content": piece}
                elif typ == "message_stop":
                    break
                elif typ == "error":
                    err = chunk.get("error") or {}
                    msg = ""
                    if isinstance(err, dict):
                        msg = str(err.get("message") or "")
                    yield {"type": "error", "message": msg or "Ошибка стрима Claude."}
                    return
    except requests.RequestException as e:
        yield {"type": "error", "message": f"Сеть / таймаут: {e}"}
        return

    if not acc.strip():
        yield {"type": "error", "message": "Пустой ответ в потоке Claude (нет текста)."}
        return

    yield {"type": "status", "message": "Готово."}
    yield {"type": "result", "content": strip_markdown_code_fence(acc)}
