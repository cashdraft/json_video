"""SAM2 automatic mask generation (без DINO bbox)."""

from __future__ import annotations

import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
from PIL import Image
from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
from sam2.build_sam import build_sam2

from dino_to_sam2 import (
    OUTPUTS_DIR,
    mask_bbox_pct,
    mask_bbox_px,
    rel_path,
)
from predictor import DEFAULT_CHECKPOINT, DEFAULT_MODEL_CFG, pick_device

ROOT = Path(__file__).resolve().parent
AUTO_MASKS_DIR = OUTPUTS_DIR / "masks_auto"
AUTO_PREVIEW_PATH = OUTPUTS_DIR / "preview_sam2_auto.png"
AUTO_MERGED_PATH = AUTO_MASKS_DIR / "merged_occupied.png"

MAX_AUTO_SIDE = 1536
POINTS_PER_SIDE = 16
POINTS_PER_BATCH = 16
MAX_MASKS_SAVE = 48

_auto_generator: SAM2AutomaticMaskGenerator | None = None
_auto_device: str = ""


def _get_auto_generator() -> tuple[SAM2AutomaticMaskGenerator, str]:
    global _auto_generator, _auto_device
    if _auto_generator is not None:
        return _auto_generator, _auto_device

    device = pick_device()
    if not DEFAULT_CHECKPOINT.is_file():
        raise FileNotFoundError(f"SAM2 checkpoint not found: {DEFAULT_CHECKPOINT}")

    t0 = time.perf_counter()
    model = build_sam2(DEFAULT_MODEL_CFG, str(DEFAULT_CHECKPOINT), device=device)
    _auto_generator = SAM2AutomaticMaskGenerator(
        model,
        points_per_side=POINTS_PER_SIDE,
        points_per_batch=POINTS_PER_BATCH,
        pred_iou_thresh=0.72,
        stability_score_thresh=0.88,
        crop_n_layers=0,
        output_mode="binary_mask",
    )
    _auto_device = device
    return _auto_generator, device


def _maybe_downscale(image_rgb: np.ndarray) -> tuple[np.ndarray, tuple[int, int], float]:
    h, w = image_rgb.shape[:2]
    orig = (w, h)
    max_side = max(w, h)
    if max_side <= MAX_AUTO_SIDE:
        return image_rgb, orig, 1.0
    scale = MAX_AUTO_SIDE / max_side
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = cv2.resize(image_rgb, (nw, nh), interpolation=cv2.INTER_AREA)
    return resized, orig, scale


def _scale_mask_to_orig(mask: np.ndarray, orig_wh: tuple[int, int]) -> np.ndarray:
    ow, oh = orig_wh
    if mask.shape[1] == ow and mask.shape[0] == oh:
        return mask.astype(bool)
    up = cv2.resize(mask.astype(np.uint8), (ow, oh), interpolation=cv2.INTER_NEAREST)
    return up.astype(bool)


def draw_auto_preview(image_rgb: np.ndarray, items: list[dict[str, Any]], out_path: Path) -> None:
    overlay = image_rgb.copy()
    rng = np.random.default_rng(11)
    for item in items:
        mask = item.get("_mask")
        if mask is None:
            continue
        color = tuple(int(x) for x in rng.integers(64, 255, size=3))
        color_arr = np.array(color, dtype=np.float32)
        m = mask.astype(bool)
        overlay[m] = (overlay[m].astype(np.float32) * 0.45 + color_arr * 0.55).astype(np.uint8)
        contours, _ = cv2.findContours(
            mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        cv2.drawContours(overlay, contours, -1, color, 2)
        mb = item.get("mask_bbox_px") or {}
        if mb:
            x1, y1, x2, y2 = int(mb["x1"]), int(mb["y1"]), int(mb["x2"]), int(mb["y2"])
            cv2.rectangle(overlay, (x1, y1), (x2, y2), (255, 255, 255), 1)
            cv2.putText(
                overlay,
                f"#{item.get('index', 0)}",
                (x1, max(12, y1 - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                color,
                1,
                cv2.LINE_AA,
            )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))


def process_sam2_auto(
    image_bytes: bytes,
) -> tuple[dict[str, Any], list[str], str | None]:
    log: list[str] = []
    try:
        pil = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        return {}, log, f"Invalid image: {exc}"

    image_rgb = np.array(pil)
    oh, ow = image_rgb.shape[:2]
    work_rgb, orig_wh, scale = _maybe_downscale(image_rgb)
    wh, ww = work_rgb.shape[:2]
    if scale < 1.0:
        log.append(f"auto: downscale {ow}x{oh} → {ww}x{wh} для inference (max side {MAX_AUTO_SIDE})")

    gen, device = _get_auto_generator()
    log.append(f"SAM2 auto · device: {device}")

    t0 = time.perf_counter()
    with torch.inference_mode():
        if device == "cuda":
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            with torch.autocast(device_type="cuda", dtype=dtype):
                raw_masks = gen.generate(work_rgb)
        else:
            raw_masks = gen.generate(work_rgb)
    infer_s = time.perf_counter() - t0
    log.append(f"generated {len(raw_masks)} raw masks in {infer_s:.1f}s")

    AUTO_MASKS_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    merged = np.zeros((oh, ow), dtype=bool)

    for i, ann in enumerate(raw_masks[:MAX_MASKS_SAVE]):
        seg = ann.get("segmentation")
        if not isinstance(seg, np.ndarray):
            continue
        mask = _scale_mask_to_orig(seg.astype(bool), orig_wh)
        area_px = int(mask.sum())
        if area_px <= 0:
            continue
        bbox_px = mask_bbox_px(mask)
        if not bbox_px:
            continue

        mask_name = f"auto_{i:03d}.png"
        mask_path = AUTO_MASKS_DIR / mask_name
        Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(mask_path)
        merged |= mask

        items.append(
            {
                "index": i,
                "predicted_iou": round(float(ann.get("predicted_iou") or 0.0), 4),
                "stability_score": round(float(ann.get("stability_score") or 0.0), 4),
                "mask_path": rel_path(mask_path),
                "mask_bbox_px": bbox_px,
                "mask_bbox_pct": mask_bbox_pct(bbox_px, ow, oh),
                "mask_area_px": area_px,
                "mask_area_pct": round(area_px / (ow * oh) * 100, 2),
                "_mask": mask,
            }
        )

    merged_area_px = int(merged.sum())
    Image.fromarray((merged.astype(np.uint8) * 255), mode="L").save(AUTO_MERGED_PATH)
    draw_auto_preview(image_rgb, items, AUTO_PREVIEW_PATH)

    public_items = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
    result = {
        "image_width": ow,
        "image_height": oh,
        "mode": "sam2_auto",
        "processed_width": ww if scale < 1.0 else ow,
        "processed_height": wh if scale < 1.0 else oh,
        "items": public_items,
        "masks_total": len(raw_masks),
        "masks_saved": len(public_items),
        "merged_occupied": {
            "mask_path": rel_path(AUTO_MERGED_PATH),
            "preview_path": rel_path(AUTO_PREVIEW_PATH),
            "mask_area_px": merged_area_px,
            "mask_area_pct": round(merged_area_px / (ow * oh) * 100, 2),
        },
        "run_id": uuid.uuid4().hex[:12],
    }
    log.append(f"saved {len(public_items)} masks · merged {result['merged_occupied']['mask_area_pct']}%")
    return result, log, None
