"""Сбор случайных готовых сцен с диска для статической демо-страницы (без привязки к проекту)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"
DEFAULT_JOB_RESOLUTION = "1K"


def _slot_image_meta(meta: dict[str, Any]) -> str:
    res = str(meta.get("resolution") or DEFAULT_JOB_RESOLUTION).strip()
    label = str(meta.get("image_model_label") or meta.get("image_model") or "").strip()
    return f"{res} · {label}" if label else res


def _slot_video_meta(meta: dict[str, Any]) -> str:
    return str(meta.get("video_model_label") or meta.get("video_model") or "").strip()


def _load_job_scenes(path: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    if ".lock" in path.name:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    scenes = raw.get("scenes")
    if not isinstance(scenes, list):
        return None
    meta = raw.get("job_meta") if isinstance(raw.get("job_meta"), dict) else {}
    return path.stem, scenes, meta


def _scene_is_ready(scene: dict[str, Any]) -> bool:
    if not isinstance(scene, dict):
        return False
    start = scene.get("start") if isinstance(scene.get("start"), dict) else {}
    url = str(start.get("image_url") or "").strip()
    prompt = str(start.get("prompt") or "").strip()
    return bool(url and prompt)


def _scene_to_display(scene: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    start = scene.get("start") if isinstance(scene.get("start"), dict) else {}
    end = scene.get("end") if isinstance(scene.get("end"), dict) else {}
    video = scene.get("video") if isinstance(scene.get("video"), dict) else {}
    at = scene.get("audio_timing") if isinstance(scene.get("audio_timing"), dict) else {}

    img_meta = _slot_image_meta(meta)
    vid_label = _slot_video_meta(meta)

    duration_ms = at.get("duration_ms")
    try:
        duration_ms_int = int(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_ms_int = None

    duration_s = str(at.get("duration_s") or "").strip()
    if not duration_s and duration_ms_int is not None:
        duration_s = f"{duration_ms_int / 1000:.2f}".rstrip("0").rstrip(".")

    return {
        "scene_id": str(scene.get("scene_id") or "scene").strip(),
        "text": str(scene.get("text") or "").strip(),
        "text_ru": str(scene.get("text_ru") or "").strip(),
        "start_prompt": str(start.get("prompt") or "").strip(),
        "video_prompt": str(video.get("prompt") or "").strip(),
        "start_image_url": str(start.get("image_url") or "").strip(),
        "end_image_url": str(end.get("image_url") or "").strip(),
        "video_url": str(video.get("video_url") or "").strip(),
        "audio_timing": at,
        "duration_s": duration_s,
        "duration_ms": duration_ms_int,
        "duration_ms_display": (
            str(duration_ms_int) if duration_ms_int is not None else ""
        ),
        "start_time": str(at.get("start_time") or "").strip(),
        "start_end": str(at.get("start_end") or "").strip(),
        "scene_slot_image_header_meta": img_meta,
        "scene_slot_video_header_meta": vid_label,
        "has_start_prompt": bool(str(start.get("prompt") or "").strip()),
        "has_video_prompt": bool(str(video.get("prompt") or "").strip()),
        "has_start_image": bool(str(start.get("image_url") or "").strip()),
        "has_end_image": bool(str(end.get("image_url") or "").strip()),
        "has_video": bool(str(video.get("video_url") or "").strip()),
    }


def collect_random_ready_scenes(
    count: int = 10,
    *,
    seed: int | None = None,
    jobs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """До ``count`` случайных сцен с готовым start-кадром и промптом (разные job на диске)."""
    root = jobs_dir or JOBS_DIR
    if not root.is_dir():
        return []

    with_timing: list[dict[str, Any]] = []
    without_timing: list[dict[str, Any]] = []
    rng = random.Random(seed)

    for path in sorted(root.glob("job_*.json")):
        loaded = _load_job_scenes(path)
        if not loaded:
            continue
        _job_id, scenes, meta = loaded
        for scene in scenes:
            if not _scene_is_ready(scene):
                continue
            row = _scene_to_display(scene, meta)
            at = row.get("audio_timing") if isinstance(row.get("audio_timing"), dict) else {}
            if at.get("duration_ms") is not None or at.get("start_ms") is not None:
                with_timing.append(row)
            else:
                without_timing.append(row)

    rng.shuffle(with_timing)
    rng.shuffle(without_timing)
    pool = with_timing + without_timing
    return pool[: max(0, count)]
