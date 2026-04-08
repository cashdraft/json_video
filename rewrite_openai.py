"""
ReWrite Master — вызов OpenAI Chat Completions (system = промпт, user = текст).
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

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

REWRITE_MODELS: list[dict[str, str]] = [
    {"id": "gpt-4.1", "label": "GPT-4.1"},
]

REWRITE_MODEL_IDS = {m["id"] for m in REWRITE_MODELS}

REWRITE_DEFAULT_MODEL = "gpt-4.1"

REWRITE_CHAT_TEMPERATURE = 0.3


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
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=payload,
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
        "temperature": REWRITE_CHAT_TEMPERATURE,
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


def iter_rewrite_completion_stream(
    api_key: str,
    model: str,
    prompt: str,
    text: str,
    *,
    timeout: int | None = None,
) -> Iterator[dict[str, Any]]:
    """
    Поток chat/completions (stream=true). События для NDJSON:
    {"type": "status", "message": "..."}
    {"type": "delta", "content": "..."} — фрагмент ответа (подряд склеиваются в полный текст)
    {"type": "error", "message": "..."}
    {"type": "result", "content": "..."} — полный накопленный ответ в конце
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
    yield {"type": "status", "message": "Потоковый запрос к OpenAI (stream)…"}

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        "temperature": REWRITE_CHAT_TEMPERATURE,
        "stream": True,
    }

    acc = ""
    try:
        with requests.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=payload,
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
    except requests.RequestException as e:
        yield {"type": "error", "message": f"Сеть / таймаут: {e}"}
        return

    if not acc.strip():
        yield {"type": "error", "message": "Пустой ответ в потоке (нет текста от модели)."}
        return

    yield {"type": "status", "message": "Готово."}
    yield {"type": "result", "content": acc}


def _post_chat_completion_sync(
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[str | None, str | None]:
    try:
        r = requests.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=payload,
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
        try:
            tmin = int(b.get("target_chars_min"))
            tideal = int(b.get("target_chars_ideal"))
            tmax = int(b.get("target_chars_max"))
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


def iter_draft1_blockwise_completion(
    api_key: str,
    model: str,
    system_prompt: str,
    analysis_result: str,
    structure_result: str,
    *,
    timeout: int | None = None,
    max_attempts_per_block: int = 7,
) -> Iterator[dict[str, Any]]:
    """Draft1: по одному блоку. Принятие строго при target_chars_min <= len <= target_chars_max.

    После исчерпания попыток: из всех успешно распарсенных вариантов берём тот, чья длина ближе всего к
    target_chars_ideal, и помечаем принудительный приём.
    """
    model = normalize_rewrite_model(model)
    system_prompt = (system_prompt or "").strip()
    analysis_result = (analysis_result or "").strip()
    structure_result = (structure_result or "").strip()
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
        yield {"type": "error", "message": "Structure Result должен быть JSON с массивом blocks и target_chars_*."}
        return

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
        accepted_so_far = "\n\n".join(accepted).strip() or "(пока пусто)"
        feedback = ""
        accepted_this_block = False
        parsed_candidates: list[tuple[str, int]] = []
        for attempt in range(1, max_attempts_per_block + 1):
            yield {"type": "status", "message": f"Блок {idx}/{len(blocks)}: попытка {attempt}…"}
            user_msg = (
                "--- Analysis Result ---\n"
                + (analysis_result or "(пусто)")
                + "\n\n--- Structure Result ---\n"
                + (structure_result or "(пусто)")
                + "\n\n--- Already accepted blocks (DO NOT REWRITE) ---\n"
                + accepted_so_far
                + "\n\n--- Task ---\n"
                + f"Write ONLY block {idx} in this exact format:\n"
                + f"BLOCK_START: {idx}\n"
                + f"BLOCK_NAME: {name}\n"
                + "<block_text>\n"
                + f"BLOCK_END: {idx}\n\n"
                + "Rules:\n"
                + f"- target_chars_min={tmin}\n- target_chars_ideal={tideal}\n- target_chars_max={tmax}\n"
                + "- Do not output SCRIPT_END.\n"
                + "- Do not output other blocks.\n"
                + "- Do not include any text before BLOCK_START or after BLOCK_END.\n"
                + "must_cover:\n"
                + must_cover
                + "\nmust_not_cover:\n"
                + must_not_cover
            )
            if feedback:
                user_msg += "\n\nRewrite request:\n" + feedback
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": REWRITE_CHAT_TEMPERATURE,
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
                            f"Блок {idx}: ожидание ответа OpenAI… ~{wait_round * 8} с "
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
