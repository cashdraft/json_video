"""Remotion props для /overlay-text (OverlayCaption)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from montage_render_shared import BASE_DIR
from overlay_text_upload import OVERLAY_UPLOADS_DIR

OVERLAY_TEXT_REMOTION_DIR = BASE_DIR / "data" / "overlay_text" / "remotion"
OVERLAY_TEXT_REMOTION_PUBLIC = BASE_DIR / "remotion" / "public" / "overlay-text"
FRAME_FILENAME = "frame.jpg"


def remotion_props_path() -> Path:
    return OVERLAY_TEXT_REMOTION_DIR / "props.json"


def remotion_out_path() -> Path:
    return OVERLAY_TEXT_REMOTION_DIR / "out.mp4"


def strip_json_fence(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return s


def parse_overlay_result_text(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    body = strip_json_fence(raw)
    if not body:
        return None, "Result пустой — сначала получите JSON от модели."
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None, "В Result нет JSON-объекта."
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as e:
        return None, f"Result — невалидный JSON: {e}"
    if not isinstance(data, dict):
        return None, "Result JSON должен быть объектом."
    overlay = data.get("overlay")
    if not isinstance(overlay, dict):
        return None, "В Result нет поля overlay."
    if overlay.get("enabled") is False:
        return None, "overlay.enabled = false — нечего рендерить."
    lines = overlay.get("final_text_lines")
    if not isinstance(lines, list) or not any(str(x).strip() for x in lines):
        return None, "overlay.final_text_lines пуст."
    return data, None


def _upload_path_from_url(image_url: str) -> Path | None:
    url = (image_url or "").strip()
    if not url:
        return None
    path = urlparse(url).path or ""
    marker = "/overlay-text/media/"
    idx = path.find(marker)
    if idx < 0:
        return None
    name = path[idx + len(marker) :].split("/")[0]
    if not name:
        return None
    local = (OVERLAY_UPLOADS_DIR / name).resolve()
    root = OVERLAY_UPLOADS_DIR.resolve()
    if not str(local).startswith(str(root)) or not local.is_file():
        return None
    return local


def _ensure_public_symlink() -> None:
    OVERLAY_TEXT_REMOTION_DIR.mkdir(parents=True, exist_ok=True)
    public = OVERLAY_TEXT_REMOTION_PUBLIC
    target = OVERLAY_TEXT_REMOTION_DIR.resolve()
    if public.is_symlink():
        try:
            if public.resolve() == target:
                return
        except OSError:
            pass
        public.unlink()
    elif public.exists():
        return
    public.symlink_to(target, target_is_directory=True)


def prepare_frame_image(image_url: str) -> tuple[str | None, str | None]:
    src = _upload_path_from_url(image_url)
    if src is None:
        return None, "Локальное фото не найдено — загрузите JPEG/PNG через «Загрузить»."
    ext = src.suffix.lower()
    out_name = FRAME_FILENAME if ext in {".jpg", ".jpeg"} else f"frame{ext}"
    OVERLAY_TEXT_REMOTION_DIR.mkdir(parents=True, exist_ok=True)
    dst = OVERLAY_TEXT_REMOTION_DIR / out_name
    shutil.copy2(src, dst)
    _ensure_public_symlink()
    return f"overlay-text/{out_name}", None


def build_remotion_props_from_prefs(prefs: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    parsed, err = parse_overlay_result_text(str(prefs.get("result") or ""))
    if err or parsed is None:
        return None, err

    image_url = str(prefs.get("image_url") or prefs.get("image_preview_url") or "").strip()
    image_src, img_err = prepare_frame_image(image_url)
    if img_err or not image_src:
        return None, img_err

    try:
        fps = max(1, int(float(parsed.get("fps") or 30)))
    except (TypeError, ValueError):
        fps = 30
    try:
        duration_sec = max(
            0.5,
            float(
                parsed.get("scene_duration_sec")
                or prefs.get("duration_sec")
                or 5
            ),
        )
    except (TypeError, ValueError):
        duration_sec = 5.0

    overlay = parsed.get("overlay")
    if not isinstance(overlay, dict):
        return None, "overlay отсутствует."

    props: dict[str, Any] = {
        "schema": "overlay_caption_props@1",
        "fps": fps,
        "width": 1920,
        "height": 1080,
        "duration_sec": duration_sec,
        "duration_frames": max(1, int(round(duration_sec * fps))),
        "image_src": image_src,
        "overlay": overlay,
    }
    return props, None


def write_remotion_props(prefs: dict[str, Any]) -> tuple[Path | None, str | None]:
    props, err = build_remotion_props_from_prefs(prefs)
    if err or props is None:
        return None, err
    path = remotion_props_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, None
