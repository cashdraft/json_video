"""
Подготовка ассетов и props.json для Remotion-композиции `JobMontage`.

Каталог: data/job_remotion/<job_id>/
  voiceover.mp3
  media/scene_001.<ext>            (видео > start image > первый выбранный Pexels)
  media/scene_001.kind              ("video" | "image")
  props.json

props.json — JSON со схемой, которую читает Remotion-композиция.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


def _ext_from_url(url: str, fallback: str) -> str:
    path = (urlparse(url).path or "").lower()
    for e in (".png", ".webp", ".jpg", ".jpeg", ".gif", ".mp4", ".webm", ".mov"):
        if path.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return fallback


def _pick_scene_media(scene: dict[str, Any]) -> dict[str, Any] | None:
    """Приоритет: scene.video.video_url → scene.start.image_url → первый выбранный Pexels."""
    if not isinstance(scene, dict):
        return None
    video_blk = scene.get("video") if isinstance(scene.get("video"), dict) else None
    if video_blk:
        v_url = str(video_blk.get("video_url") or "").strip()
        if v_url:
            return {"kind": "video", "source": "scene.video", "url": v_url, "local_path": None}
    start_blk = scene.get("start") if isinstance(scene.get("start"), dict) else None
    if start_blk:
        s_url = str(start_blk.get("image_url") or "").strip()
        if s_url:
            return {"kind": "image", "source": "scene.start", "url": s_url, "local_path": None}
    results = scene.get("pexels_results") if isinstance(scene.get("pexels_results"), list) else []
    sel_raw = scene.get("pexels_selected_indices") if isinstance(scene.get("pexels_selected_indices"), list) else []
    for v in sel_raw:
        try:
            idx = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(results):
            row = results[idx]
            if not isinstance(row, dict):
                continue
            is_video = str(row.get("type") or "").strip().lower() == "video"
            media_url = str(row.get("media_url") or "").strip()
            local_url = str(row.get("local_url") or "").strip()
            return {
                "kind": "video" if is_video else "image",
                "source": "pexels",
                "url": media_url,
                "local_url": local_url,
            }
    return None


def _safe_scene_stem(scene: dict[str, Any], idx0: int) -> str:
    sid = str(scene.get("scene_id") or "").strip()
    if not sid:
        sid = f"scene_{idx0 + 1:03d}"
    out = []
    for ch in sid:
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    base = "".join(out).strip("._") or f"scene_{idx0 + 1:03d}"
    return base


_MONTAGE_ZOOM_MIN = 1.0
_MONTAGE_ZOOM_MAX = 1.5
_MONTAGE_ZOOM_STEP = 0.025
_MONTAGE_ZOOM_MODES = ("alternate", "all_in", "all_out", "random")
_MONTAGE_ZOOM_MODE_DEFAULT = "alternate"


def _montage_zoom_mode_resolve(montage: dict[str, Any]) -> str:
    s = str(montage.get("zoom_mode") or "").strip().lower()
    if s in _MONTAGE_ZOOM_MODES:
        return s
    return _MONTAGE_ZOOM_MODE_DEFAULT


def _montage_zoom_scale_resolve(montage: dict[str, Any]) -> float:
    """Согласовано с app.py: zoom_scale или legacy zoom_pct 0…100 → 1.0…1.5."""
    if montage.get("zoom_scale") is not None and str(montage.get("zoom_scale")).strip() != "":
        try:
            x = float(montage.get("zoom_scale"))
        except (TypeError, ValueError):
            x = _MONTAGE_ZOOM_MIN
    else:
        try:
            zp = int(round(float(montage.get("zoom_pct") or 0)))
        except (TypeError, ValueError):
            zp = 0
        zp = max(0, min(100, zp))
        x = _MONTAGE_ZOOM_MIN + (zp / 100.0) * (_MONTAGE_ZOOM_MAX - _MONTAGE_ZOOM_MIN)
    x = max(_MONTAGE_ZOOM_MIN, min(_MONTAGE_ZOOM_MAX, x))
    n = int(round((x - _MONTAGE_ZOOM_MIN) / _MONTAGE_ZOOM_STEP))
    x = _MONTAGE_ZOOM_MIN + n * _MONTAGE_ZOOM_STEP
    return float(round(min(x, _MONTAGE_ZOOM_MAX), 3))


def _aspect_to_size(aspect: str) -> tuple[int, int]:
    s = str(aspect or "16:9").strip()
    if s == "9:16":
        return 1080, 1920
    if s == "1:1":
        return 1080, 1080
    if s == "4:5":
        return 1080, 1350
    return 1920, 1080


def build_props(
    *,
    job_id: str,
    job: dict[str, Any],
    scenes: list[dict[str, Any]],
    audio_path: Path | None,
    audio_duration_ms: int,
    scene_media: list[dict[str, Any]],
    static_prefix: str,
    fps: int = 30,
) -> dict[str, Any]:
    meta = job.get("job_meta") if isinstance(job.get("job_meta"), dict) else {}
    aspect = str(meta.get("aspect_ratio") or "16:9")
    width, height = _aspect_to_size(aspect)
    montage = meta.get("montage") if isinstance(meta.get("montage"), dict) else {}
    zoom_scale = _montage_zoom_scale_resolve(montage)
    zoom_mode = _montage_zoom_mode_resolve(montage)
    try:
        fade_in_pct = max(0, min(100, int(round(float(montage.get("fade_in_pct") or 0)))))
    except (TypeError, ValueError):
        fade_in_pct = 0

    out_scenes: list[dict[str, Any]] = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            continue
        at = s.get("audio_timing") if isinstance(s.get("audio_timing"), dict) else {}
        try:
            sm = int(at.get("start_ms") or 0)
        except (TypeError, ValueError):
            sm = 0
        try:
            em = int(at.get("end_ms") or 0)
        except (TypeError, ValueError):
            em = 0
        if em < sm:
            em = sm
        media_raw = scene_media[i] if i < len(scene_media) else None
        media_obj: dict[str, Any] | None = None
        if isinstance(media_raw, dict) and media_raw.get("src"):
            local_rel = str(media_raw["src"]).lstrip("/")
            media_obj = {
                "kind": media_raw.get("kind"),
                "source": media_raw.get("source"),
                "src": f"{static_prefix}/{local_rel}",
                "local_path": local_rel,
            }
        out_scenes.append(
            {
                "scene_id": str(s.get("scene_id") or ""),
                "text": str(s.get("text") or ""),
                "text_ru": str(s.get("text_ru") or ""),
                "start_ms": sm,
                "end_ms": em,
                "duration_ms": max(0, em - sm),
                "media": media_obj,
                "low_confidence": bool(at.get("low_confidence")),
            }
        )

    total_ms = audio_duration_ms
    if total_ms <= 0 and out_scenes:
        total_ms = max(int(sc["end_ms"]) for sc in out_scenes)
    if total_ms <= 0:
        total_ms = 1000

    audio_src_static = (
        f"{static_prefix}/{audio_path.name}" if isinstance(audio_path, Path) else None
    )
    return {
        "schema": "job_montage_props@1",
        "job_id": job_id,
        "project_name": str(job.get("project_name") or ""),
        "fps": int(fps),
        "width": int(width),
        "height": int(height),
        "aspect_ratio": aspect,
        "total_duration_ms": int(total_ms),
        "static_prefix": static_prefix,
        "audio": {
            "src": audio_src_static,
            "duration_ms": int(audio_duration_ms),
        },
        "montage": {
            "zoom_scale": zoom_scale,
            "zoom_mode": zoom_mode,
            "fade_in_pct": fade_in_pct,
        },
        "scenes": out_scenes,
    }


def _image_optimize_for_studio(src: Path, target_w: int, target_h: int) -> bool:
    """Опционально (MONTAGE_OPTIMIZE_IMAGES=1) ужимает картинку до композиции, перекодирует в jpg q≈5.

    Цель — снизить размер блобов в превью Remotion Studio (картинки 0.5–1 МБ тормозят на сик).
    Уменьшает только если изображение больше композиции; пропорции сохраняем (object-fit: cover делает остальное).
    На рендер влияния минимальное: 1920×1080 при q=5 ≈ 200–300 КБ.
    """
    if (os.getenv("MONTAGE_OPTIMIZE_IMAGES") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not src.is_file():
        return False
    try:
        w = max(2, int(target_w))
        h = max(2, int(target_h))
    except (TypeError, ValueError):
        return False
    tmp = src.with_suffix(src.suffix + ".opt.jpg")
    vf = (
        f"scale='min({w},iw)':-2:force_original_aspect_ratio=decrease,"
        f"scale='-2':'min({h},ih)':force_original_aspect_ratio=decrease"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(src),
        "-vf", vf,
        "-q:v", "5",
        "-map_metadata", "-1",
        str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        os.replace(tmp, src)
    except OSError:
        return False
    return True


def _transcode_voiceover_for_studio(src: Path, dst: Path) -> bool:
    """Конвертирует озвучку в WAV PCM s16le 44.1 kHz stereo.

    Studio (`mediabunny`) для отрисовки волны на таймлайне вызывает
    `AudioDecoder.isConfigSupported(...)`. В части браузеров `codec: 'mp3'` не
    поддерживается и Studio показывает ошибку
    "This audio track cannot be decoded by this browser". WAV PCM mediabunny
    декодирует сам (см. `input-track.ts`: `codec.startsWith('pcm-') → true`),
    поэтому такой формат работает везде. На рендер влияния нет — Remotion
    подаёт wav в ffmpeg как обычно.

    Возвращает True при успехе. На ошибку — оставляет dst пустым, False.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg,
        "-y",
        "-i", str(src),
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-c:a", "pcm_s16le",
        "-map_metadata", "-1",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0


