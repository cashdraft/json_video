#!/usr/bin/env python3
"""Minimal SAM2 automatic mask generation smoke test (no API / no project integration)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

ROOT = Path(__file__).resolve().parent
TEST_IMAGE = ROOT / "test_images" / "test.jpg"
CHECKPOINT = ROOT / "checkpoints" / "sam2.1_hiera_tiny.pt"
MODEL_CFG = "configs/sam2.1/sam2.1_hiera_t.yaml"
OUTPUT_DIR = ROOT / "outputs"

# Lighter grid for CPU / low VRAM smoke test
POINTS_PER_SIDE = 16
POINTS_PER_BATCH = 16
MAX_MASKS_SAVE = 32


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_image_rgb(path: Path) -> np.ndarray:
    bgr = cv2.imread(str(path))
    if bgr is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def mask_to_rgba(mask: np.ndarray, color: tuple[int, int, int], alpha: float = 0.45) -> np.ndarray:
    h, w = mask.shape
    layer = np.zeros((h, w, 4), dtype=np.uint8)
    layer[mask] = (*color, int(255 * alpha))
    return layer


def save_preview_overlay(image_rgb: np.ndarray, masks: list, out_path: Path) -> None:
    """Blend colored masks on top of the original image."""
    base = Image.fromarray(image_rgb).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    rng = np.random.default_rng(42)
    for ann in masks:
        seg = ann["segmentation"]
        if not isinstance(seg, np.ndarray):
            continue
        color = tuple(int(x) for x in rng.integers(0, 255, size=3))
        layer = Image.fromarray(mask_to_rgba(seg.astype(bool), color))
        overlay = Image.alpha_composite(overlay, layer)
    preview = Image.alpha_composite(base, overlay).convert("RGB")
    preview.save(out_path, quality=92)


def save_mask_pngs(masks: list, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for i, ann in enumerate(masks):
        if saved >= MAX_MASKS_SAVE:
            break
        seg = ann["segmentation"]
        if not isinstance(seg, np.ndarray):
            continue
        mask_u8 = (seg.astype(np.uint8) * 255)
        Image.fromarray(mask_u8, mode="L").save(out_dir / f"mask_{i:03d}.png")
        saved += 1
    return saved


def main() -> int:
    if not TEST_IMAGE.is_file():
        print(f"Missing test image: {TEST_IMAGE}", file=sys.stderr)
        return 1
    if not CHECKPOINT.is_file():
        print(f"Missing checkpoint: {CHECKPOINT}", file=sys.stderr)
        print("Download: cd checkpoints && bash download_ckpts.sh (or wget tiny .pt)", file=sys.stderr)
        return 1

    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    from sam2.build_sam import build_sam2

    device = pick_device()
    print(f"Device: {device}")
    print(f"Checkpoint: {CHECKPOINT}")
    print(f"Test image: {TEST_IMAGE}")

    image_rgb = load_image_rgb(TEST_IMAGE)
    h, w = image_rgb.shape[:2]
    print(f"Image size: {w}x{h}")

    t0 = time.perf_counter()
    sam2_model = build_sam2(MODEL_CFG, str(CHECKPOINT), device=device)
    load_s = time.perf_counter() - t0
    print(f"Model loaded in {load_s:.1f}s")

    mask_generator = SAM2AutomaticMaskGenerator(
        sam2_model,
        points_per_side=POINTS_PER_SIDE,
        points_per_batch=POINTS_PER_BATCH,
        pred_iou_thresh=0.7,
        stability_score_thresh=0.85,
        crop_n_layers=0,
        output_mode="binary_mask",
    )

    t1 = time.perf_counter()
    with torch.inference_mode():
        if device == "cuda":
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            with torch.autocast(device_type="cuda", dtype=dtype):
                masks = mask_generator.generate(image_rgb)
        else:
            masks = mask_generator.generate(image_rgb)
    infer_s = time.perf_counter() - t1
    print(f"Generated {len(masks)} masks in {infer_s:.1f}s")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    preview_path = OUTPUT_DIR / "preview_masks.jpg"
    masks_dir = OUTPUT_DIR / "masks"
    save_preview_overlay(image_rgb, masks, preview_path)
    n_saved = save_mask_pngs(masks, masks_dir)

    summary = {
        "device": device,
        "checkpoint": CHECKPOINT.name,
        "image": str(TEST_IMAGE.relative_to(ROOT)),
        "image_size": [w, h],
        "masks_total": len(masks),
        "masks_saved_png": n_saved,
        "load_seconds": round(load_s, 2),
        "infer_seconds": round(infer_s, 2),
        "preview": str(preview_path.relative_to(ROOT)),
        "masks_dir": str(masks_dir.relative_to(ROOT)),
    }
    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Preview: {preview_path}")
    print(f"Mask PNGs: {masks_dir} ({n_saved} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
