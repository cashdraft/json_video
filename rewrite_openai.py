"""
ReWrite Master — вызов OpenAI Chat Completions (system = промпт, user = текст).
"""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Iterator
from typing import Any

import requests

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

REWRITE_MODELS: list[dict[str, str]] = [
    {"id": "gpt-4.1", "label": "GPT-4.1"},
]

REWRITE_MODEL_IDS = {m["id"] for m in REWRITE_MODELS}

REWRITE_DEFAULT_MODEL = "gpt-4.1"


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


def _post_chat_completion(api_key: str, payload: dict[str, Any], timeout: int, out: queue.Queue) -> None:
    try:
        r = requests.post(
            OPENAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
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
) -> Iterator[dict[str, Any]]:
    """
    Yields события для NDJSON-стрима:
    {"type": "status", "message": "..."}
    {"type": "error", "message": "..."} — последнее при ошибке
    {"type": "result", "content": "..."} — успех
    """
    prompt = (prompt or "").strip()
    text = (text or "").strip()
    model = normalize_rewrite_model(model)
    if timeout is None:
        timeout = _chat_timeout_seconds()

    yield {"type": "status", "message": "Проверка ввода…"}
    if not api_key.strip():
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY в .env"}
        return
    if not prompt:
        yield {"type": "error", "message": "Введите промпт (инструкцию для модели)."}
        return
    if not text:
        yield {"type": "error", "message": "Введите текст для обработки."}
        return

    yield {"type": "status", "message": f"Модель: {model}"}
    yield {"type": "status", "message": "Формирование запроса к OpenAI (system + user)…"}

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.7,
    }

    yield {"type": "status", "message": "Отправка chat/completions на api.openai.com…"}
    yield {
        "type": "status",
        "message": "Пока OpenAI считает ответ, статус будет обновляться каждые ~8 с (это не зависание).",
    }

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
                    f"Ожидание ответа OpenAI… ~{wait_round * 8} с. "
                    "Тяжёлые модели могут отвечать несколько минут."
                ),
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
