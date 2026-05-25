"""Вызов SAM2 /segment_dino для Remotion Preview (DINO detections → masks)."""

from __future__ import annotations

import json
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

from overlay_text_grounding_dino import _load_image_bytes
from overlay_text_upload import OVERLAY_UPLOADS_DIR

DEFAULT_SAM2_URL = os.environ.get("SAM2_URL", "http://127.0.0.1:8011").rstrip("/")
SAM2_SERVICE_ROOT = Path(__file__).resolve().parent / "sam2_service"


def _copy_preview_to_uploads(
    preview_rel: str,
    public_base: str,
    *,
    name_prefix: str = "sam2_preview",
) -> tuple[str | None, str | None]:
    rel = (preview_rel or "").strip().lstrip("/")
    if not rel:
        return None, "Нет preview_path в ответе SAM2."
    src = (SAM2_SERVICE_ROOT / rel).resolve()
    root = SAM2_SERVICE_ROOT.resolve()
    if not str(src).startswith(str(root)) or not src.is_file():
        return None, f"Preview не найден на диске: {rel}"

    OVERLAY_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() or ".png"
    name = f"{name_prefix}_{uuid.uuid4().hex}{ext}"
    dest = OVERLAY_UPLOADS_DIR / name
    dest.write_bytes(src.read_bytes())

    base = (public_base or "").strip().rstrip("/")
    if not base:
        return None, "Не задан PUBLIC_BASE_URL."
    return f"{base}/overlay-text/media/{name}", None


def _sam2_health(base: str, log: list[str]) -> str | None:
    try:
        hr = requests.get(f"{base}/health", timeout=5)
        if hr.ok:
            h = hr.json()
            log.append(f"SAM2: {h.get('status', '?')} · {h.get('device', '?')}")
            return None
        return f"SAM2 недоступен (health HTTP {hr.status_code}). Запустите сервис на {base}."
    except Exception as exc:
        return f"SAM2 не отвечает ({base}): {exc}"


def run_sam2_segment_for_remotion_preview(
    *,
    image_url: str,
    rp_dino_result: str,
    min_score: float = 0.35,
    service_url: str | None = None,
    public_base: str = "",
) -> tuple[dict[str, Any] | None, list[str], str | None, str | None]:
    """
    Returns (result_dict, log_lines, preview_url, error).
    Uses original Remotion Preview photo — not DINO annotated image.
    """
    log: list[str] = []
    base = (service_url or DEFAULT_SAM2_URL).rstrip("/")

    dino_text = (rp_dino_result or "").strip()
    if not dino_text:
        return None, log, None, "Сначала получите Result from DINO (↻)."

    img_bytes, ierr = _load_image_bytes(image_url)
    if ierr or not img_bytes:
        return None, log, None, ierr or "Не удалось загрузить исходное фото Remotion Preview."

    log.append(f"image (original): {len(img_bytes)} bytes")
    log.append(f"image_url: {image_url}")

    herr = _sam2_health(base, log)
    if herr:
        return None, log, None, herr

    log.append("Запрос /segment_dino… (bbox от DINO → box prompt SAM2)")
    files = {"image": ("frame.jpg", BytesIO(img_bytes), "image/jpeg")}
    data = {
        "detections_json": dino_text,
        "min_score": str(min_score),
    }
    try:
        sr = requests.post(f"{base}/segment_dino", files=files, data=data, timeout=1800)
    except Exception as exc:
        return None, log, None, f"Ошибка запроса к SAM2: {exc}"

    if not sr.ok:
        detail = sr.text[:500]
        try:
            j = sr.json()
            detail = j.get("detail") or j.get("error") or detail
        except Exception:
            pass
        return None, log, None, f"SAM2 HTTP {sr.status_code}: {detail}"

    try:
        raw = sr.json()
    except json.JSONDecodeError:
        return None, log, None, "SAM2 вернул не-JSON."

    merged = raw.get("merged_occupied") or {}
    preview_rel = merged.get("preview_path") if isinstance(merged, dict) else None
    preview_url, perr = _copy_preview_to_uploads(str(preview_rel or ""), public_base)
    if perr:
        log.append(f"preview copy: {perr}")

    n_items = len(raw.get("items") or [])
    n_skip = len(raw.get("skipped") or [])
    log.append(f"Готово: {n_items} mask(s), skipped {n_skip}.")
    if preview_url:
        log.append(f"preview: {preview_url}")

    clean = {k: v for k, v in raw.items() if k not in ("ok", "log")}
    return clean, log, preview_url, None


def run_sam2_auto_for_remotion_preview(
    *,
    image_url: str,
    service_url: str | None = None,
    public_base: str = "",
) -> tuple[dict[str, Any] | None, list[str], str | None, str | None]:
    """
    SAM2 automatic masks — без DINO detections.
    Returns (result_dict, log_lines, preview_url, error).
    """
    log: list[str] = []
    base = (service_url or DEFAULT_SAM2_URL).rstrip("/")

    img_bytes, ierr = _load_image_bytes(image_url)
    if ierr or not img_bytes:
        return None, log, None, ierr or "Не удалось загрузить исходное фото Remotion Preview."

    log.append(f"image (original): {len(img_bytes)} bytes")
    log.append(f"image_url: {image_url}")
    log.append("режим: SAM2 auto (сетка точек, без DINO bbox)")

    herr = _sam2_health(base, log)
    if herr:
        return None, log, None, herr

    log.append("Запрос /segment_auto…")
    files = {"image": ("frame.jpg", BytesIO(img_bytes), "image/jpeg")}
    try:
        sr = requests.post(f"{base}/segment_auto", files=files, timeout=3600)
    except Exception as exc:
        return None, log, None, f"Ошибка запроса к SAM2: {exc}"

    if not sr.ok:
        detail = sr.text[:500]
        try:
            j = sr.json()
            detail = j.get("detail") or j.get("error") or detail
        except Exception:
            pass
        return None, log, None, f"SAM2 HTTP {sr.status_code}: {detail}"

    try:
        raw = sr.json()
    except json.JSONDecodeError:
        return None, log, None, "SAM2 вернул не-JSON."

    merged = raw.get("merged_occupied") or {}
    preview_rel = merged.get("preview_path") if isinstance(merged, dict) else None
    preview_url, perr = _copy_preview_to_uploads(
        str(preview_rel or ""),
        public_base,
        name_prefix="sam2_auto_preview",
    )
    if perr:
        log.append(f"preview copy: {perr}")

    n_items = len(raw.get("items") or [])
    n_total = raw.get("masks_total")
    log.append(f"Готово: {n_items} mask(s) saved" + (f" из {n_total}" if n_total else "") + ".")
    if preview_url:
        log.append(f"preview: {preview_url}")

    clean = {k: v for k, v in raw.items() if k not in ("ok", "log")}
    return clean, log, preview_url, None
