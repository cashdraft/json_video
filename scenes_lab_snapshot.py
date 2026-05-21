"""Фиксированный снимок сцен для /scenes-lab — без чтения jobs при каждом запросе."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from scenes_static_preview import collect_random_ready_scenes

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = BASE_DIR / "data" / "scenes_lab" / "snapshot.json"
MEDIA_DIR = BASE_DIR / "static" / "scenes_lab"
DEFAULT_SNAPSHOT_SEED = 20260520
DEFAULT_SCENE_COUNT = 10

_MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov"}


def _safe_scene_stem(scene_id: str, index: int) -> str:
    stem = re.sub(r"[^\w\-]", "_", (scene_id or "").strip())[:60]
    return stem or f"scene_{index}"


def _guess_ext(url: str, *, video: bool) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext in _MEDIA_EXT:
        return ext
    return ".mp4" if video else ".jpg"


def _download_to(url: str, dest: Path) -> bool:
    if not url or not str(url).strip().startswith(("http://", "https://")):
        return False
    try:
        r = requests.get(url, timeout=90, stream=True)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest.is_file() and dest.stat().st_size > 0
    except (OSError, requests.RequestException):
        return False


def _copy_media_field(
    scene: dict[str, Any],
    field: str,
    stem: str,
    suffix: str,
) -> None:
    url = str(scene.get(field) or "").strip()
    if not url:
        return
    video = suffix == "video"
    ext = _guess_ext(url, video=video)
    fname = f"{stem}_{suffix}{ext}"
    dest = MEDIA_DIR / fname
    if _download_to(url, dest):
        scene[field] = f"/static/scenes_lab/{fname}"
    # иначе оставляем исходный URL (лучше картинка, чем пусто)


def build_scenes_lab_snapshot(
    count: int = DEFAULT_SCENE_COUNT,
    *,
    seed: int = DEFAULT_SNAPSHOT_SEED,
) -> dict[str, Any]:
    """Собрать снимок: 10 сцен + локальные копии медиа в static/scenes_lab/."""
    scenes = collect_random_ready_scenes(count, seed=seed)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, Any]] = []
    for i, row in enumerate(scenes):
        sc = dict(row)
        stem = _safe_scene_stem(str(sc.get("scene_id") or ""), i)
        _copy_media_field(sc, "start_image_url", stem, "start")
        _copy_media_field(sc, "end_image_url", stem, "end")
        _copy_media_field(sc, "video_url", stem, "video")
        copied.append(sc)

    payload: dict[str, Any] = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "scene_count": len(copied),
        "scenes": copied,
    }
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def load_scenes_lab_snapshot() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Вернуть (scenes, meta). Пустой список, если снимок не собран."""
    if not SNAPSHOT_PATH.is_file():
        return [], {}
    try:
        raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return [], {}
    if not isinstance(raw, dict):
        return [], {}
    scenes = raw.get("scenes")
    if not isinstance(scenes, list):
        return [], {}
    meta = {
        "built_at": str(raw.get("built_at") or "").strip(),
        "seed": raw.get("seed"),
        "scene_count": raw.get("scene_count"),
    }
    return [s for s in scenes if isinstance(s, dict)], meta


def main() -> None:
    p = argparse.ArgumentParser(description="Собрать статический снимок для /scenes-lab")
    p.add_argument("--count", type=int, default=DEFAULT_SCENE_COUNT)
    p.add_argument("--seed", type=int, default=DEFAULT_SNAPSHOT_SEED)
    args = p.parse_args()
    out = build_scenes_lab_snapshot(args.count, seed=args.seed)
    print(f"OK: {out['scene_count']} scenes -> {SNAPSHOT_PATH}")
    print(f"Media: {MEDIA_DIR}")


if __name__ == "__main__":
    main()
