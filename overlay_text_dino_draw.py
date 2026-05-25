"""Обводка bbox из Result from DINO на исходном фото (Pillow)."""

from __future__ import annotations

import json
import math
import re
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from overlay_text_grounding_dino import _load_image_bytes
from overlay_text_upload import OVERLAY_UPLOADS_DIR

# Разные цвета для каждого detection (RGB)
BOX_COLORS: list[tuple[int, int, int]] = [
    (255, 77, 109),
    (77, 196, 255),
    (119, 255, 102),
    (255, 200, 61),
    (186, 120, 255),
    (255, 130, 210),
    (64, 230, 210),
    (255, 168, 102),
    (140, 255, 140),
    (102, 153, 255),
    (255, 102, 178),
    (210, 255, 77),
]


def parse_dino_result_payload(text: str) -> tuple[dict[str, Any] | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "Result from DINO пуст."

    payload = raw
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.I)
        if fence:
            payload = fence.group(1).strip()
        else:
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                payload = raw[start : end + 1]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, f"Result from DINO не JSON: {exc}"

    if not isinstance(data, dict):
        return None, "Ожидается JSON-объект с detections."
    return data, None


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    fill: tuple[int, int, int],
    width: int = 2,
    dash: tuple[int, int] = (7, 5),
) -> None:
    x0, y0 = start
    x1, y1 = end
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1:
        return
    dx = (x1 - x0) / length
    dy = (y1 - y0) / length
    pos = 0.0
    draw_on = True
    while pos < length:
        seg_len = (dash[0] if draw_on else dash[1])
        seg = min(seg_len, length - pos)
        if draw_on:
            sx, sy = x0 + dx * pos, y0 + dy * pos
            ex, ey = x0 + dx * (pos + seg), y0 + dy * (pos + seg)
            draw.line([(sx, sy), (ex, ey)], fill=fill, width=width)
        pos += seg
        draw_on = not draw_on


def _dashed_rectangle(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    width: int = 2,
) -> None:
    x1, y1, x2, y2 = box
    _dashed_line(draw, (x1, y1), (x2, y1), color, width)
    _dashed_line(draw, (x2, y1), (x2, y2), color, width)
    _dashed_line(draw, (x2, y2), (x1, y2), color, width)
    _dashed_line(draw, (x1, y2), (x1, y1), color, width)


def _clamp_box(box: dict[str, Any], w: int, h: int) -> tuple[int, int, int, int] | None:
    try:
        x1 = int(round(float(box.get("x1", 0))))
        y1 = int(round(float(box.get("y1", 0))))
        x2 = int(round(float(box.get("x2", 0))))
        y2 = int(round(float(box.get("y2", 0))))
    except (TypeError, ValueError):
        return None
    x1 = max(0, min(x1, w - 1))
    y1 = max(0, min(y1, h - 1))
    x2 = max(0, min(x2, w - 1))
    y2 = max(0, min(y2, h - 1))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _load_font(size: int = 11) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_dino_detections(
    image_bytes: bytes,
    dino_payload: dict[str, Any],
) -> tuple[bytes | None, str | None]:
    """Рисует прерывистые bbox + подписи; возвращает JPEG bytes."""
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        return None, f"Не удалось открыть изображение: {exc}"

    w, h = img.size
    draw = ImageDraw.Draw(img)
    font = _load_font(11)

    detections = dino_payload.get("detections") or []
    if not isinstance(detections, list) or not detections:
        return None, "В Result from DINO нет detections."

    drawn = 0
    for idx, item in enumerate(detections):
        if not isinstance(item, dict):
            continue
        box_raw = item.get("box_px")
        if not isinstance(box_raw, dict):
            continue
        rect = _clamp_box(box_raw, w, h)
        if not rect:
            continue

        color = BOX_COLORS[idx % len(BOX_COLORS)]
        _dashed_rectangle(draw, rect, color, width=2)

        label = str(item.get("label") or "?").strip()
        score = item.get("score")
        if score is not None:
            try:
                label = f"{label} {float(score):.2f}"
            except (TypeError, ValueError):
                pass

        x1, y1, x2, y2 = rect
        tw, th = _text_size(draw, label, font)
        pad = 2
        # подпись в верхнем левом углу бокса (чуть выше, иначе внутри)
        ty = y1 - th - pad * 2
        if ty < 0:
            ty = y1 + pad
        tx = x1 + pad
        if tx + tw + pad * 2 > w:
            tx = max(0, w - tw - pad * 2)
        bg = (0, 0, 0)
        draw.rectangle([tx - pad, ty - pad, tx + tw + pad, ty + th + pad], fill=bg)
        draw.text((tx, ty), label, fill=color, font=font)
        drawn += 1

    if drawn == 0:
        return None, "Нет валидных box_px для отрисовки."

    out = BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue(), None


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    return draw.textsize(text, font=font)  # type: ignore[attr-defined]


def save_annotated_overlay(
    jpeg_bytes: bytes,
    public_base: str,
) -> tuple[str | None, str | None]:
    if not jpeg_bytes:
        return None, "Пустой результат отрисовки."
    OVERLAY_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"dino_ann_{uuid.uuid4().hex}.jpg"
    path = OVERLAY_UPLOADS_DIR / name
    path.write_bytes(jpeg_bytes)
    base = (public_base or "").strip().rstrip("/")
    if not base:
        return None, "Не задан PUBLIC_BASE_URL."
    return f"{base}/overlay-text/media/{name}", None


def annotate_image_from_dino_result(
    *,
    image_url: str,
    rp_dino_result: str,
    public_base: str,
) -> tuple[str | None, list[str], str | None]:
    """
    Returns (annotated_url, log_lines, error).
    """
    log: list[str] = []
    payload, perr = parse_dino_result_payload(rp_dino_result)
    if perr or not payload:
        return None, log, perr or "Пустой Result from DINO."

    n = len(payload.get("detections") or [])
    log.append(f"detections: {n}")

    img_bytes, ierr = _load_image_bytes(image_url)
    if ierr or not img_bytes:
        return None, log, ierr or "Не удалось загрузить фото."

    log.append(f"image: {len(img_bytes)} bytes")

    out_bytes, derr = draw_dino_detections(img_bytes, payload)
    if derr or not out_bytes:
        return None, log, derr or "Отрисовка не удалась."

    url, serr = save_annotated_overlay(out_bytes, public_base)
    if serr or not url:
        return None, log, serr or "Не удалось сохранить файл."

    log.append(f"готово: {url}")
    return url, log, None
