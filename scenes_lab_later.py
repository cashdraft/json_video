"""API для блока Later… на странице /scenes-lab (Claude Kie + OpenAI GPT + одно фото)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import requests

from claude_kie import (
    CLAUDE_MODELS,
    claude_messages_wire_payload,
    is_claude_model,
    kie_api_key_present,
    post_claude_messages_sync,
)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GPT_LATER_MODEL_ID = "gpt-5.4"
OPENAI_LATER_MODELS: list[dict[str, str]] = [
    {"id": GPT_LATER_MODEL_ID, "label": "ChatGPT 5.4"},
]
OPENAI_LATER_MODEL_IDS: set[str] = {m["id"] for m in OPENAI_LATER_MODELS}
LATER_CHAT_TEMPERATURE = 0.1
from werkzeug.utils import secure_filename

from later_anim_dictionary import (
    anim_dictionary_backlog_prompt_line,
    anim_dictionary_prompt_block,
)

BASE_DIR = Path(__file__).resolve().parent
SCENES_LAB_UPLOADS_DIR = BASE_DIR / "data" / "scenes_lab_uploads"
ALLOWED_UPLOAD_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

def _build_later_response_contract() -> str:
    anim_block = anim_dictionary_prompt_block()
    backlog = anim_dictionary_backlog_prompt_line()
    backlog_note = (
        f"Доп. заявки в backlog ({backlog}) — только в NOTES."
        if backlog
        else ""
    )
    return f"""
Что обязан вернуть (ровно три блока, без Remotion-кода).

ЗАПРЕЩЕНО: markdown-код-фенсы ``` в ответе. Только маркеры ===…===, между ними — чистый SVG или чистый JSON без backtick.

1) Разметка SVG:
===SVG_START===
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080">...</svg>
===SVG_END===
Между маркерами — только XML, первая строка начинается с <svg.

Подписи — ТОЛЬКО валидные <text>, никогда голый id= без тега. Пример:
<g id="word-1">
  <text id="t-word-1" x="660" y="400" font-size="24">VOYAGER</text>
</g>
ЗАПРЕЩЕНО: id="t-word-1" x="660" y="400">VOYAGER (без <text> и </text>).

2) Лист анимации — один JSON-объект, без ```:
===ANIM_START===
{{"duration_sec": 8.62, "fps": 30, "tracks": [{{"id": "word-1", "anim": "fade-in", "start": 0, "end": 12}}]}}
===ANIM_END===
tracks[].id — id группы <g> в SVG. anim — ТОЛЬКО из словаря ниже. end ≤ duration_sec × fps.

{anim_block}

3) Пояснение:
===NOTES_START===
...текст. {backlog_note}
===NOTES_END===