def _audio_duration_ms_ffprobe(mp3_path: Path) -> int:
    try:
        from elevenlabs_client import mp3_duration_seconds_ffprobe
    except Exception:
        return 0
    try:
        return int(round(mp3_duration_seconds_ffprobe(mp3_path) * 1000))
    except Exception:
        return 0


def prepare_montage(
    *,
    job_id: str,
    job: dict[str, Any],
    base_dir: Path,
    audio_src: Path | None,
    pexels_dir: Path | None,
    fetch_url_bytes: Callable[..., bytes | None],
    remotion_public_dir: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Готовит ассеты и `props.json` для Remotion. Возвращает финальный props (dict).
    """
    def push(stage: str, **kw: Any) -> None:
        if progress is None:
            return
        payload = {"stage": stage, "ts": time.time(), **kw}
        try:
            progress(payload)
        except Exception:
            pass

    base_dir.mkdir(parents=True, exist_ok=True)
    media_dir = base_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    # Audio
    audio_target: Path | None = None
    audio_duration_ms = 0
    if isinstance(audio_src, Path) and audio_src.is_file():
        src_name = audio_src.name
        push(
            "audio_prepare",
            source=src_name,
            target="voiceover.wav",
            detail="ffmpeg: PCM s16le, 44.1 kHz, stereo (для Remotion Studio и рендера)",
        )
        audio_target = base_dir / "voiceover.wav"
        ok = _transcode_voiceover_for_studio(audio_src, audio_target)
        if not ok:
            push(
                "audio_fallback",
                source=src_name,
                target="voiceover.mp3",
                detail="ffmpeg недоступен или ошибка — копирую исходный MP3 без перекодирования",
            )
            audio_target = base_dir / "voiceover.mp3"
            try:
                shutil.copyfile(audio_src, audio_target)
            except OSError:
                audio_target = None
        if audio_target and audio_target.is_file():
            audio_duration_ms = _audio_duration_ms_ffprobe(audio_target)
            push(
                "audio_done",
                filename=audio_target.name,
                duration_ms=audio_duration_ms,
                source=src_name,
                transcoded=bool(ok),
                format=("wav" if audio_target.suffix.lower() == ".wav" else "mp3"),
            )
        else:
            push("audio_missing", source=src_name, detail="не удалось записать voiceover.wav / voiceover.mp3")
    else:
        push("audio_missing", source=None, detail="нет файла озвучки в data/job_audio/<job_id>/")

    scenes = job.get("scenes") if isinstance(job.get("scenes"), list) else []
    total = len(scenes)

    scene_media: list[dict[str, Any] | None] = []
    for i, s in enumerate(scenes):
        if cancel_check is not None and cancel_check():
            push("cancelled", at_scene=i)
            return build_props(
                job_id=job_id,
                job=job,
                scenes=scenes,
                audio_path=audio_target,
                audio_duration_ms=audio_duration_ms,
                scene_media=[m or {} for m in scene_media] + [{} for _ in range(total - len(scene_media))],
            )
        stem = _safe_scene_stem(s, i)
        pick = _pick_scene_media(s if isinstance(s, dict) else {})
        push(
            "scene_start",
            index=i,
            total=total,
            scene_id=str((s or {}).get("scene_id") or ""),
            stem=stem,
            kind=(pick or {}).get("kind"),
            source=(pick or {}).get("source"),
        )
        if not pick:
            scene_media.append(None)
            push("scene_done", index=i, total=total, scene_id=str((s or {}).get("scene_id") or ""), kind=None)
            continue

        kind = pick["kind"]
        source = pick["source"]
        target_path: Path | None = None

        if source == "pexels":
            local_url = pick.get("local_url") or ""
            prefix = f"/job/{job_id}/pexels/"
            local_file: Path | None = None
            if pexels_dir and isinstance(local_url, str) and local_url.startswith(prefix):
                fname = local_url[len(prefix):]
                cand = (pexels_dir / fname).resolve()
                try:
                    cand.relative_to(pexels_dir.resolve())
                    if cand.is_file():
                        local_file = cand
                except ValueError:
                    local_file = None
            if local_file:
                ext = local_file.suffix or (".mp4" if kind == "video" else ".jpg")
                target_path = media_dir / f"{stem}{ext}"
                try:
                    shutil.copyfile(local_file, target_path)
                except OSError:
                    target_path = None

        if target_path is None:
            url = str(pick.get("url") or "").strip()
            if url:
                ext = _ext_from_url(url, ".mp4" if kind == "video" else ".jpg")
                target_path = media_dir / f"{stem}{ext}"
                data = fetch_url_bytes(url)
                if data:
                    try:
                        target_path.write_bytes(data)
                    except OSError:
                        target_path = None
                else:
                    target_path = None

        if target_path and target_path.is_file():
            if kind == "image":
                meta = job.get("job_meta") if isinstance(job.get("job_meta"), dict) else {}
                aspect = str(meta.get("aspect_ratio") or "16:9")
                tw, th = _aspect_to_size(aspect)
                _image_optimize_for_studio(target_path, tw, th)
            rel = f"media/{target_path.name}"
            scene_media.append({"kind": kind, "source": source, "src": rel})
            push("scene_done", index=i, total=total, scene_id=str((s or {}).get("scene_id") or ""), kind=kind, src=rel)
        else:
            scene_media.append(None)
            push(
                "scene_fail",
                index=i,
                total=total,
                scene_id=str((s or {}).get("scene_id") or ""),
                reason="copy_or_download_failed",
            )

    static_prefix = f"jobs/{job_id}"
    props = build_props(
        job_id=job_id,
        job=job,
        scenes=scenes,
        audio_path=audio_target,
        audio_duration_ms=audio_duration_ms,
        scene_media=[m or {} for m in scene_media],
        static_prefix=static_prefix,
    )
    props_path = base_dir / "props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")

    if remotion_public_dir is not None:
        try:
            remotion_public_dir.mkdir(parents=True, exist_ok=True)
            link_path = remotion_public_dir / job_id
            try:
                if link_path.is_symlink() or link_path.exists():
                    if link_path.is_symlink() or link_path.is_file():
                        link_path.unlink()
                    else:
                        shutil.rmtree(link_path, ignore_errors=True)
            except OSError:
                pass
            try:
                link_path.symlink_to(base_dir.resolve(), target_is_directory=True)
            except OSError:
                shutil.copytree(base_dir, link_path)
        except OSError as exc:
            push("public_link_fail", error=str(exc))

    push("props_written", filename="props.json", scenes=len(props["scenes"]))
    return props
