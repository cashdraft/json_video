"""DINO detections → SAM2 box-prompt masks → merged occupied mask + preview JSON."""

from __future__ import annotations

import json
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from predictor import Sam2BoxSegmenter

ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
MASKS_DIR = OUTPUTS_DIR / "masks"
PREVIEW_PATH = OUTPUTS_DIR / "preview_dino_sam2.png"
MERGED_MASK_PATH = MASKS_DIR / "merged_occupied.png"

DEFAULT_MIN_SCORE = 0.35

LABEL_THRESHOLDS: dict[str, float] = {
    "face": 0.40,
    "glasses": 0.35,
    "hand": 0.40,
    "hands": 0.40,
    "monitor": 0.45,
    "screen": 0.45,
    "keyboard": 0.45,
    "mouse": 0.40,
    "desk phone": 0.40,
    "smartphone": 0.40,
    "notebook": 0.40,
    "document": 0.40,
    "text": 0.30,
    "logo": 0.30,
}

BODY_PART_LABELS = {
    "face",
    "glasses",
    "hand",
    "hands",
    "head",
    "arm",
    "arms",
    "body",
    "torso",
    "leg",
    "legs",
    "foot",
    "feet",
    "finger",
    "fingers",
    "hair",
    "ear",
    "ears",
    "mouth",
    "nose",
    "eye",
    "eyes",
}


def normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", str(label or "").strip().lower())


def threshold_for_label(label: str, min_score: float) -> float:
    return LABEL_THRESHOLDS.get(normalize_label(label), min_score)


def has_body_part_detections(detections: list[dict[str, Any]]) -> bool:
    for det in detections:
        lab = normalize_label(str(det.get("label") or ""))
        if not lab:
            continue
        if lab in BODY_PART_LABELS:
            return True
        if "hand" in lab or "head" in lab or "face" in lab:
            return True
    return False


def parse_detections_json(raw: str | dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, dict):
        return raw, None
    text = str(raw or "").strip()
    if not text:
        return {}, "detections_json is empty"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"detections_json is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return {}, "detections_json must be a JSON object"
    return data, None


def clamp_box(box: dict[str, Any], w: int, h: int) -> tuple[int, int, int, int] | None:
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


def mask_bbox_px(mask: np.ndarray) -> dict[str, int] | None:
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    return {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "w": x2 - x1 + 1,
        "h": y2 - y1 + 1,
    }


def mask_bbox_pct(bbox_px: dict[str, int], w: int, h: int) -> dict[str, float]:
    return {
        "x_pct": round(bbox_px["x1"] / w * 100, 2),
        "y_pct": round(bbox_px["y1"] / h * 100, 2),
        "w_pct": round(bbox_px["w"] / w * 100, 2),
        "h_pct": round(bbox_px["h"] / h * 100, 2),
    }


def rel_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _slug_label(label: str) -> str:
    s = normalize_label(label).replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]+", "", s)
    return s or "item"


