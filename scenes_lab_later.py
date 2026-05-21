"""API для блока Later… на странице /scenes-lab (Claude через Kie + одно фото)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from claude_kie import (
    CLAUDE_MODEL_IDS,
    CLAUDE_MODELS,
    claude_messages_wire_payload,
    is_claude_model,
    kie_api_key_present,
    post_claude_messages_sync,
)
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


def claude_models_for_ui() -> list[dict[str, str]]:
    return list(CLAUDE_MODELS)


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


def run_later_claude_request(
    *,
    model: str,
    user_prompt: str,
    image_url: str,
    scene_text: str = "",
    scene_text_ru: str = "",
    system_prompt: str = "",
) -> tuple[str | None, str | None]:
    """Синхронный запрос к Claude. Возвращает (answer_text, error)."""
    if not kie_api_key_present():
        return None, "Не задан KEYAI_API_KEY в .env (Kie.ai / Claude)."
    mid = (model or "").strip()
    if not is_claude_model(mid):
        mid = CLAUDE_MODELS[0]["id"]
    img = (image_url or "").strip()
    if not img:
        return None, "Прикрепите фото (или используйте кадр Start)."
    parts: list[str] = []
    if (scene_text or "").strip():
        parts.append(f"Текст сцены (EN):\n{scene_text.strip()}")
    if (scene_text_ru or "").strip():
        parts.append(f"Текст сцены (RU):\n{scene_text_ru.strip()}")
    up = (user_prompt or "").strip()
    if up:
        parts.append(f"Запрос пользователя:\n{up}")
    else:
        parts.append("Опиши изображение и предложи, что можно сделать с этим кадром для следующего шага видео.")
    user_body = "\n\n".join(parts)
    sys_p = (system_prompt or "").strip() or DEFAULT_LATER_SYSTEM_PROMPT
    payload = claude_messages_wire_payload(
        mid,
        sys_p,
        user_body,
        image_url=img,
        stream=False,
    )
    return post_claude_messages_sync(payload, timeout=300)
