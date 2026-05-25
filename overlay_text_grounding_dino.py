"""Вызов Grounding DINO для Remotion Preview Agent (шаг после dino_prompt)."""

from __future__ import annotations

import json
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from werkzeug.utils import secure_filename

from overlay_text_upload import OVERLAY_UPLOADS_DIR

DEFAULT_GROUNDING_DINO_URL = os.environ.get("GROUNDING_DINO_URL", "http://127.0.0.1:8010").rstrip("/")
MEDIA_PATH_RE = re.compile(r"/overlay-text/media/([^?#]+)$", re.I)


def extract_dino_prompt(rp_result: str, explicit: str = "") -> tuple[str, str | None]:
    """Достать dino_prompt из Result агента (JSON) или из явной строки."""
    raw_explicit = (explicit or "").strip()
    if raw_explicit:
        return raw_explicit, None

    text = (rp_result or "").strip()
    if not text:
        return "", "В Result нет JSON — сначала запустите Remotion Preview Agent (↻)."

    payload = text
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if fence:
            payload = fence.group(1).strip()
        else:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                payload = text[start : end + 1]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return "", (
                f"Result не JSON с полем dino_prompt: {exc}. "
                "Сначала ↻ Remotion Preview Agent (не обычный чат-ответ)."
            )

    if not isinstance(data, dict):
        return "", "Result должен быть JSON-объектом с полем dino_prompt."

    prompt = data.get("dino_prompt") or data.get("prompt") or data.get("queries")
    if isinstance(prompt, list):
        prompt = ". ".join(str(p) for p in prompt if str(p).strip())
    prompt = str(prompt or "").strip()
    if not prompt:
        return "", "В Result нет поля dino_prompt."
    return prompt, None


def _local_upload_path(image_url: str) -> Path | None:
    url = (image_url or "").strip()
    if not url:
        return None
    m = MEDIA_PATH_RE.search(url)
    if m:
        name = secure_filename(Path(m.group(1)).name)
        if name:
            p = (OVERLAY_UPLOADS_DIR / name).resolve()
            root = OVERLAY_UPLOADS_DIR.resolve()
            if str(p).startswith(str(root)) and p.is_file():
                return p
    parsed = urlparse(url)
    if parsed.path:
        m2 = MEDIA_PATH_RE.search(parsed.path)
        if m2:
            name = secure_filename(Path(m2.group(1)).name)
            if name:
                p = (OVERLAY_UPLOADS_DIR / name).resolve()
                root = OVERLAY_UPLOADS_DIR.resolve()
                if str(p).startswith(str(root)) and p.is_file():
                    return p
    return None


def _load_image_bytes(image_url: str) -> tuple[bytes | None, str | None]:
    path = _local_upload_path(image_url)
    if path:
        return path.read_bytes(), None
    url = (image_url or "").strip()
    if not url:
        return None, "Нет URL фото Remotion Preview Agent."
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content, None
    except Exception as exc:
        return None, f"Не удалось загрузить фото: {exc}"


def simplify_dino_payload(payload: dict[str, Any]) -> dict[str, Any]:
    detections: list[dict[str, Any]] = []
    for item in payload.get("detections") or []:
        if not isinstance(item, dict):
            continue
        box = item.get("box_px") if isinstance(item.get("box_px"), dict) else {}
        detections.append(
            {
                "label": item.get("label"),
                "score": item.get("score"),
                "box_px": {
                    "x1": box.get("x1"),
                    "y1": box.get("y1"),
                    "x2": box.get("x2"),
                    "y2": box.get("y2"),
                },
            }
        )
    return {
        "image_width": payload.get("image_width"),
        "image_height": payload.get("image_height"),
        "prompt": payload.get("prompt"),
        "detections": detections,
    }


def run_grounding_dino_for_remotion_preview(
    *,
    image_url: str,
    dino_prompt: str = "",
    rp_result: str = "",
    box_threshold: float = 0.25,
    text_threshold: float = 0.25,
    service_url: str | None = None,
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """
    Returns (result_dict, log_lines, error).
    """
    log: list[str] = []
    base = (service_url or DEFAULT_GROUNDING_DINO_URL).rstrip("/")

    prompt, perr = extract_dino_prompt(rp_result, dino_prompt)
    if perr:
        return None, log, perr
    log.append("источник: Result → ключ dino_prompt")
    log.append(f"dino_prompt: {prompt}")

    img_bytes, ierr = _load_image_bytes(image_url)
    if ierr or not img_bytes:
        return None, log, ierr or "Пустое изображение."

    log.append(f"image: {len(img_bytes)} bytes")

    try:
        hr = requests.get(f"{base}/health", timeout=5)
        if hr.ok:
            h = hr.json()
            log.append(f"Grounding DINO: {h.get('status', '?')} · {h.get('device', '?')}")
        else:
            return None, log, f"Grounding DINO недоступен (health HTTP {hr.status_code}). Запустите сервис на {base}."
    except Exception as exc:
        return None, log, f"Grounding DINO не отвечает ({base}): {exc}"

    log.append("Запрос /detect…")
    files = {"image": ("frame.jpg", BytesIO(img_bytes), "image/jpeg")}
    data = {
        "prompt": prompt,
        "box_threshold": str(box_threshold),
        "text_threshold": str(text_threshold),
    }
    try:
        dr = requests.post(f"{base}/detect", files=files, data=data, timeout=600)
    except Exception as exc:
        return None, log, f"Ошибка запроса к Grounding DINO: {exc}"

    if not dr.ok:
        detail = dr.text[:500]
        try:
            j = dr.json()
            detail = j.get("detail") or j.get("error") or detail
        except Exception:
            pass
        return None, log, f"Grounding DINO HTTP {dr.status_code}: {detail}"

    try:
        raw = dr.json()
    except json.JSONDecodeError:
        return None, log, "Grounding DINO вернул не-JSON."

    simplified = simplify_dino_payload(raw)
    n = len(simplified.get("detections") or [])
    log.append(f"Готово: {n} detection(s).")
    return simplified, log, None
