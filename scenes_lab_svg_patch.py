"""Рендер фрагмента SVG в растр и правка через модель (только блок SVG_START/END)."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from later_response_parse import (
    MARKER_SVG_END,
    MARKER_SVG_START,
    _extract_svg_document,
    _normalize_svg_block,
    _slice_markers,
    parse_later_response,
    unwrap_code_fence_block,
)
from scenes_lab_later import (
    SCENES_LAB_UPLOADS_DIR,
    save_scenes_lab_upload,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RENDER_WIDTH = 1920
DEFAULT_RENDER_HEIGHT = 1080

DEFAULT_SVG_PATCH_SYSTEM_PROMPT = """Ты правишь фрагмент SVG для motion-графики (холст 1920×1080).

ФОРМАТ ОТВЕТА — строго один блок, без markdown ``` и без JSON/NOTES:
===SVG_START===
…XML фрагмента (полный <svg>…</svg> или внутренние элементы, как в запросе)…
===SVG_END===

Правила:
- Между маркерами только сырой XML.
- Каждый текст — полный тег <text>…</text>, никогда голые id= без тега.
- Во вложении растр текущего вида фрагмента: сохраняй стиль, палитру, шрифты; меняй только то, что просит пользователь.
- Не добавляй блоки ANIM, NOTES и пояснения вне SVG."""


def _target_raster_size(
    svg_document: str,
    width: int,
    height: int,
) -> tuple[int, int]:
    """Размер растра: явный viewBox или целевой 1920×1080."""
    m = re.search(r'viewBox\s*=\s*["\']([^"\']+)["\']', svg_document or "", re.I)
    if m:
        parts = re.split(r"[\s,]+", m.group(1).strip())
        if len(parts) >= 4:
            try:
                vbw = float(parts[2])
                vbh = float(parts[3])
                if vbw > 0 and vbh > 0:
                    return int(round(vbw)), int(round(vbh))
            except ValueError:
                pass
    return width, height


def ensure_svg_raster_dimensions(
    svg_document: str,
    *,
    width: int = DEFAULT_RENDER_WIDTH,
    height: int = DEFAULT_RENDER_HEIGHT,
) -> str:
    """librsvg/ffmpeg без width/height рисуют ~100×100 — задаём размер корня SVG."""
    doc = (svg_document or "").strip()
    if not doc or not re.search(r"<svg[\s>]", doc, re.I):
        return doc
    rw, rh = _target_raster_size(doc, width, height)

    def _set_attr(svg_tag: str, name: str, value: int) -> str:
        pat = rf"(\b{name}\s*=\s*[\"'])([^\"']*)([\"'])"
        if re.search(pat, svg_tag, re.I):
            return re.sub(pat, rf"\g<1>{value}\g<3>", svg_tag, count=1, flags=re.I)
        return re.sub(r"<svg\b", f'<svg {name}="{value}"', svg_tag, count=1, flags=re.I)

    def _repl_root(match: re.Match[str]) -> str:
        tag = match.group(0)
        tag = _set_attr(tag, "width", rw)
        tag = _set_attr(tag, "height", rh)
        return tag

    return re.sub(r"<svg\b[^>]*>", _repl_root, doc, count=1, flags=re.I)


def svg_to_standalone_document(fragment: str) -> str:
    """Обёртка фрагмента в валидный SVG для рендера."""
    raw = (fragment or "").strip()
    if not raw:
        return ""
    doc = _extract_svg_document(raw)
    if doc and re.search(r"<svg[\s>]", doc, re.I):
        return ensure_svg_raster_dimensions(doc)
    vb = "0 0 1920 1080"
    wrapped = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'width="{DEFAULT_RENDER_WIDTH}" height="{DEFAULT_RENDER_HEIGHT}">'
        f"{raw}</svg>"
    )
    return ensure_svg_raster_dimensions(wrapped)


def _png_dimensions(png_path: Path) -> tuple[int, int] | None:
    """Размер PNG через ffprobe (без Pillow)."""
    ffprobe = os.environ.get("FFPROBE", "") or __import__("shutil").which("ffprobe") or ""
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(png_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = line[-1].split(",")
    if len(parts) >= 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None
    return None


def render_svg_to_png(
    svg_document: str,
    *,
    width: int = DEFAULT_RENDER_WIDTH,
    height: int = DEFAULT_RENDER_HEIGHT,
) -> tuple[bytes | None, str | None]:
    """Растеризация SVG → PNG через ffmpeg (librsvg), нативно 1920×1080."""
    doc = ensure_svg_raster_dimensions(
        (svg_document or "").strip(),
        width=width,
        height=height,
    )
    if not doc:
        return None, "Пустой SVG."
    ffmpeg = os.environ.get("FFMPEG", "") or __import__("shutil").which("ffmpeg") or ""
    if not ffmpeg:
        return None, "ffmpeg не найден в PATH."
    rw, rh = _target_raster_size(doc, width, height)

    with tempfile.TemporaryDirectory(prefix="scenes_lab_svg_") as tmp:
        svg_path = Path(tmp) / "frame.svg"
        png_path = Path(tmp) / "frame.png"
        svg_path.write_text(doc, encoding="utf-8")

        def _run_ffmpeg(vf: str | None) -> subprocess.CompletedProcess[str]:
            cmd = [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(svg_path),
            ]
            if vf:
                cmd.extend(["-vf", vf])
            cmd.extend(["-frames:v", "1", "-update", "1", str(png_path)])
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)

        try:
            proc = _run_ffmpeg(None)
            dims = _png_dimensions(png_path) if png_path.is_file() else None
            if proc.returncode != 0 or not dims or dims[0] < rw * 0.5 or dims[1] < rh * 0.5:
                if png_path.is_file():
                    png_path.unlink(missing_ok=True)
                proc = _run_ffmpeg(f"scale={rw}:{rh}:flags=lanczos")
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"ffmpeg: {exc}"
        if proc.returncode != 0 or not png_path.is_file():
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            return None, f"Не удалось отрендерить SVG в PNG. {err}"
        return png_path.read_bytes(), None


def save_rendered_preview_png(
    png_bytes: bytes,
    public_base: str,
    *,
    stem: str = "svg_preview",
) -> tuple[str | None, str | None]:
    if not png_bytes:
        return None, "Пустой PNG."
    name = f"{uuid.uuid4().hex}_{stem}.png"
    return save_scenes_lab_upload(png_bytes, name, public_base)


def wrap_svg_markers(svg_body: str) -> str:
    inner = (svg_body or "").strip()
    return f"{MARKER_SVG_START}\n{inner}\n{MARKER_SVG_END}"


def extract_svg_from_patch_response(text: str) -> tuple[str | None, str | None]:
    """Вытащить SVG из ответа модели (только блок между маркерами)."""
    raw = (text or "").strip()
    if not raw:
        return None, "Пустой ответ модели."
    frag = _slice_markers(raw, MARKER_SVG_START, MARKER_SVG_END)
    if frag is None:
        frag = unwrap_code_fence_block(raw)
    if not (frag or "").strip():
        doc = _extract_svg_document(raw)
        if doc:
            frag = doc
    if not (frag or "").strip():
        return None, "В ответе нет блока ===SVG_START=== … ===SVG_END===."
    svg, _meta = _normalize_svg_block(frag)
    if not svg:
        return None, "SVG в ответе пустой или невалиден."
    return svg, None


def replace_svg_in_later_text(full_text: str, new_svg: str) -> tuple[str, str | None]:
    """Подставить новый SVG в полный ответ Later… (между SVG_START и SVG_END)."""
    raw = full_text or ""
    new_inner, _meta = _normalize_svg_block(new_svg)
    if not new_inner:
        return raw, "Новый SVG пустой."

    start = raw.find(MARKER_SVG_START)
    end = raw.find(MARKER_SVG_END)
    if start >= 0 and end > start:
        before = raw[: start + len(MARKER_SVG_START)]
        after = raw[end:]
        merged = f"{before}\n{new_inner}\n{after}"
        return merged, None

    # Нет маркеров — собираем минимальный каркас, сохраняя ANIM/NOTES если есть
    parts = parse_later_response(raw)
    anim_raw = parts.get("animation_raw") or ""
    notes = parts.get("notes") or ""
    chunks = [wrap_svg_markers(new_inner)]
    if (anim_raw or "").strip():
        chunks.append(f"===ANIM_START===\n{anim_raw.strip()}\n===ANIM_END===")
    if (notes or "").strip():
        chunks.append(f"===NOTES_START===\n{notes.strip()}\n===NOTES_END===")
    return "\n\n".join(chunks) + "\n", None


def build_patch_user_message(svg_fragment: str, user_prompt: str) -> str:
    frag = (svg_fragment or "").strip()
    up = (user_prompt or "").strip()
    parts = [
        "Текущий фрагмент SVG (исходник для правки):",
        wrap_svg_markers(frag),
    ]
    if up:
        parts.append(f"Запрос на правку:\n{up}")
    else:
        parts.append("Запрос на правку: улучши фрагмент по приложенному растру, сохраняя общий стиль.")
    return "\n\n".join(parts)


def run_svg_patch_flow(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    svg_fragment: str,
    full_later_text: str,
    public_base: str,
) -> dict[str, Any]:
    """Рендер → модель → замена SVG в полном тексте → parse/validate."""
    from scenes_lab_later import run_later_model_request
    from later_response_parse import process_later_model_response

    doc = svg_to_standalone_document(svg_fragment)
    if not doc:
        return {"ok": False, "error": "Фрагмент SVG пустой."}

    png_bytes, rend_err = render_svg_to_png(doc)
    if rend_err or not png_bytes:
        return {"ok": False, "error": rend_err or "Рендер PNG не удался."}

    preview_url, up_err = save_rendered_preview_png(png_bytes, public_base, stem="svg_patch")
    if up_err or not preview_url:
        return {"ok": False, "error": up_err or "Не удалось сохранить превью PNG."}

    sys_p = (system_prompt or "").strip() or DEFAULT_SVG_PATCH_SYSTEM_PROMPT
    user_body = build_patch_user_message(svg_fragment, user_prompt)

    answer, model_err = run_later_model_request(
        model=model,
        user_prompt=user_body,
        image_url=preview_url,
        system_prompt=sys_p,
    )
    if model_err:
        return {
            "ok": False,
            "error": model_err,
            "preview_url": preview_url,
        }

    patch_svg, ext_err = extract_svg_from_patch_response(answer or "")
    if ext_err or not patch_svg:
        return {
            "ok": False,
            "error": ext_err or "Не удалось извлечь SVG из ответа.",
            "preview_url": preview_url,
            "model_response": answer or "",
        }

    merged_text, merge_err = replace_svg_in_later_text(full_later_text, patch_svg)
    if merge_err:
        return {
            "ok": False,
            "error": merge_err,
            "preview_url": preview_url,
            "model_response": answer or "",
            "patch_svg": patch_svg,
        }

    bundle = process_later_model_response(merged_text)
    validation = bundle["validation"]

    return {
        "ok": True,
        "text": merged_text,
        "parsed": bundle["parsed"],
        "validation": validation,
        "pipeline_ok": bool(validation.get("ok")),
        "preview_url": preview_url,
        "model_response": answer or "",
        "patch_svg": patch_svg,
    }
