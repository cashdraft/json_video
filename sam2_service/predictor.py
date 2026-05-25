"""Lazy-loaded SAM2 image predictor (box prompts only)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor

ROOT = Path(__file__).resolve().parent
DEFAULT_CHECKPOINT = ROOT / "checkpoints" / "sam2.1_hiera_tiny.pt"
DEFAULT_MODEL_CFG = "configs/sam2.1/sam2.1_hiera_t.yaml"


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Sam2BoxSegmenter:
    def __init__(
        self,
        checkpoint: Path | None = None,
        model_cfg: str = DEFAULT_MODEL_CFG,
    ) -> None:
        self.checkpoint = Path(checkpoint or DEFAULT_CHECKPOINT)
        self.model_cfg = model_cfg
        self.device_name = pick_device()
        self._predictor: SAM2ImagePredictor | None = None

    @property
    def device(self) -> str:
        return self.device_name

    def load(self) -> None:
        if self._predictor is not None:
            return
        if not self.checkpoint.is_file():
            raise FileNotFoundError(f"SAM2 checkpoint not found: {self.checkpoint}")
        model = build_sam2(self.model_cfg, str(self.checkpoint), device=self.device_name)
        self._predictor = SAM2ImagePredictor(model)

    def set_image(self, image_rgb: np.ndarray) -> None:
        self.load()
        assert self._predictor is not None
        with torch.inference_mode():
            if self.device_name == "cuda":
                dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
                with torch.autocast(device_type="cuda", dtype=dtype):
                    self._predictor.set_image(image_rgb)
            else:
                self._predictor.set_image(image_rgb)

    def segment_box(self, image_rgb: np.ndarray, box_xyxy: tuple[float, float, float, float]) -> np.ndarray:
        """Return boolean mask (H, W) for one DINO bbox."""
        self.set_image(image_rgb)
        return self._predict_box(box_xyxy)

    def segment_box_after_set_image(self, box_xyxy: tuple[float, float, float, float]) -> np.ndarray:
        return self._predict_box(box_xyxy)

    def _predict_box(self, box_xyxy: tuple[float, float, float, float]) -> np.ndarray:
        assert self._predictor is not None
        box = np.array(box_xyxy, dtype=np.float32)
        with torch.inference_mode():
            if self.device_name == "cuda":
                dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
                with torch.autocast(device_type="cuda", dtype=dtype):
                    masks, _, _ = self._predictor.predict(
                        box=box,
                        multimask_output=False,
                    )
            else:
                masks, _, _ = self._predictor.predict(
                    box=box,
                    multimask_output=False,
                )
        if masks.ndim == 3:
            mask = masks[0]
        else:
            mask = masks
        return mask.astype(bool)