def filter_detections(
    detections: list[dict[str, Any]],
    *,
    min_score: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    body_parts_present = has_body_part_detections(detections)

    for det in detections:
        if not isinstance(det, dict):
            continue
        label = str(det.get("label") or "").strip()
        lab_norm = normalize_label(label)
        try:
            score = float(det.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0

        if lab_norm == "person":
            if body_parts_present:
                skipped.append(
                    {
                        "label": label or "person",
                        "reason": "skipped because body-part detections exist",
                    }
                )
                continue

        thr = threshold_for_label(label, min_score)
        if score < thr:
            skipped.append(
                {
                    "label": label or "?",
                    "score": score,
                    "reason": f"score {score:.3f} < threshold {thr:.2f}",
                }
            )
            continue

        box = det.get("box_px")
        if not isinstance(box, dict):
            skipped.append({"label": label or "?", "reason": "missing box_px"})
            continue

        kept.append(
            {
                "label": label,
                "score": score,
                "box_px": {
                    "x1": box.get("x1"),
                    "y1": box.get("y1"),
                    "x2": box.get("x2"),
                    "y2": box.get("y2"),
                },
            }
        )

    return kept, skipped


def draw_preview(
    image_rgb: np.ndarray,
    items: list[dict[str, Any]],
    out_path: Path,
) -> None:
    base = image_rgb.copy()
    overlay = base.copy()
    rng = np.random.default_rng(7)

    for item in items:
        mask = item.get("_mask")
        if mask is None or not isinstance(mask, np.ndarray):
            continue
        color = tuple(int(x) for x in rng.integers(64, 255, size=3))
        color_arr = np.array(color, dtype=np.float32)
        m = mask.astype(bool)
        overlay[m] = (overlay[m].astype(np.float32) * 0.45 + color_arr * 0.55).astype(np.uint8)

        contours, _ = cv2.findContours(
            mask.astype(np.uint8),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        cv2.drawContours(overlay, contours, -1, color, 2)

        src = item.get("source_box_px") or {}
        rect = clamp_box(src, base.shape[1], base.shape[0])
        if rect:
            x1, y1, x2, y2 = rect
            cv2.rectangle(overlay, (x1, y1), (x2, y2), (180, 180, 180), 1)

        mb = item.get("mask_bbox_px") or {}
        mrect = clamp_box(mb, base.shape[1], base.shape[0])
        if mrect:
            x1, y1, x2, y2 = mrect
            cv2.rectangle(overlay, (x1, y1), (x2, y2), (255, 255, 255), 2)

        label = str(item.get("label") or "?")
        if mrect:
            tx, ty = mrect[0], max(12, mrect[1] - 6)
            cv2.putText(
                overlay,
                label,
                (tx, ty),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                color,
                1,
                cv2.LINE_AA,
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))


def process_dino_to_sam2(
    image_bytes: bytes,
    detections_payload: str | dict[str, Any],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    segmenter: Sam2BoxSegmenter | None = None,
) -> tuple[dict[str, Any], list[str], str | None]:
    log: list[str] = []
    payload, perr = parse_detections_json(detections_payload)
    if perr:
        return {}, log, perr

    try:
        pil = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        return {}, log, f"Invalid image: {exc}"

    image_rgb = np.array(pil)
    h, w = image_rgb.shape[:2]
    img_w = int(payload.get("image_width") or w)
    img_h = int(payload.get("image_height") or h)
    if img_w != w or img_h != h:
        log.append(f"note: JSON dims {img_w}x{img_h}, actual image {w}x{h}")

    detections = payload.get("detections") or []
    if not isinstance(detections, list):
        return {}, log, "detections must be an array"

    kept, skipped = filter_detections(detections, min_score=min_score)
    log.append(f"detections in: {len(detections)} · kept: {len(kept)} · skipped: {len(skipped)}")
    if not kept:
        return {
            "image_width": w,
            "image_height": h,
            "mode": "dino_to_sam2",
            "items": [],
            "merged_occupied": None,
            "skipped": skipped,
        }, log, None

    seg = segmenter or Sam2BoxSegmenter()
    log.append(f"SAM2 device: {seg.device}")
    t0 = time.perf_counter()
    seg.load()
    log.append(f"SAM2 loaded in {time.perf_counter() - t0:.1f}s")
    seg.set_image(image_rgb)

    MASKS_DIR.mkdir(parents=True, exist_ok=True)
    label_counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    merged = np.zeros((h, w), dtype=bool)

    for det in kept:
        label = str(det.get("label") or "item")
        slug = _slug_label(label)
        label_counts[slug] = label_counts.get(slug, 0) + 1
        seq = label_counts[slug]
        rect = clamp_box(det["box_px"], w, h)
        if not rect:
            skipped.append({"label": label, "reason": "invalid box_px after clamp"})
            continue

        x1, y1, x2, y2 = rect
        t1 = time.perf_counter()
        try:
            mask = seg.segment_box_after_set_image((float(x1), float(y1), float(x2), float(y2)))
        except Exception as exc:
            skipped.append({"label": label, "reason": f"SAM2 failed: {exc}"})
            continue
        log.append(f"  {label}: {time.perf_counter() - t1:.1f}s")

        area_px = int(mask.sum())
        if area_px <= 0:
            skipped.append({"label": label, "reason": "empty SAM2 mask"})
            continue

        bbox_px = mask_bbox_px(mask)
        if not bbox_px:
            skipped.append({"label": label, "reason": "empty mask bbox"})
            continue

        mask_name = f"{slug}_{seq:03d}.png"
        mask_path = MASKS_DIR / mask_name
        Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(mask_path)

        merged |= mask
        area_pct = round(area_px / (w * h) * 100, 2)
        item = {
            "label": label,
            "source_score": float(det.get("score") or 0.0),
            "source_box_px": {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            },
            "mask_path": rel_path(mask_path),
            "mask_bbox_px": bbox_px,
            "mask_bbox_pct": mask_bbox_pct(bbox_px, w, h),
            "mask_area_px": area_px,
            "mask_area_pct": area_pct,
            "_mask": mask,
        }
        items.append(item)

    merged_area_px = int(merged.sum())
    merged_area_pct = round(merged_area_px / (w * h) * 100, 2) if w * h else 0.0
    Image.fromarray((merged.astype(np.uint8) * 255), mode="L").save(MERGED_MASK_PATH)

    draw_items = items
    draw_preview(image_rgb, draw_items, PREVIEW_PATH)

    public_items = []
    for item in items:
        public_items.append({k: v for k, v in item.items() if not k.startswith("_")})

    result = {
        "image_width": w,
        "image_height": h,
        "mode": "dino_to_sam2",
        "items": public_items,
        "merged_occupied": {
            "mask_path": rel_path(MERGED_MASK_PATH),
            "preview_path": rel_path(PREVIEW_PATH),
            "mask_area_px": merged_area_px,
            "mask_area_pct": merged_area_pct,
        },
        "skipped": skipped,
        "run_id": uuid.uuid4().hex[:12],
    }
    log.append(f"items: {len(public_items)} · merged area {merged_area_pct}%")
    return result, log, None
