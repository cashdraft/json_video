"""FastAPI service for Grounding DINO bbox detection."""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from detector import MODEL_ID, GroundingDinoDetector

_detector: GroundingDinoDetector | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _detector
    _detector = GroundingDinoDetector()
    yield
    _detector = None


app = FastAPI(
    title="Grounding DINO Service",
    version="1.0.0",
    lifespan=lifespan,
)


def get_detector() -> GroundingDinoDetector:
    if _detector is None:
        raise HTTPException(status_code=503, detail="Detector is not loaded yet.")
    return _detector


@app.get("/health")
def health() -> dict[str, str]:
    det = get_detector()
    return {
        "status": "ok",
        "device": det.device_name,
        "model": MODEL_ID,
    }


@app.post("/detect")
async def detect(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    box_threshold: float = Form(0.25),
    text_threshold: float = Form(0.25),
) -> JSONResponse:
    if not (prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt is required.")

    try:
        raw = await image.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty image file.")
        pil_image = Image.open(io.BytesIO(raw))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}") from exc

    det = get_detector()
    try:
        result: dict[str, Any] = det.detect(
            pil_image,
            prompt,
            box_threshold=box_threshold,
            text_threshold=text_threshold,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Detection failed: {exc}") from exc

    return JSONResponse(content=result)
