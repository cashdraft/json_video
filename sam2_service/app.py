"""FastAPI service: DINO bbox → SAM2 masks."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from dino_to_sam2 import DEFAULT_MIN_SCORE, process_dino_to_sam2
from predictor import Sam2BoxSegmenter
from sam2_auto import process_sam2_auto

_segmenter: Sam2BoxSegmenter | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _segmenter
    _segmenter = Sam2BoxSegmenter()
    yield
    _segmenter = None


app = FastAPI(
    title="SAM2 Service",
    version="1.0.0",
    lifespan=lifespan,
)


def get_segmenter() -> Sam2BoxSegmenter:
    if _segmenter is None:
        raise HTTPException(status_code=503, detail="SAM2 segmenter is not loaded yet.")
    return _segmenter


@app.get("/health")
def health() -> dict[str, str]:
    seg = get_segmenter()
    return {
        "status": "ok",
        "device": seg.device,
        "checkpoint": str(seg.checkpoint.name),
    }


@app.post("/segment_dino")
async def segment_dino(
    image: UploadFile = File(...),
    detections_json: str = Form(...),
    min_score: float = Form(DEFAULT_MIN_SCORE),
) -> JSONResponse:
    try:
        raw = await image.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty image file.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image upload: {exc}") from exc

    if not (detections_json or "").strip():
        raise HTTPException(status_code=400, detail="detections_json is required.")

    seg = get_segmenter()
    try:
        result, log, err = process_dino_to_sam2(
            raw,
            detections_json,
            min_score=float(min_score),
            segmenter=seg,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"segment_dino failed: {exc}") from exc

    if err:
        raise HTTPException(status_code=400, detail=err)

    body: dict[str, Any] = dict(result)
    body["ok"] = True
    body["log"] = log
    return JSONResponse(content=body)


@app.post("/segment_auto")
async def segment_auto(
    image: UploadFile = File(...),
) -> JSONResponse:
    try:
        raw = await image.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty image file.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image upload: {exc}") from exc

    try:
        result, log, err = process_sam2_auto(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"segment_auto failed: {exc}") from exc

    if err:
        raise HTTPException(status_code=400, detail=err)

    body: dict[str, Any] = dict(result)
    body["ok"] = True
    body["log"] = log
    return JSONResponse(content=body)
