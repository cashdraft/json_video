"""Загрузка фото для /overlay-text."""

from __future__ import annotations

import uuid
from pathlib import Path

from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
OVERLAY_UPLOADS_DIR = BASE_DIR / "data" / "overlay_text" / "uploads"
ALLOWED_UPLOAD_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def save_overlay_upload(
    data: bytes,
    filename: str,
    public_base: str,
) -> tuple[str | None, str | None]:
    if not data:
        return None, "Пустой файл."
    if len(data) > MAX_UPLOAD_BYTES:
        return None, f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ."
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        ext = ".jpg"
    OVERLAY_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    safe = secure_filename(Path(filename or "upload").stem)[:40] or "upload"
    out_name = f"{stem}_{safe}{ext}"
    path = OVERLAY_UPLOADS_DIR / out_name
    path.write_bytes(data)
    base = (public_base or "").strip().rstrip("/")
    if not base:
        return None, "Не задан PUBLIC_BASE_URL — модель не сможет скачать фото."
    return f"{base}/overlay-text/media/{out_name}", None
