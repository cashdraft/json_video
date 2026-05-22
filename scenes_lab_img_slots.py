"""Слоты готовых картинок Later… (img_1, img_2, …) — SVG, ответ, PNG."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from later_response_parse import MARKER_SVG_END, MARKER_SVG_START

BASE_DIR = Path(__file__).resolve().parent
IMG_SLOTS_ROOT = BASE_DIR / "data" / "scenes_lab"
_SLOT_RE = re.compile(r"^img_\d+$")

PREVIEW_FULL_NAME = "preview.png"
PREVIEW_THUMB_NAME = "preview_thumb.png"
PREVIEW_THUMB_WIDTH = 960
PREVIEW_THUMB_HEIGHT = 540

ANIM_RESPONSE_NAME = "anim_response.txt"
SCENE_AT_ANIM_NAME = "scene_at_anim.svg"
FIXLOG_NAME = "fixlog.txt"

ALLOWED_SLOT_FILES = frozenset(
    {
        "response.txt",
        "scene.svg",
        SCENE_AT_ANIM_NAME,
        FIXLOG_NAME,
        PREVIEW_FULL_NAME,
        PREVIEW_THUMB_NAME,
        "meta.json",
        ANIM_RESPONSE_NAME,
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slot_dir(slot_id: str) -> Path | None:
    sid = (slot_id or "").strip().lower()
    if not _SLOT_RE.match(sid):
        return None
    return IMG_SLOTS_ROOT / sid


def wrap_svg_response_text(svg: str, full_text: str = "") -> str:
    """Текст ответа с маркерами SVG (для response.txt)."""
    raw = (full_text or "").strip()
    if MARKER_SVG_START in raw and MARKER_SVG_END in raw:
        return raw
    inner = (svg or "").strip()
    return f"{MARKER_SVG_START}\n{inner}\n{MARKER_SVG_END}\n"


def save_img_slot(
    slot_id: str,
    *,
    full_text: str = "",
    svg: str = "",
    fixlog: str = "",
    public_base: str = "",
) -> dict[str, Any]:
    """
    Записать в data/scenes_lab/img_1/ (и т.д.):
    response.txt, scene.svg, preview.png, fixlog.txt (если есть), meta.json.
    Промт редактора — один на все слоты (later_prefs.json).
    """
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return {"ok": False, "error": f"Недопустимый слот: {slot_id!r}"}

    svg_body = (svg or "").strip()
    if not svg_body:
        return {"ok": False, "error": "SVG пустой."}

    slot_path.mkdir(parents=True, exist_ok=True)
    full = (full_text or "").strip()
    if full and MARKER_SVG_START in full:
        from scenes_lab_svg_patch import replace_svg_in_later_text

        merged, err = replace_svg_in_later_text(full, svg_body)
        response_text = merged if not err else wrap_svg_response_text(svg_body, full)
    else:
        response_text = wrap_svg_response_text(svg_body, full_text)
    (slot_path / "response.txt").write_text(response_text, encoding="utf-8")
    (slot_path / "scene.svg").write_text(svg_body, encoding="utf-8")
    fixlog_body = (fixlog or "").strip()
    if fixlog_body:
        (slot_path / FIXLOG_NAME).write_text(fixlog_body, encoding="utf-8")
    elif (slot_path / FIXLOG_NAME).is_file():
        (slot_path / FIXLOG_NAME).unlink()

    preview_error: str | None = None
    has_full = False
    has_thumb = False
    try:
        from scenes_lab_svg_patch import render_svg_to_png, svg_to_standalone_document

        doc = svg_to_standalone_document(svg_body)
        if doc:
            png_full, rend_err = render_svg_to_png(doc)
            if rend_err or not png_full:
                preview_error = rend_err or "Рендер PNG не удался"
            else:
                (slot_path / PREVIEW_FULL_NAME).write_bytes(png_full)
                has_full = True
                png_thumb, thumb_err = render_svg_to_png(
                    doc,
                    width=PREVIEW_THUMB_WIDTH,
                    height=PREVIEW_THUMB_HEIGHT,
                )
                if not thumb_err and png_thumb:
                    (slot_path / PREVIEW_THUMB_NAME).write_bytes(png_thumb)
                    has_thumb = True
        else:
            preview_error = "Не удалось собрать SVG-документ"
    except Exception as exc:
        preview_error = str(exc)

    meta = {
        "slot_id": slot_id,
        "saved_at": _now_iso(),
        "has_preview": has_full,
        "has_preview_thumb": has_thumb,
        "has_fixlog": bool(fixlog_body),
        "preview_error": preview_error,
    }
    (slot_path / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    base = (public_base or "").rstrip("/")
    prefix = f"{base}/scenes-lab/img-slots/{slot_id}" if base else f"/scenes-lab/img-slots/{slot_id}"
    return {
        "ok": True,
        "slot_id": slot_id,
        "saved_at": meta["saved_at"],
        "has_preview": meta["has_preview"],
        "preview_error": preview_error,
        "preview_url": _preview_public_url(slot_id, public_base, thumb=True)
        if (has_thumb or has_full)
        else None,
        "preview_url_full": f"{prefix}/{PREVIEW_FULL_NAME}" if has_full else None,
        "response_url": f"{prefix}/response.txt",
        "svg_url": f"{prefix}/scene.svg",
    }


def _preview_public_url(slot_id: str, public_base: str, *, thumb: bool = True) -> str | None:
    """URL для UI (thumb) или полный PNG (thumb=False)."""
    base = (public_base or "").rstrip("/")
    prefix = f"{base}/scenes-lab/img-slots/{slot_id}" if base else f"/scenes-lab/img-slots/{slot_id}"
    if thumb:
        if img_slot_asset_path(slot_id, PREVIEW_THUMB_NAME):
            return f"{prefix}/{PREVIEW_THUMB_NAME}"
        if img_slot_asset_path(slot_id, PREVIEW_FULL_NAME):
            return f"{prefix}/{PREVIEW_FULL_NAME}"
        return None
    if img_slot_asset_path(slot_id, PREVIEW_FULL_NAME):
        return f"{prefix}/{PREVIEW_FULL_NAME}"
    return None


def list_img_slot_ids() -> list[str]:
    if not IMG_SLOTS_ROOT.is_dir():
        return []

    def _key(name: str) -> int:
        m = re.match(r"^img_(\d+)$", name)
        return int(m.group(1)) if m else 0

    ids = []
    for p in IMG_SLOTS_ROOT.iterdir():
        if p.is_dir() and _SLOT_RE.match(p.name):
            if (p / "scene.svg").is_file() or (p / "response.txt").is_file():
                ids.append(p.name)
    return sorted(ids, key=_key)


def latest_img_slot_id() -> str | None:
    ids = list_img_slot_ids()
    return ids[-1] if ids else None


def delete_all_img_slots() -> list[str]:
    """Удалить все img_N на диске. Возвращает список удалённых id."""
    deleted: list[str] = []
    for sid in list_img_slot_ids():
        slot_path = _slot_dir(sid)
        if slot_path is None or not slot_path.is_dir():
            continue
        shutil.rmtree(slot_path)
        deleted.append(sid)
    return deleted


def next_img_slot_id() -> str:
    ids = list_img_slot_ids()
    if not ids:
        return "img_1"
    last = ids[-1]
    m = re.match(r"^img_(\d+)$", last)
    n = int(m.group(1)) + 1 if m else len(ids) + 1
    return f"img_{n}"


def load_img_slot_response(slot_id: str) -> str:
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return ""
    p = slot_path / "response.txt"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def load_img_slot_repaired_response(slot_id: str) -> str:
    """response.txt с подставленным починенным SVG из scene.svg (для «Переделать» / «Анимировать»)."""
    raw = load_img_slot_response(slot_id).strip()
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return raw
    svg_path = slot_path / "scene.svg"
    if not svg_path.is_file():
        return raw
    repaired = svg_path.read_text(encoding="utf-8").strip()
    if not repaired:
        return raw
    from scenes_lab_svg_patch import replace_svg_in_later_text

    merged, err = replace_svg_in_later_text(raw or repaired, repaired)
    return merged if not err else raw


def load_img_slot_anim_response(slot_id: str) -> str:
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return ""
    p = slot_path / ANIM_RESPONSE_NAME
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def load_img_slot_fixlog(slot_id: str) -> str:
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return ""
    p = slot_path / FIXLOG_NAME
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def _merge_slot_meta(slot_path: Path, **fields: Any) -> None:
    meta: dict[str, Any] = {}
    meta_path = slot_path / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            meta = {}
    meta.update(fields)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_img_slot_svg_for_remotion(slot_id: str) -> str:
    """SVG для Remotion: снимок на момент «Анимировать», иначе scene.svg."""
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return ""
    snap = slot_path / SCENE_AT_ANIM_NAME
    if snap.is_file():
        return snap.read_text(encoding="utf-8")
    svg_path = slot_path / "scene.svg"
    if svg_path.is_file():
        return svg_path.read_text(encoding="utf-8")
    return ""


def save_img_slot_anim_response(
    slot_id: str,
    anim_model_text: str,
    *,
    svg_snapshot: str = "",
) -> None:
    """Ответ анимации + SVG, с которым её генерировали (для Remotion)."""
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return
    slot_path.mkdir(parents=True, exist_ok=True)
    (slot_path / ANIM_RESPONSE_NAME).write_text(anim_model_text or "", encoding="utf-8")
    snap = (svg_snapshot or "").strip()
    if snap:
        (slot_path / SCENE_AT_ANIM_NAME).write_text(snap, encoding="utf-8")
    _merge_slot_meta(
        slot_path,
        anim_saved_at=_now_iso(),
        has_scene_at_anim=bool(snap),
        anim_slot_id=slot_id,
    )


def update_img_slot_response_text(slot_id: str, full_text: str) -> dict[str, Any]:
    """Обновить response.txt (например после вставки ANIM), без перерендера PNG."""
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return {"ok": False, "error": f"Недопустимый слот: {slot_id!r}"}
    if not (slot_path / "scene.svg").is_file():
        return {"ok": False, "error": f"В слоте {slot_id} нет scene.svg."}
    slot_path.mkdir(parents=True, exist_ok=True)
    (slot_path / "response.txt").write_text(full_text or "", encoding="utf-8")
    return {"ok": True, "slot_id": slot_id}


def img_slot_preview_public_url(slot_id: str, public_base: str, *, full: bool = False) -> str | None:
    """full=True — 1920×1080 для «Переделать»; иначе лёгкий thumb для UI."""
    return _preview_public_url(slot_id, public_base, thumb=not full)


def img_slot_summary(slot_id: str, public_base: str = "") -> dict[str, Any]:
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return {"id": slot_id, "ok": False}
    meta: dict[str, Any] = {}
    meta_path = slot_path / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            meta = {}
    return {
        "ok": True,
        "id": slot_id,
        "saved_at": meta.get("saved_at"),
        "has_preview": (slot_path / "preview.png").is_file(),
        "preview_url": img_slot_preview_public_url(slot_id, public_base),
        "response_len": len(load_img_slot_response(slot_id)),
    }


def list_img_slots_payload(public_base: str = "") -> dict[str, Any]:
    ids = list_img_slot_ids()
    return {
        "ok": True,
        "slots": [img_slot_summary(sid, public_base) for sid in ids],
        "latest_id": ids[-1] if ids else None,
    }


def load_img_slot_detail(slot_id: str, public_base: str = "") -> dict[str, Any]:
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return {"ok": False, "error": f"Слот {slot_id!r} не найден."}
    text = load_img_slot_response(slot_id)
    svg = ""
    svg_path = slot_path / "scene.svg"
    if svg_path.is_file():
        svg = svg_path.read_text(encoding="utf-8")
    fixlog = load_img_slot_fixlog(slot_id)
    return {
        "ok": True,
        "id": slot_id,
        "text": text,
        "svg": svg,
        "fixlog_text": fixlog,
        "has_fixlog": bool(fixlog.strip()),
        "preview_url": img_slot_preview_public_url(slot_id, public_base),
    }


def img_slot_asset_path(slot_id: str, filename: str) -> Path | None:
    if filename not in ALLOWED_SLOT_FILES:
        return None
    slot_path = _slot_dir(slot_id)
    if slot_path is None:
        return None
    path = (slot_path / filename).resolve()
    try:
        path.relative_to(slot_path.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None