Сервер при разборе сам чинит пропущенные <text> и снимает ``` внутри маркеров; всё равно отдавай валидный XML.
""".strip()


LATER_RESPONSE_CONTRACT = _build_later_response_contract()

DEFAULT_LATER_SYSTEM_PROMPT = (
    "Ты генерируешь инфографику для Remotion: SVG-разметка + JSON таймлайна + краткое пояснение. "
    "В запросе пользователя может быть референс-изображение — обязательно учти его палитру, "
    "типографику и настроение; не выдумывай стиль, если картинка приложена. "
    "Не пиши, что изображение не пришло, пока не убедился, что в сообщении нет блока image. "
    "Язык пояснения — как у пользователя.\n\n"
    + LATER_RESPONSE_CONTRACT
)


def later_models_for_ui() -> list[dict[str, str]]:
    """Список моделей для select на /scenes-lab."""
    return list(CLAUDE_MODELS) + list(OPENAI_LATER_MODELS)


def claude_models_for_ui() -> list[dict[str, str]]:
    return later_models_for_ui()


def is_openai_later_model(model: str) -> bool:
    return (model or "").strip() in OPENAI_LATER_MODEL_IDS


def openai_api_key_present() -> bool:
    return bool((os.getenv("OPENAI_API_KEY") or "").strip())


def later_api_ready() -> bool:
    return kie_api_key_present() or openai_api_key_present()


def _openai_api_key() -> str:
    return (os.getenv("OPENAI_API_KEY") or "").strip()


def _sanitize_for_openai_json(s: str) -> str:
    t = (s or "").replace("\x00", " ")
    return t.encode("utf-8", "replace").decode("utf-8", "replace")


def _openai_chat_timeout() -> int:
    raw = (os.getenv("OPENAI_CHAT_TIMEOUT") or "").strip()
    if not raw:
        return 600
    try:
        return max(60, min(int(raw), 3600))
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


def openai_vision_wire_payload(
    model: str,
    system_prompt: str,
    user_text: str,
    image_url: str,
) -> dict[str, Any]:
    img = str(image_url or "").strip()
    user_content: list[dict[str, Any]] | str
    if img:
        user_content = [
            {"type": "text", "text": _sanitize_for_openai_json(user_text)},
            {
                "type": "image_url",
                "image_url": {"url": img, "detail": "high"},
            },
        ]
    else:
        user_content = _sanitize_for_openai_json(user_text)
    return {
        "model": (model or GPT_LATER_MODEL_ID).strip(),
        "temperature": LATER_CHAT_TEMPERATURE,
        "messages": [
            {"role": "system", "content": _sanitize_for_openai_json(system_prompt)},
            {"role": "user", "content": user_content},
        ],
    }


def post_openai_chat_sync(payload: dict[str, Any], timeout: int | None = None) -> tuple[str | None, str | None]:
    api_key = _openai_api_key()
    if not api_key:
        return None, "Не задан OPENAI_API_KEY в .env"
    if timeout is None:
        timeout = _openai_chat_timeout()
    try:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        r = requests.post(
            OPENAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        return None, f"Сеть / таймаут: {e}"
    if not r.ok:
        return None, _openai_error_message(r)
    try:
        data = r.json()
        choice0 = (data.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        content = msg.get("content")
        if content is None:
            return None, "В ответе нет choices[0].message.content"
        if not isinstance(content, str):
            content = str(content)
        return content, None
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None, "Неожиданная структура ответа OpenAI."


def save_scenes_lab_upload(
    data: bytes,
    filename: str,
    public_base: str,
) -> tuple[str | None, str | None]:
    """Сохраняет файл и возвращает (public_url, error)."""
    if not data:
        return None, "Пустой файл."
    if len(data) > MAX_UPLOAD_BYTES:
        return None, f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ."
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        ext = ".jpg"
    SCENES_LAB_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    safe = secure_filename(Path(filename or "upload").stem)[:40] or "upload"
    out_name = f"{stem}_{safe}{ext}"
    path = SCENES_LAB_UPLOADS_DIR / out_name
    path.write_bytes(data)
    base = (public_base or "").strip().rstrip("/")
    if not base:
        return None, "Не задан PUBLIC_BASE_URL — Kie не сможет скачать загруженное фото."
    return f"{base}/scenes-lab/media/{out_name}", None


def _build_later_user_body(
    *,
    user_prompt: str,
    scene_text: str = "",
    scene_text_ru: str = "",
) -> str:
    parts: list[str] = []
    if (scene_text or "").strip():
        parts.append(f"Текст сцены (EN):\n{scene_text.strip()}")
    if (scene_text_ru or "").strip():
        parts.append(f"Текст сцены (RU):\n{scene_text_ru.strip()}")
    up = (user_prompt or "").strip()
    if up:
        parts.append(f"Запрос пользователя:\n{up}")
    else:
        parts.append(
            "Опиши изображение и сгенерируй инфографику по контракту (SVG + JSON анимации + NOTES)."
        )
    return "\n\n".join(parts)


def run_later_model_request(
    *,
    model: str,
    user_prompt: str,
    image_url: str,
    scene_text: str = "",
    scene_text_ru: str = "",
    system_prompt: str = "",
) -> tuple[str | None, str | None]:
    """Синхронный запрос к выбранной модели. Возвращает (answer_text, error)."""
    mid = (model or "").strip()
    img = (image_url or "").strip()
    if not img:
        return None, "Прикрепите фото (или используйте кадр Start)."
    user_body = _build_later_user_body(
        user_prompt=user_prompt,
        scene_text=scene_text,
        scene_text_ru=scene_text_ru,
    )
    sys_p = (system_prompt or "").strip() or DEFAULT_LATER_SYSTEM_PROMPT

    if is_openai_later_model(mid):
        if not openai_api_key_present():
            return None, "Не задан OPENAI_API_KEY в .env (ChatGPT)."
        payload = openai_vision_wire_payload(mid, sys_p, user_body, img)
        return post_openai_chat_sync(payload, timeout=300)

    if not kie_api_key_present():
        return None, "Не задан KEYAI_API_KEY в .env (Kie.ai / Claude)."
    if not is_claude_model(mid):
        mid = CLAUDE_MODELS[0]["id"]
    payload = claude_messages_wire_payload(
        mid,
        sys_p,
        user_body,
        image_url=img,
        stream=False,
    )
    return post_claude_messages_sync(payload, timeout=300)


def run_later_claude_request(
    *,
    model: str,
    user_prompt: str,
    image_url: str,
    scene_text: str = "",
    scene_text_ru: str = "",
    system_prompt: str = "",
) -> tuple[str | None, str | None]:
    return run_later_model_request(
        model=model,
        user_prompt=user_prompt,
        image_url=image_url,
        scene_text=scene_text,
        scene_text_ru=scene_text_ru,
        system_prompt=system_prompt,
    )
