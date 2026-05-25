"""Grounding DINO detection via Hugging Face Transformers."""

from __future__ import annotations

import re
from typing import Any

import torch
from PIL import Image
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

MODEL_ID = "IDEA-Research/grounding-dino-base"


def normalize_prompt(prompt: str) -> str:
    """HF Grounding DINO: lowercase queries, dot-separated, trailing period."""
    raw = (prompt or "").strip().lower()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"[.\n;,]+", raw) if p.strip()]
    if not parts:
        return ""
    return ". ".join(parts) + "."


def _round2(value: float) -> float:
    return round(float(value), 2)


def _clean_label(label: str) -> str:
    s = str(label or "").strip().lower()
    s = re.sub(r"^a\s+|^an\s+|^the\s+", "", s)
    return s.strip() or "object"


class GroundingDinoDetector:
    def __init__(self, model_id: str = MODEL_ID) -> None:
        self.model_id = model_id
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id)
        self.model.to(self.device)
        self.model.eval()

    @property
    def device_name(self) -> str:
        return self.device

    def detect(
        self,
        image: Image.Image,
        prompt: str,
        *,
        box_threshold: float = 0.25,
        text_threshold: float = 0.25,
    ) -> dict[str, Any]:
        if image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        text = normalize_prompt(prompt)
        if not text:
            return {
                "image_width": width,
                "image_height": height,
                "prompt": "",
                "detections": [],
            }

        inputs = self.processor(images=image, text=text, return_tensors="pt").to(self.device)

        with torch.no_grad():
            outputs = self.model(**inputs)

        results = self.processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=box_threshold,
            text_threshold=text_threshold,
            target_sizes=[(height, width)],
        )

        detections: list[dict[str, Any]] = []
        if results:
            batch = results[0]
            scores = batch.get("scores", [])
            boxes = batch.get("boxes", [])
            labels = batch.get("text_labels") or batch.get("labels") or []

            for score_t, box_t, label in zip(scores, boxes, labels):
                score = float(score_t.item() if hasattr(score_t, "item") else score_t)
                coords = box_t.tolist() if hasattr(box_t, "tolist") else list(box_t)
                x1, y1, x2, y2 = [int(round(c)) for c in coords[:4]]

                x1 = max(0, min(x1, width))
                x2 = max(0, min(x2, width))
                y1 = max(0, min(y1, height))
                y2 = max(0, min(y2, height))
                if x2 <= x1 or y2 <= y1:
                    continue

                w = x2 - x1
                h = y2 - y1

                detections.append(
                    {
                        "label": _clean_label(str(label)),
                        "score": _round2(score),
                        "box_px": {
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                            "w": w,
                            "h": h,
                        },
                        "box_pct": {
                            "x_pct": _round2(100.0 * x1 / width) if width else 0.0,
                            "y_pct": _round2(100.0 * y1 / height) if height else 0.0,
                            "w_pct": _round2(100.0 * w / width) if width else 0.0,
                            "h_pct": _round2(100.0 * h / height) if height else 0.0,
                        },
                    }
                )

        detections.sort(key=lambda d: d["score"], reverse=True)

        return {
            "image_width": width,
            "image_height": height,
            "prompt": text,
            "detections": detections,
        }
