#!/usr/bin/env python3
"""
JSON Video Generator - First Page
Web interface for parsing scene JSON and preparing for image/video generation.
"""

from __future__ import annotations

import copy
import difflib
from html import escape as html_escape
import json
import mimetypes
import os
import queue
from contextlib import contextmanager

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

from dotenv import load_dotenv

# Загружаем .env из каталога приложения (не из cwd): systemd/uwsgi могут иметь другой cwd.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

from flask import (
    Flask,
    Response,
    abort,
    flash,
    has_request_context,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
    url_for,
)
import requests
from yt_dlp import YoutubeDL

from image_templates import (
    IMAGE_TEMPLATES_DIR,
    build_image_input_urls,
    collect_reference_and_logo,
    list_templates,
    safe_template_dir,
)
from elevenlabs_client import (
    TTS_MODELS,
    chars_to_words_ms,
    list_voices as elevenlabs_list_voices,
    max_chars_for_model,
    merge_mp3_files_ffmpeg,
    mp3_duration_seconds_ffprobe,
    split_tts_text_into_chunks,
    text_to_speech_bytes,
    text_to_speech_with_timestamps,
)
from job_scene_audio_align import align_scenes_to_word_timings, merge_audio_timing_into_scenes
from elevenlabs_templates import (
    list_elevenlabs_template_names,
    load_elevenlabs_template,
    save_elevenlabs_template,
)
from kie_client import (
    create_grok_image_to_video_task,
    create_image_task,
    create_video_task,
    get_task_result,
    get_video_task_result,
    get_video_1080p_result,
    normalize_aspect_ratio,
)
from rewrite_openai import (
    REWRITE_DEFAULT_MODEL,
    REWRITE_MODELS,
    iter_draft1_blockwise_completion,
    iter_rewrite_completion,
    iter_rewrite_completion_stream,
    list_draft1_wire_chat_payloads_for_export,
    normalize_rewrite_model,
    rewrite_chat_completion_wire_payload,
)
from rewrite_pipeline import (
    REWRITE_PRESET_DEFAULT,
    REWRITE_PRESET_KEYS,
    REWRITE_PRESET_LABELS,
    REWRITE_PRESET_PREWRITTEN,
    REWRITE_PRESET_STAGE_KEYS,
    REWRITE_STAGE_HELP_HINTS,
    REWRITE_STAGE_KEYS,
    REWRITE_STAGE_SEND_HINTS,
    REWRITE_STAGE_SUBTITLES,
    REWRITE_STAGES,
    _extract_edited_text,
    any_stage_has_result,
    clamp_target_chars,
    apply_title_strategist_original_title_to_user_json,
    compose_rewrite_openai_request_body,
    normalize_rewrite_preset,
    snapshot_rewrite_preset_from_body,
    stages_for_preset,
    merge_stages_from_request,
    new_stages_dict,
    normalize_rewrite_job_data,
    snapshot_master_prompt_from_body,
    snapshot_original_title_from_body,
    snapshot_pipeline_extras_from_body,
    snapshot_stages_from_body,
    stage_run_prerequisites_met,
    strip_author_stream_end_marker,
)
from rewrite_templates import (
    REWRITE_TEMPLATES_DIR,
    list_rewrite_template_names,
    load_rewrite_template,
    save_rewrite_template_to_disk,
)
from claude_kie import strip_markdown_code_fence
from task_manager import (
    cancel_task as _tm_cancel_task,
    get_active_task as _tm_get_active_task,
    get_task_meta as _tm_get_task_meta,
    list_active_tasks_for_project as _tm_list_active_tasks_for_project,
    mark_orphan_running_as_interrupted as _tm_mark_orphan_running_as_interrupted,
    start_task as _tm_start_task,
    subscribe_events as _tm_subscribe_events,
)

# --- Paths ---
JOBS_DIR = BASE_DIR / "data" / "jobs"
JOB_AUDIO_DIR = BASE_DIR / "data" / "job_audio"
JOB_PEXELS_DIR = BASE_DIR / "data" / "job_pexels"
REWRITE_JOBS_DIR = BASE_DIR / "data" / "rewrite_jobs"
REWRITE_MEDIA_DIR = BASE_DIR / "data" / "rewrite_media"


def _sanitize_scene_deprecated(scene: dict[str, Any]) -> None:
    """Удаляет поля снятых фич: animation, prompt_master, prompt_master_render, …"""
    if not isinstance(scene, dict):
        return
    scene.pop("animation", None)
    for k in list(scene.keys()):
        if isinstance(k, str) and k.startswith("prompt_master"):
            scene.pop(k, None)


def _sanitize_job_scenes(job: dict[str, Any] | None) -> None:
    if not isinstance(job, dict):
        return
    scenes = job.get("scenes")
    if not isinstance(scenes, list):
        return
    for s in scenes:
        if isinstance(s, dict):
            _sanitize_scene_deprecated(s)


# Serialize read-modify-write on the same job JSON. Without this, concurrent
# requests (bulk ↻ video, overlapping polls) can last-write-win and drop e.g.
# start.image_url while the UI still shows a thumbnail from an earlier response.
_fcntl_job_locks_guard = threading.Lock()
_fcntl_job_locks: dict[str, threading.Lock] = {}


@contextmanager
def _job_file_lock(job_id: str):
    jid = (job_id or "").strip()
    if not jid:
        yield
        return

    if fcntl is not None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = JOBS_DIR / f"{jid}.json.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        return

    with _fcntl_job_locks_guard:
        lk = _fcntl_job_locks.get(jid)
        if lk is None:
            lk = threading.Lock()
            _fcntl_job_locks[jid] = lk
    lk.acquire()
    try:
        yield
    finally:
        lk.release()


_REWRITE_ID_RE = re.compile(r"^rewrite_\d{8}_\d{6}$")


def _latest_tts_words_doc_for_job(job_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Последний по mtime MP3 в data/job_audio/<job_id>/ и парный <stem>.words.json."""
    audio_dir = JOB_AUDIO_DIR / job_id
    if not audio_dir.is_dir():
        return None, None
    mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3s:
        return None, None
    mp3 = mp3s[0]
    words_path = audio_dir / f"{mp3.stem}.words.json"
    if not words_path.is_file():
        return None, None
    try:
        doc = json.loads(words_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(doc, dict):
        return None, None
    return doc, mp3.name


def _render_scenes_stripped_with_timing(scenes: list[dict]) -> str:
    """Компактный «JSON-код сцен с таймингами»: 8 строк-объектов на сцену.
    Если ни у одной сцены нет `audio_timing` — возвращает пустую строку (UI скрывает блок).
    """
    if not scenes:
        return ""
    has_any_timing = any(
        isinstance(s, dict) and isinstance(s.get("audio_timing"), dict) and s["audio_timing"].get("start_ms") is not None
        for s in scenes
    )
    if not has_any_timing:
        return ""

    def js(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)

    out: list[str] = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        at = s.get("audio_timing") if isinstance(s.get("audio_timing"), dict) else {}
        sm = at.get("start_ms")
        em = at.get("end_ms")
        dm = at.get("duration_ms")
        st_time = at.get("start_time") or ""
        en_time = at.get("start_end") or ""
        dur_s = at.get("duration_s") or ""
        if not st_time and isinstance(sm, (int, float)):
            from job_scene_audio_align import format_ms_clock as _f
            st_time = _f(int(sm))
        if not en_time and isinstance(em, (int, float)):
            from job_scene_audio_align import format_ms_clock as _f
            en_time = _f(int(em))
        if not dur_s and isinstance(dm, (int, float)):
            from job_scene_audio_align import format_duration_seconds as _f
            dur_s = _f(int(dm))

        out.append(f'{{"scene_id": {js(str(s.get("scene_id") or ""))}}}')
        out.append(f'{{"text": {js(str(s.get("text") or ""))}}}')
        out.append(f'{{"text_ru": {js(str(s.get("text_ru") or ""))}}}')
        out.append(f'{{"start_time_ms": {js(str(sm) if sm is not None else "")}}}')
        out.append(f'{{"start_end_ms": {js(str(em) if em is not None else "")}}}')
        out.append(f'{{"start_time": {js(str(st_time))}}}')
        out.append(f'{{"start_end": {js(str(en_time))}}}')
        out.append(f'{{"Duration": {js(str(dur_s))}}}')
        out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _apply_tts_word_timings_to_scenes(job_id: str, scenes: list[dict]) -> None:
    """Если у последнего TTS есть words.json — выравнивает сцены и пишет audio_timing (на месте)."""
    if not scenes:
        return
    words_doc, audio_fname = _latest_tts_words_doc_for_job(job_id)
    if not words_doc or not audio_fname:
        return
    words = words_doc.get("words")
    if not isinstance(words, list) or not words:
        return
    try:
        total_ms = int(words_doc.get("total_duration_ms") or 0)
    except (TypeError, ValueError):
        total_ms = 0
    if total_ms <= 0:
        try:
            last = words[-1]
            total_ms = int((last or {}).get("end_ms") or 0)
        except (TypeError, ValueError, IndexError):
            total_ms = 0
    if total_ms <= 0:
        return
    try:
        timings = align_scenes_to_word_timings(
            scenes,
            words,
            total_duration_ms=total_ms,
        )
        merge_audio_timing_into_scenes(scenes, timings, audio_filename=audio_fname)
    except Exception as exc:  # noqa: BLE001
        try:
            app.logger.warning("apply TTS timings to scenes job=%s: %s", job_id, exc)
        except Exception:
            pass


def _safe_job_audio_filename(name: str) -> bool:
    # Разрешаем озвучку (.mp3) и парный JSON с пословными таймингами (.words.json),
    # который ElevenLabs `/with-timestamps` отдаёт рядом с MP3.
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.(mp3|words\.json)$", name))


def _safe_zip_archive_basename(name: str, fallback: str) -> str:
    base = re.sub(r"[^\w\-. ()\[\]]+", "_", (name or "").strip())
    base = base.strip("._- ")[:120] or fallback
    return base


def _archive_scene_basename(scene: dict[str, Any], idx0: int) -> str:
    sid = str(scene.get("scene_id") or "").strip()
    if not sid:
        sid = f"scene_{idx0 + 1:03d}"
    sid = re.sub(r"[^\w.\-]+", "_", sid).strip("._") or f"scene_{idx0 + 1:03d}"
    return sid


def _job_pexels_dir(job_id: str) -> Path:
    return JOB_PEXELS_DIR / job_id


def _media_ext_from_url(url: str, slot: str) -> str:
    path = (urlparse(url).path or "").lower()
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return ".jpg"
    if path.endswith(".gif"):
        return ".gif"
    if path.endswith(".mp4"):
        return ".mp4"
    if path.endswith(".webm"):
        return ".webm"
    return ".mp4" if slot == "video" else ".png"


_MEDIA_FETCH_MAX_BYTES = 120 * 1024 * 1024
PEXELS_API_KEY = (os.getenv("PEXELS_API_KEY") or "").strip()


def _fetch_url_bytes_capped(
    url: str,
    on_progress: Callable[[int], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> bytes | None:
    u = (url or "").strip()
    if not u.startswith(("https://", "http://")):
        return None
    try:
        with requests.get(u, timeout=180, stream=True) as r:
            r.raise_for_status()
            total = 0
            parts: list[bytes] = []
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if should_abort is not None and should_abort():
                    return None
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MEDIA_FETCH_MAX_BYTES:
                    return None
                parts.append(chunk)
                if on_progress is not None:
                    on_progress(total)
            return b"".join(parts)
    except (requests.RequestException, OSError):
        return None


def _pexels_search_assets(
    *,
    keywords: str,
    content_type: str,
    target_aspect_ratio: str = "16:9",
    per_page: int = 8,
) -> tuple[list[dict[str, Any]], str | None]:
    key = (PEXELS_API_KEY or "").strip()
    if not key:
        return [], "Не задан PEXELS_API_KEY в .env."
    q = str(keywords or "").strip()
    if not q:
        return [], "Пустые keywords."
    ct = str(content_type or "photos").strip().lower()
    if ct not in ("photos", "videos"):
        ct = "photos"
    pp = max(1, min(80, int(per_page or 8)))
    def _target_orientation(ar: str) -> str:
        s = str(ar or "").strip()
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", s)
        if not m:
            return "landscape"
        try:
            w = float(m.group(1))
            h = float(m.group(2))
        except (TypeError, ValueError):
            return "landscape"
        if w > h:
            return "landscape"
        if h > w:
            return "portrait"
        return "any"

    def _orientation_ok(w: int, h: int, want: str) -> bool:
        if w <= 0 or h <= 0:
            return False
        if want == "any":
            return True
        if want == "landscape":
            return w >= h
        if want == "portrait":
            return h >= w
        return True

    want_orient = _target_orientation(target_aspect_ratio)

    try:
        if ct == "videos":
            url = "https://api.pexels.com/videos/search"
            r = requests.get(
                url,
                headers={"Authorization": key},
                params={"query": q, "per_page": pp, "page": 1},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
            rows = data.get("videos") if isinstance(data, dict) else []
            out: list[dict[str, Any]] = []
            if isinstance(rows, list):
                for v in rows:
                    if not isinstance(v, dict):
                        continue
                    files = v.get("video_files") if isinstance(v.get("video_files"), list) else []
                    mp4_url = ""
                    pick_w = 0
                    pick_h = 0
                    pick_area = 0
                    for f in files:
                        if not isinstance(f, dict):
                            continue
                        link = str(f.get("link") or "")
                        ftype = str(f.get("file_type") or "")
                        fw = int(f.get("width") or 0)
                        fh = int(f.get("height") or 0)
                        if not (link and ("mp4" in ftype.lower() or link.lower().endswith(".mp4"))):
                            continue
                        if not _orientation_ok(fw, fh, want_orient):
                            continue
                        area = fw * fh
                        if area > pick_area:
                            pick_area = area
                            pick_w = fw
                            pick_h = fh
                            mp4_url = link
                    if not mp4_url:
                        vw = int(v.get("width") or 0)
                        vh = int(v.get("height") or 0)
                        if not _orientation_ok(vw, vh, want_orient):
                            continue
                        # fallback: берем любой mp4, если прошла проверка ориентации на уровне видео
                        for f in files:
                            if not isinstance(f, dict):
                                continue
                            link = str(f.get("link") or "")
                            ftype = str(f.get("file_type") or "")
                            if link and ("mp4" in ftype.lower() or link.lower().endswith(".mp4")):
                                mp4_url = link
                                pick_w = vw
                                pick_h = vh
                                break
                    img = str(v.get("image") or "")
                    if not mp4_url and not img:
                        continue
                    out.append(
                        {
                            "type": "video",
                            "thumbnail_url": img,
                            "media_url": mp4_url,
                            "source_url": str(v.get("url") or ""),
                            "author": str((v.get("user") or {}).get("name") or ""),
                            "width": pick_w,
                            "height": pick_h,
                        }
                    )
                    if len(out) >= pp:
                        break
            return out, None
        url = "https://api.pexels.com/v1/search"
        r = requests.get(
            url,
            headers={"Authorization": key},
            params={"query": q, "per_page": pp, "page": 1},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json() if r.content else {}
        rows = data.get("photos") if isinstance(data, dict) else []
        out2: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for p in rows:
                if not isinstance(p, dict):
                    continue
                pw = int(p.get("width") or 0)
                ph = int(p.get("height") or 0)
                # Требование: фото >= 2000px хотя бы по одной стороне.
                if max(pw, ph) < 2000:
                    continue
                # Ориентация — под текущий Aspect Ratio проекта.
                if not _orientation_ok(pw, ph, want_orient):
                    continue
                src = p.get("src") if isinstance(p.get("src"), dict) else {}
                img = str(src.get("large2x") or src.get("large") or src.get("original") or "")
                thumb = str(src.get("medium") or src.get("small") or img)
                if not img and not thumb:
                    continue
                out2.append(
                    {
                        "type": "photo",
                        "thumbnail_url": thumb,
                        "media_url": img or thumb,
                        "source_url": str(p.get("url") or ""),
                        "author": str(p.get("photographer") or ""),
                        "width": pw,
                        "height": ph,
                    }
                )
                if len(out2) >= pp:
                    break
        return out2, None
    except requests.RequestException as e:
        return [], f"Pexels API error: {e}"


def _split_keywords(raw: str) -> list[str]:
    txt = str(raw or "")
    parts = [p.strip() for p in re.split(r"[,;\n]+", txt) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _normalize_keyword_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        s = str(x or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _norm_kw_key(s: str) -> str:
    """Сопоставление keyword в UI / excluded с учётом дефисов и пробелов."""
    t = str(s or "").strip().lower().replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Large scene batches can exceed Werkzeug's form defaults.
# Allow bigger payloads for `/parse` and similar form submissions.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 64 * 1024 * 1024
# Если nginx не отдаёт /static/ с того же хоста: STATIC_STYLE_HREF=https://…/static/style.css
app.config["STATIC_STYLE_HREF"] = (os.getenv("STATIC_STYLE_HREF") or "").strip()
GENERATION_TASKS: dict[str, dict] = {}


def public_base_url_for_kie() -> str:
    """Базовый URL этого приложения, доступный из интернета (Kie скачивает image_input по URL)."""
    b = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if b:
        return b
    if has_request_context():
        return request.url_root.rstrip("/")
    return ""


def templates_ui_rows() -> list[dict]:
    rows = list_templates()
    for r in rows:
        lf = r.get("logo_file")
        if lf:
            r["logo_url"] = url_for(
                "template_assets",
                template_name=r["folder_name"],
                filename=lf,
            )
        else:
            r["logo_url"] = None
    return rows


def job_template_display(folder_name: str) -> dict:
    """Контекст для страницы проекта: превью выбранного шаблона."""
    name = (folder_name or "").strip()
    if not name:
        return {"kind": "none", "folder_name": ""}
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, name)
    if not td:
        return {"kind": "missing", "folder_name": name}
    _refs, logo = collect_reference_and_logo(td)
    logo_url = (
        url_for("template_assets", template_name=name, filename=logo.name)
        if logo
        else None
    )
    return {"kind": "ok", "folder_name": name, "logo_url": logo_url}


@app.route("/template-assets/<template_name>/<path:filename>")
def template_assets(template_name: str, filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(404)
    if "/" in template_name or "\\" in template_name:
        abort(404)
    d = safe_template_dir(IMAGE_TEMPLATES_DIR, template_name)
    if not d:
        abort(404)
    target = (d / filename).resolve()
    try:
        target.relative_to(d.resolve())
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    return send_from_directory(d, filename, max_age=86400)


# --- Parsing logic ---

def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_voiceover_plain_text(raw_text: str) -> str:
    txt = str(raw_text or "")
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        return txt
    if isinstance(obj, dict):
        edited = obj.get("edited_text")
        if isinstance(edited, str) and edited.strip():
            return edited
    return txt


def _parse_structure_splitter_blocks(raw_text: str) -> list[dict]:
    blocks, _err = _parse_structure_splitter_blocks_with_error(raw_text)
    return blocks


def _parse_structure_splitter_blocks_with_error(raw_text: str) -> tuple[list[dict], str | None]:
    txt = str(raw_text or "").strip()
    if not txt:
        return [], "empty_result"

    # Accept common manual-edit format: fenced markdown with JSON inside.
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", txt, re.IGNORECASE)
    if fence_match:
        txt = str(fence_match.group(1) or "").strip()

    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError as e:
        return [], f"json_decode_error:{e.msg} at line {e.lineno}, col {e.colno}"

    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)], None
    if isinstance(parsed, dict) and isinstance(parsed.get("blocks"), list):
        return [x for x in parsed.get("blocks") if isinstance(x, dict)], None
    return [], "json_is_not_list_or_blocks_object"


def _build_structure_splitter_check(input_text: str, splitter_result_text: str) -> dict[str, Any]:
    blocks = _parse_structure_splitter_blocks(splitter_result_text)
    input_txt = str(input_text or "")
    joined = "".join(str((b or {}).get("text") or "") for b in blocks)
    input_compact = re.sub(r"\s+", "", input_txt)
    output_compact = re.sub(r"\s+", "", joined)
    input_chars = len(input_txt)
    output_chars = len(joined)
    delta_chars = output_chars - input_chars
    input_compact_chars = len(input_compact)
    output_compact_chars = len(output_compact)
    delta_compact_chars = output_compact_chars - input_compact_chars
    has_blocks = len(blocks) > 0
    has_output_text = output_compact_chars > 0
    structure_ok = has_blocks and has_output_text
    return {
        "type": "structure_splitter_check",
        "summary": {
            "blocks": len(blocks),
            "input_chars": input_chars,
            "output_chars": output_chars,
            "delta_chars": delta_chars,
            "input_compact_chars": input_compact_chars,
            "output_compact_chars": output_compact_chars,
            "delta_compact_chars": delta_compact_chars,
            # Для Structure Splitter считаем проверку пройденной, если пришла валидная
            # структура с блоками и непустым текстом; дельту показываем информационно.
            "ok": structure_ok,
            "ok_compact": structure_ok,
            "strict_ok": input_chars == output_chars,
            "strict_ok_compact": input_compact_chars == output_compact_chars,
        },
    }


def _build_block_writer_check(completed_blocks: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for i, b in enumerate(completed_blocks or [], start=1):
        idx = int(b.get("block_index") or i)
        target = int(b.get("target_chars_ideal") or 0)
        # Chars OUT считаем только по реальному block_text, чтобы совпадало с итоговым full_text.
        out_chars = len(str(b.get("block_text") or ""))
        delta = out_chars - target
        short_summary = b.get("short_summary") if isinstance(b.get("short_summary"), list) else []
        sum_ok = len(short_summary) > 0
        rows.append(
            {
                "index": idx,
                "sum_ok": bool(sum_ok),
                "target_chars": target,
                "out_chars": out_chars,
                "delta": delta,
            }
        )
    return {
        "type": "block_writer_check",
        "summary": {
            "blocks": len(rows),
            "ok": len(rows) > 0 and all(bool(r.get("sum_ok")) for r in rows),
        },
        "blocks_info": rows,
    }


def _scene_writer_block_check(block: dict[str, Any], part_text: str, idx: int) -> dict[str, Any]:
    block_text = str(block.get("text") or block.get("block_text") or "")
    scenes, _ = parse_scene_blocks(part_text or "")
    start_count = 0
    end_count = 0
    video_count = 0
    char_total = 0
    for s in scenes:
        t = str(s.get("text") or "")
        char_total += len(t)
        if str(((s.get("start") or {}).get("prompt") or "")).strip():
            start_count += 1
        if str(((s.get("end") or {}).get("prompt") or "")).strip():
            end_count += 1
        if str(((s.get("video") or {}).get("prompt") or "")).strip():
            video_count += 1
    merged_scene_text = "\n".join(str(s.get("text") or "") for s in scenes)
    ok = _norm_ws(merged_scene_text) == _norm_ws(block_text)
    avg_chars = (char_total / len(scenes)) if scenes else 0.0
    return {
        "index": idx,
        "block_chars": len(block_text),
        "scenes": len(scenes),
        "with_start": start_count,
        "with_end": end_count,
        "with_video": video_count,
        "avg_scene_chars": round(avg_chars, 1),
        "ok": ok,
    }


def _scene_media_batch_check(
    input_scenes: list[dict[str, Any]],
    part_text: str,
    idx: int,
    *,
    content_type: str,
) -> dict[str, Any]:
    scenes_out, _ = parse_scene_blocks(part_text or "")
    in_count = len(input_scenes or [])
    out_count = len(scenes_out or [])
    in_chars = sum(len(str((s or {}).get("text") or "")) for s in (input_scenes or []))
    out_chars = sum(len(str((s or {}).get("text") or "")) for s in (scenes_out or []))
    with_ct = 0
    for s in scenes_out:
        if content_type == "videos":
            slot = s.get("video")
            ok = isinstance(slot, dict) and bool(str(slot.get("prompt") or "").strip())
        elif content_type == "mixed":
            v = s.get("video")
            st = s.get("start")
            ok = (
                (isinstance(v, dict) and bool(str(v.get("prompt") or "").strip()))
                or (isinstance(st, dict) and bool(str(st.get("prompt") or "").strip()))
            )
        else:
            slot = s.get("start")
            ok = isinstance(slot, dict) and bool(str(slot.get("prompt") or "").strip())
        if ok:
            with_ct += 1
    return {
        "index": idx,
        "input_scenes": in_count,
        "output_scenes": out_count,
        "input_chars": in_chars,
        "output_chars": out_chars,
        "with_target_content": with_ct,
        "ok": in_count > 0 and out_count == in_count,
    }


def _inject_past_prompt_into_scene_json_lines(raw_text: str, past_prompt: str) -> str:
    """Prepends past_prompt to non-empty start/end prompts in line-delimited scene JSON."""
    txt = str(raw_text or "")
    pp = str(past_prompt or "").strip()
    if not txt.strip() or not pp:
        return txt

    out_lines: list[str] = []
    changed = False
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            out_lines.append(ln)
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            out_lines.append(ln)
            continue
        if not isinstance(obj, dict):
            out_lines.append(ln)
            continue

        for slot_name in ("start", "end"):
            slot = obj.get(slot_name)
            if not isinstance(slot, dict):
                continue
            prompt = str(slot.get("prompt") or "").strip()
            if not prompt:
                continue
            if prompt.startswith(pp):
                continue
            slot["prompt"] = f"{pp} {prompt}"
            changed = True

        out_lines.append(json.dumps(obj, ensure_ascii=False))

    return "\n".join(out_lines) if changed else txt


def _sanitize_editor_result_json(raw_result: str) -> str:
    """Normalize editor JSON result so edited_text has no \\n artifacts."""
    raw = str(raw_result or "")
    if not raw.strip():
        return raw
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(obj, dict):
        return raw
    et = obj.get("edited_text")
    if not isinstance(et, str):
        return raw

    txt = et
    # Handle literal escaped sequences first (possibly double-escaped).
    txt = re.sub(r"\\+r\\+n", " ", txt)
    txt = re.sub(r"\\+n", " ", txt)
    txt = re.sub(r"\\+r", " ", txt)
    # Then normalize real line breaks.
    txt = txt.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    txt = re.sub(r"[ \t]{2,}", " ", txt).strip()
    obj["edited_text"] = txt
    return json.dumps(obj, ensure_ascii=False)


def _iter_scene_json_objects(raw_text: str) -> list[tuple[int, Any, str | None]]:
    """Парсит raw_text как поток JSON-объектов (могут занимать несколько строк).

    Возвращает список троек (1-based line number начала объекта, объект, error_message).
    Если объект распарсился — error_message is None. Если нет — obj is None.
    Поддерживает «расслабленный» формат `{"keywords":"k1","k2","k3"}` (значения через запятую)
    как однострочный fallback.
    """
    out: list[tuple[int, Any, str | None]] = []
    text = raw_text or ""
    n = len(text)
    decoder = json.JSONDecoder()
    i = 0
    line_no = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line_no += 1
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        # Только JSON-объекты
        if ch != "{":
            # Считываем «строку-мусор» до конца строки и запишем ошибку
            j = text.find("\n", i)
            if j == -1:
                j = n
            chunk = text[i:j].strip()
            if chunk:
                out.append((line_no, None, f"Ошибка в строке {line_no}: ожидается JSON-объект"))
            i = j
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError as e:
            # Однострочный fallback для {"keywords":"a","b","c"}
            j = text.find("\n", i)
            if j == -1:
                j = n
            line_chunk = text[i:j]
            m_kw = re.match(r'^\s*\{\s*"keywords"\s*:\s*(.+)\}\s*$', line_chunk.strip())
            if m_kw:
                tail = m_kw.group(1).strip()
                vals = re.findall(r'"([^"]*)"', tail)
                if vals:
                    out.append((line_no, {"keywords": vals}, None))
                    line_no += line_chunk.count("\n")
                    i = j
                    continue
            out.append((line_no, None, f"Ошибка в строке {line_no}: не удалось распарсить JSON — {e}"))
            i = j
            continue
        # Учитываем переносы строк, пройденные внутри объекта
        line_no += text.count("\n", i, end)
        out.append((line_no - text.count("\n", i, end), obj, None))
        i = end
    return out


def _unwrap_json_array_of_objects(raw_text: str) -> str:
    """Если вставили один JSON-массив объектов — превращаем в построчные объекты для parse_scene_blocks."""
    t = (raw_text or "").strip()
    if not t.startswith("["):
        return raw_text or ""
    try:
        arr = json.loads(t)
    except json.JSONDecodeError:
        return raw_text or ""
    if not isinstance(arr, list):
        return raw_text or ""
    lines: list[str] = []
    for item in arr:
        if isinstance(item, dict):
            lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines) if lines else (raw_text or "")


def parse_scene_blocks(raw_text: str) -> tuple[list[dict], list[str]]:
    """
    Parse raw text into scene blocks.
    Logic: new scene_id starts a new scene; subsequent blocks belong to current scene.
    Поддерживаются как однострочные, так и многострочные JSON-объекты.
    Returns: (list of scene dicts, list of error messages)
    """
    raw_text = _unwrap_json_array_of_objects(raw_text)
    scenes: list[dict] = []
    errors: list[str] = []
    current_scene: dict | None = None

    for line_no, obj, err in _iter_scene_json_objects(raw_text):
        if err is not None:
            errors.append(err)
            continue
        i = line_no  # для совместимости: i — номер строки начала объекта

        if not isinstance(obj, dict):
            errors.append(f"Ошибка в строке {i}: ожидается JSON-объект")
            continue

        # Новый scene_id — начинаем новую сцену
        if "scene_id" in obj:
            # Сохраняем предыдущую сцену, если была
            if current_scene is not None:
                err = validate_scene(current_scene)
                if err:
                    errors.append(err)
                else:
                    scenes.append(normalize_scene(current_scene))

            current_scene = {
                "scene_id": obj.get("scene_id"),
                "text": None,
                "text_ru": None,
                "start": None,
                "end": None,
                "video": None,
            }
            continue

        if current_scene is None:
            errors.append(f"Ошибка в строке {i}: блок без предшествующего scene_id")
            continue

        # Блок text
        if "text" in obj:
            current_scene["text"] = obj.get("text")
            continue

        # Блок text_ru — русский перевод; необязательный, под текстом сцены.
        if "text_ru" in obj:
            current_scene["text_ru"] = obj.get("text_ru")
            continue

        # Блок start
        if "start" in obj:
            start_val = obj["start"]
            if start_val is not None and not isinstance(start_val, dict):
                errors.append(f"У сцены {current_scene.get('scene_id')} блок start должен быть объектом")
            elif start_val is not None and "prompt" not in start_val:
                errors.append(f"У сцены {current_scene.get('scene_id')} в блоке start нет поля prompt")
            else:
                current_scene["start"] = start_val
            continue

        # Блок end
        if "end" in obj:
            end_val = obj["end"]
            if end_val is not None and not isinstance(end_val, dict):
                errors.append(f"У сцены {current_scene.get('scene_id')} блок end должен быть объектом")
            elif end_val is not None and "prompt" not in end_val:
                errors.append(f"У сцены {current_scene.get('scene_id')} в блоке end нет поля prompt")
            else:
                current_scene["end"] = end_val
            continue

        # Блок video
        if "video" in obj:
            video_val = obj["video"]
            if video_val is not None and not isinstance(video_val, dict):
                errors.append(f"У сцены {current_scene.get('scene_id')} блок video должен быть объектом")
            elif video_val is not None and "prompt" not in video_val:
                errors.append(f"У сцены {current_scene.get('scene_id')} в блоке video нет поля prompt")
            else:
                current_scene["video"] = video_val
            continue

        # Новый формат Scene Writer Live: content_type + keywords.
        if "content_type" in obj:
            ct = str(obj.get("content_type") or "").strip().lower()
            if ct in ("photo", "photos"):
                current_scene["content_type"] = "photos"
            elif ct in ("video", "videos"):
                current_scene["content_type"] = "videos"
            else:
                errors.append(
                    f"У сцены {current_scene.get('scene_id')} поле content_type должно быть photos/videos"
                )
            continue

        if "keywords" in obj:
            kv = obj.get("keywords")
            if isinstance(kv, list):
                current_scene["keywords"] = ", ".join(str(x).strip() for x in kv if str(x).strip())
            else:
                current_scene["keywords"] = str(kv or "").strip()
            continue

        if "animation" in obj:
            errors.append(
                f"У сцены {current_scene.get('scene_id')} блок «animation» больше не поддерживается — удалите его из разметки сцен."
            )
            continue
        pm_keys = [k for k in obj if isinstance(k, str) and k.startswith("prompt_master")]
        if pm_keys:
            errors.append(
                f"У сцены {current_scene.get('scene_id')} поля {', '.join(pm_keys)} больше не поддерживаются — удалите их из разметки сцен."
            )
            continue

    # Последняя сцена
    if current_scene is not None:
        err = validate_scene(current_scene)
        if err:
            errors.append(err)
        else:
            scenes.append(normalize_scene(current_scene))

    return scenes, errors


def validate_scene(scene: dict) -> str | None:
    """Returns error message or None if valid."""
    if not scene.get("scene_id"):
        return "Найдена сцена без scene_id"
    return None


def normalize_scene(scene_parts: dict) -> dict:
    """Приводит сцену к нормализованному виду."""
    return {
        "scene_id": scene_parts.get("scene_id", ""),
        "text": scene_parts.get("text") or "",
        "text_ru": scene_parts.get("text_ru") or "",
        "content_type": scene_parts.get("content_type") or "",
        "keywords": scene_parts.get("keywords") or "",
        "excluded_keywords": scene_parts.get("excluded_keywords") if isinstance(scene_parts.get("excluded_keywords"), list) else [],
        "pexels_results": scene_parts.get("pexels_results") if isinstance(scene_parts.get("pexels_results"), list) else [],
        "pexels_selected_indices": scene_parts.get("pexels_selected_indices") if isinstance(scene_parts.get("pexels_selected_indices"), list) else [],
        "start": scene_parts.get("start") or {"prompt": None},
        "end": scene_parts.get("end") or {"prompt": None},
        "video": scene_parts.get("video") or {"prompt": None},
    }


def build_job_payload(
    raw_input: str,
    parsed_scenes: list[dict],
    aspect_ratio: str,
    video_duration: int,
    image_model: str,
    video_model: str,
    resolution: str = "2K",
    project_name: str = "",
    image_template: str = "",
) -> dict:
    """Собирает payload для сохранения job."""
    it = (image_template or "").strip()
    return {
        "project_name": project_name.strip(),
        "raw_input": raw_input,
        "parsed_scenes": parsed_scenes,
        "selected_aspect_ratio": aspect_ratio,
        "selected_video_duration": video_duration,
        "selected_image_model": image_model,
        "selected_video_model": video_model,
        "selected_resolution": resolution,
        "selected_image_template": it,
        "created_at": datetime.now().isoformat(),
        "status": "draft",
        "job_meta": {
            "aspect_ratio": aspect_ratio,
            "video_duration": video_duration,
            "image_model": image_model,
            "video_model": video_model,
            "resolution": resolution,
            "output_format": "jpg",
            "image_template": it,
        },
        "scenes": parsed_scenes,
    }


def new_video_job_payload(project_name: str) -> dict:
    """Создает пустой video-проект (настройки и JSON редактируются на странице проекта)."""
    aspect_ratio = "16:9"
    resolution = "2K"
    image_model = "nano-banana-pro"
    video_model = "veo3_fast"
    image_template = ""
    return {
        "project_name": (project_name or "").strip(),
        "raw_input": "",
        "parsed_scenes": [],
        "selected_aspect_ratio": aspect_ratio,
        "selected_video_duration": 10,
        "selected_image_model": image_model,
        "selected_video_model": video_model,
        "selected_resolution": resolution,
        "selected_image_template": image_template,
        "created_at": datetime.now().isoformat(),
        "status": "draft",
        "job_meta": {
            "aspect_ratio": aspect_ratio,
            "video_duration": 10,
            "image_model": image_model,
            "video_model": video_model,
            "resolution": resolution,
            "output_format": "jpg",
            "image_template": image_template,
        },
        "scenes": [],
    }


def save_job_file(payload: dict) -> tuple[str, str]:
    """Сохраняет job в JSON-файл. Возвращает (путь к файлу, job_id)."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    filename = f"{job_id}.json"
    filepath = JOBS_DIR / filename
    if isinstance(payload, dict):
        _sanitize_job_scenes(payload)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return str(filepath), job_id


def load_job(job_id: str) -> dict | None:
    """Загружает job по job_id. Возвращает None если не найден."""
    filepath = JOBS_DIR / f"{job_id}.json"
    if not filepath.exists():
        return None
    for _ in range(3):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                job = json.load(f)
                if isinstance(job, dict):
                    _sanitize_job_scenes(job)
                return job
        except json.JSONDecodeError:
            time.sleep(0.02)
            continue
        except OSError:
            return None
    return None


def save_job(job_id: str, job: dict) -> None:
    """Persist job JSON to disk."""
    if isinstance(job, dict):
        _sanitize_job_scenes(job)
    filepath = JOBS_DIR / f"{job_id}.json"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(filepath.parent),
            prefix=f"{filepath.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_name = f.name
            json.dump(job, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, filepath)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def update_job_field(job_id: str, field: str, value) -> bool:
    """Обновляет поле в job-файле. Возвращает True при успехе."""
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return False
        job[field] = value
        filepath = JOBS_DIR / f"{job_id}.json"
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(filepath.parent),
                prefix=f"{filepath.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                tmp_name = f.name
                json.dump(job, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, filepath)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return True


def list_jobs() -> list[dict]:
    """Возвращает список всех проектов: job_id, status, scenes_count, created_at."""
    jobs = []
    if not JOBS_DIR.exists():
        return jobs
    for f in sorted(JOBS_DIR.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        job_id = f.stem
        try:
            data = json.load(open(f, "r", encoding="utf-8"))
            scenes = data.get("scenes", data.get("parsed_scenes", []))
            jobs.append({
                "job_id": job_id,
                "project_name": data.get("project_name", ""),
                "status": data.get("status", "draft"),
                "scenes_count": len(scenes),
                "created_at": data.get("created_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return jobs


def rewrite_id_ok(rid: str) -> bool:
    return bool(_REWRITE_ID_RE.match(rid or ""))


def _rewrite_project_dir(rewrite_id: str) -> Path:
    return REWRITE_JOBS_DIR / rewrite_id


def _rewrite_project_json_path(rewrite_id: str) -> Path:
    return _rewrite_project_dir(rewrite_id) / "project.json"


def _rewrite_legacy_filepath(rewrite_id: str) -> Path:
    return REWRITE_JOBS_DIR / f"{rewrite_id}.json"


# System prompt for «Перевести на русский» (исходный текст, батчи ~5000 симв.).
REWRITE_SOURCE_RU_TRANSLATE_SYSTEM_PROMPT = """Ты — профессиональный переводчик и редактор русского языка.

Твоя задача — перевести входной текст на русский язык максимально естественно, понятно и живо.

КРИТИЧЕСКИЕ ПРАВИЛА:

— Сохраняй исходный смысл на 100%
— Не сокращай текст
— Не добавляй новую информацию
— Не меняй факты, цифры, даты и имена
— Не упрощай смысл
— Не делай пересказ
— Не цензурируй эмоциональность автора

СТИЛЬ ПЕРЕВОДА:

— Русский текст должен звучать естественно для носителя языка
— Избегай дословного "машинного" перевода
— Сохраняй ритм и эмоциональную подачу оригинала
— Если в тексте есть сарказм, напряжение, ирония или агрессия — сохраняй это
— Если текст разговорный — перевод тоже должен быть разговорным
— Если текст экспертный — сохраняй экспертную подачу

ДОПОЛНИТЕЛЬНЫЕ ПРАВИЛА:

— Числа и факты сохраняй точно
— Денежные суммы не искажай
— Термины переводи корректно по контексту
— Английские названия брендов, компаний и сервисов не переводи без необходимости
— Сохраняй структуру абзацев

ФОРМАТ ОТВЕТА:

Верни ТОЛЬКО готовый перевод на русском языке.
Без комментариев.
Без пояснений.
Без оригинального текста."""


def _split_text_into_translation_batches(text: str, max_chars: int = 5000) -> list[str]:
    """Делит текст на части ≤ max_chars, стараясь резать по абзацам/строкам/пробелам."""
    if not (text or "").strip():
        return []
    t = str(text)
    if len(t) <= max_chars:
        return [t]
    batches: list[str] = []
    start = 0
    n = len(t)
    lookback = min(1600, max_chars // 2)
    while start < n:
        if n - start <= max_chars:
            batches.append(t[start:])
            break
        chunk = t[start : start + max_chars]
        split_off = len(chunk)
        tail = chunk[max(0, len(chunk) - lookback) :]
        for sep in ("\n\n", "\n"):
            p = tail.rfind(sep)
            if p != -1:
                split_off = max(0, len(chunk) - len(tail)) + p + len(sep)
                break
        else:
            p2 = tail.rfind(". ")
            if p2 != -1:
                split_off = max(0, len(chunk) - len(tail)) + p2 + 2
            else:
                p3 = chunk.rfind(" ")
                if p3 != -1 and p3 > max_chars // 3:
                    split_off = p3 + 1
        part = t[start : start + split_off].rstrip("\n")
        if part:
            batches.append(part)
        start = start + split_off
        while start < n and t[start] in "\n\r \t":
            start += 1
    return batches


def _rewrite_stage_result_path(rewrite_id: str, stage_key: str) -> Path:
    return _rewrite_project_dir(rewrite_id) / f"{stage_key}.result.txt"


def _rewrite_block_writer_dir(rewrite_id: str) -> Path:
    return _rewrite_project_dir(rewrite_id) / "block_writer"


_MAX_OPENAI_EXPORT_JSON_DEPTH = 32


def _json_loads_fully(s: str) -> Any | None:
    """
    json.loads(s) — только если вся s (c учётом пробелов по краям) — один JSON-значок.
    Не используем parse «с первой {», иначе теряется префикс (например, пояснение + JSON).
    """
    t = (s or "").lstrip("\ufeff")
    if not t or not t.strip():
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _wrap_plaintext_for_export(s: str) -> Any:
    """
    Текст без переносов — одна JSON-строка (как в wire), даже если очень длинная.
    С переносами строк — для читаемости в файле: {\"_export\":\"text_lines\",\"lines\":[...]}.
    """
    s = s or ""
    if "\n" not in s and "\r" not in s and "\u2028" not in s and "\u2029" not in s:
        return s
    lines = s.splitlines()
    return {"_export": "text_lines", "lines": lines if lines else [""]}


def _expand_value_for_openai_export(val: Any, depth: int = 0) -> Any:
    """Рекурсивно: JSON-строки внутри dict/list → объекты; многострочный plain text → text_lines."""
    if depth > _MAX_OPENAI_EXPORT_JSON_DEPTH:
        return val
    if isinstance(val, str):
        s = val or ""
        t = s.strip()
        if not t:
            return _wrap_plaintext_for_export(s)
        p = _json_loads_fully(s)
        if p is not None:
            return _expand_value_for_openai_export(p, depth + 1)
        return _wrap_plaintext_for_export(s)
    if isinstance(val, dict):
        return {str(k): _expand_value_for_openai_export(v, depth + 1) for k, v in val.items()}
    if isinstance(val, list):
        return [_expand_value_for_openai_export(v, depth + 1) for v in val]
    return val


def _message_content_for_openai_export(c: str) -> Any:
    """Сообщение content: разобранный JSON (с вложенностями), иначе строка или text_lines при переносах."""
    if not isinstance(c, str):
        return c
    p = _json_loads_fully(c)
    if p is not None:
        return _expand_value_for_openai_export(p, 0)
    return _wrap_plaintext_for_export(c)


def _body_for_pretty_openai_export(body: dict[str, Any]) -> dict[str, Any]:
    """
    Копия тела POST для файла: в HTTP messages[].content — строки;
    здесь JSON разворачивается рекурсивно; многострочный plain text — в _export.text_lines.
    """
    out = copy.deepcopy(body)
    msgs = out.get("messages")
    if not isinstance(msgs, list):
        return out
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = _message_content_for_openai_export(c)
    return out


def _format_openai_wire_payloads_txt(
    bodies: list[dict[str, Any]],
    *,
    header_lines: list[str] | None = None,
) -> str:
    """Один валидный JSON (UTF-8): about + requests[]; content развёрнут рекурсивно."""
    about = (
        "Логика входов как у кнопки ↻: тот же JSON со страницы (collectSnapshot), на сервере те же "
        "snapshot_stages_from_body / compose_rewrite_openai_request_body, что и в POST /rewrite/<id>/run. "
        "Дальше: для одного POST на этап — то же, что перед HTTP, что и в iter_rewrite_completion: "
        "rewrite_chat_completion_wire_payload (нормализация model, _sanitize на system/user, temperature). "
        "draft1 и scene_writer шлют несколько POST подряд — в requests[] по одному объекту на каждый такой вызов "
        "(для draft1 при отсутствии block_*.json контекст short_summary может отличаться от живого прогона — см. notes). "
        "Файл — читаемый JSON (UTF-8, отступы); реальное тело POST кодируется компактнее (другой вид сериализации JSON). "
        "Здесь messages[].content может быть развёрнут в объекты и в пометки "
        "{\"_export\":\"text_lines\",\"lines\":[...]} — это только в этом файле для просмотра; в HTTP к OpenAI такого нет, там всегда строки в content."
    )
    pretty = [_body_for_pretty_openai_export(b) for b in bodies]
    env: dict[str, Any] = {
        "about": about,
        "requests": pretty,
    }
    if header_lines:
        notes = [str(ln) for ln in header_lines if str(ln).strip()]
        if notes:
            env["notes"] = notes
    return json.dumps(env, ensure_ascii=False, indent=2) + "\n"


def _load_block_writer_saved_short_summaries(rewrite_id: str) -> list[list[str]] | None:
    """short_summary по блокам из block_NNN.json (порядок по block_index)."""
    bw_dir = _rewrite_block_writer_dir(rewrite_id)
    if not bw_dir.is_dir():
        return None
    pairs: list[tuple[int, list[str]]] = []
    for p in bw_dir.glob("block_*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        try:
            idx = int(raw.get("block_index") or 0)
        except (TypeError, ValueError):
            continue
        if idx < 1:
            continue
        ss = raw.get("short_summary")
        items: list[str] = []
        if isinstance(ss, list):
            items = [str(x or "").strip() for x in ss if str(x or "").strip()]
        pairs.append((idx, items))
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])
    return [items for _, items in pairs]


def _load_block_writer_completed_blocks(rewrite_id: str) -> list[dict[str, Any]] | None:
    """Готовые блоки Block Writer из all_blocks.json (fallback: block_*.json)."""
    bw_dir = _rewrite_block_writer_dir(rewrite_id)
    if not bw_dir.is_dir():
        return None
    all_fp = bw_dir / "all_blocks.json"
    if all_fp.is_file():
        try:
            raw = json.loads(all_fp.read_text(encoding="utf-8"))
            blocks = raw.get("blocks") if isinstance(raw, dict) else None
            if isinstance(blocks, list):
                out = [b for b in blocks if isinstance(b, dict)]
                if out:
                    return out
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    rows: list[dict[str, Any]] = []
    for p in bw_dir.glob("block_*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    if not rows:
        return None
    rows.sort(key=lambda x: int(x.get("block_index") or 0))
    return rows


def _rewrite_media_dir(rewrite_id: str) -> Path:
    return REWRITE_MEDIA_DIR / rewrite_id


def _youtube_url_normalize(url: str) -> str:
    return (url or "").strip()


def _youtube_url_is_valid(url: str) -> bool:
    u = _youtube_url_normalize(url)
    # Shorts — тот же id и аудио, что у обычного ролика; yt-dlp понимает URL как есть.
    return bool(
        re.match(
            r"^(https?://)?((www\.|m\.)?youtube\.com/(watch\?v=[A-Za-z0-9_-]{6,}|shorts/[A-Za-z0-9_-]{6,})|youtu\.be/[A-Za-z0-9_-]{6,}).*$",
            u,
            re.IGNORECASE,
        )
    )


def _youtube_url_rejection_message(url: str) -> str:
    """Пояснение, если URL не похож на один ролик (не watch / shorts id / youtu.be)."""
    base = (
        "Некорректная ссылка YouTube. Нужна ссылка на один ролик: …/watch?v=…, "
        "…/shorts/VIDEO_ID или youtu.be/…"
    )
    u = _youtube_url_normalize(url)
    if not u:
        return "Вставьте ссылку на ролик YouTube."
    try:
        parsed = urlparse(u)
    except Exception:
        return base
    netloc = (parsed.netloc or "").lower()
    if "youtube.com" not in netloc and "youtu.be" not in netloc:
        return base
    segs = [s for s in (parsed.path or "").split("/") if s]
    if not segs:
        return base
    last = segs[-1].lower()
    if last == "shorts":
        if len(segs) == 1:
            return (
                "В адресе нет id ролика после /shorts/. Откройте конкретное видео и скопируйте ссылку "
                "вида …/shorts/XXXXXXXX."
            )
        prev = segs[-2]
        if prev.startswith("@"):
            return (
                "Это страница вкладки «Shorts» канала, а не ссылка на ролик. Откройте нужный short "
                "и скопируйте адрес: …/shorts/VIDEO_ID или …watch?v=VIDEO_ID."
            )
        if len(segs) >= 3 and segs[0].lower() == "channel" and segs[2].lower() == "shorts":
            return (
                "Это вкладка Shorts канала, а не ссылка на ролик. Откройте конкретное видео и скопируйте "
                "адрес …/shorts/VIDEO_ID или …watch?v=VIDEO_ID."
            )
        if len(segs) >= 3 and segs[0].lower() in ("c", "user") and segs[2].lower() == "shorts":
            return (
                "Это вкладка канала, а не ссылка на ролик. Нужна ссылка на один short или обычное видео "
                "(…/shorts/VIDEO_ID, watch?v=…, youtu.be/…)."
            )
        if segs[0].lower() == "shorts" and len(segs) == 2:
            vid = segs[1]
            if len(vid) < 6 or not re.match(r"^[A-Za-z0-9_-]+$", vid):
                return (
                    "После /shorts/ должен быть id ролика. Скопируйте полную ссылку на короткое видео "
                    "со страницы ролика на YouTube."
                )
    return base


def _youtube_info_cache_path(rewrite_id: str) -> Path:
    return _rewrite_project_dir(rewrite_id) / "youtube_info_cache.json"


# yt-dlp по умолчанию socket_timeout=20 с; загрузка с googlevideo.com часто падает Read timed out.
# YouTube нередко throttler'ит трафик (второй ролик подряд, один IP) — throttledratelimit заставляет
# пересобрать ссылки, если скорость упала ниже порога (см. --throttled-rate в yt-dlp).
# player_client задаётся **отдельно** на попытку — см. _youtube_player_client_chain и fallback при ошибке.
_YOUTUBE_YDL_BASE: dict = {
    "noplaylist": True,
    "quiet": True,
    "socket_timeout": 180,
    "retries": 20,
    "fragment_retries": 20,
    # DASH/фрагменты: параллель (меньше = мягче к тому же IP при нескольких роликах подряд)
    "concurrent_fragment_downloads": 2,
    # ~100 КиБ/с: при типичном троттлинге YouTube — повтор с новым format URL
    "throttledratelimit": 100_000,
}


def _youtube_cookies_file_path() -> Path:
    """Путь к cookies.txt (Netscape). Переопределение: YT_COOKIES_PATH в .env."""
    raw = (os.getenv("YT_COOKIES_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (BASE_DIR / "data" / "secrets" / "yt_cookies.txt").resolve()


def _youtube_cookiefile_opts() -> dict[str, Any]:
    """Опции yt-dlp: cookiefile, если файл непустой."""
    p = _youtube_cookies_file_path()
    try:
        if p.is_file() and p.stat().st_size > 0:
            return {"cookiefile": str(p)}
    except OSError:
        pass
    return {}


def _youtube_cookies_age_human(seconds: float) -> str:
    if seconds < 60:
        return f"{int(max(1, seconds))} с"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h} ч" + (f" {m} мин" if m else "")
    d = int(seconds // 86400)
    return f"{d} дн"


def youtube_cookies_status_dict() -> dict[str, Any]:
    """Сводка для UI / GET API (без содержимого файла)."""
    p = _youtube_cookies_file_path()
    try:
        rel_hint = str(p.resolve().relative_to(BASE_DIR.resolve()))
    except ValueError:
        rel_hint = p.name
    out: dict[str, Any] = {
        "present": False,
        "mtime_iso": None,
        "age_seconds": None,
        "age_human": None,
        "size_bytes": 0,
        "path_hint": rel_hint,
    }
    try:
        if p.is_file() and p.stat().st_size > 0:
            st = p.stat()
            out["present"] = True
            out["size_bytes"] = int(st.st_size)
            mt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            out["mtime_iso"] = mt.isoformat()
            age = max(0.0, time.time() - st.st_mtime)
            out["age_seconds"] = int(age)
            out["age_human"] = _youtube_cookies_age_human(age)
    except OSError:
        pass
    return out


def _youtube_validate_cookies_upload(raw: bytes) -> str | None:
    if len(raw) > 2_000_000:
        return "Файл слишком большой (максимум 2 МБ)."
    if len(raw) < 32:
        return "Файл слишком короткий."
    head = raw[:8192].decode("utf-8", errors="replace").lower()
    if (
        "youtube.com" not in head
        and "youtu.be" not in head
        and ".google.com" not in head
        and "googlevideo.com" not in head
    ):
        return "В файле не найдены домены YouTube/Google — убедитесь, что это экспорт cookies с youtube.com (формат Netscape)."
    if "# netscape" not in head and "\t" not in head[:200]:
        return "Ожидается cookies.txt в формате Netscape (табуляция, строка «# Netscape…»)."
    return None


def _ytdl_youtube_extractor_player_client(name: str) -> dict:
    c = (name or "").strip().lower()
    return {
        "extractor_args": {
            "youtube": {
                "player_client": [c] if c else ["android"],
            }
        }
    }


def _youtube_stall_read_sec() -> int:
    """Сколько секунд ждать **без данных** по сокету (CDN / youtube) на одной попытке, затем «провал» → следующий player_client."""
    raw = (os.getenv("YOUTUBE_STALL_READ_SEC") or "20").strip()
    try:
        s = int(raw, 10)
    except ValueError:
        s = 20
    return max(5, min(120, s))


def _youtube_verify_socket_sec() -> int:
    """Проверка ссылки: мягче, иначе медленные DNS/HTML отрывают verify до смены клиента."""
    raw = (os.getenv("YOUTUBE_VERIFY_SOCKET_SEC") or "90").strip()
    try:
        s = int(raw, 10)
    except ValueError:
        s = 90
    return max(15, min(300, s))


def _youtube_same_client_retries() -> int:
    """
    Сколько **внутри** одного player_client ретраев скачивчика, прежде чем yt-dlp выкинет ошибку
    (тогда срабатывает смена клиента). 0 = одна неудачная сессия чтения с CDN → сразу следующий client.
    """
    raw = (os.getenv("YOUTUBE_SAME_CLIENT_RETRIES") or "0").strip()
    try:
        n = int(raw, 10)
    except ValueError:
        n = 0
    return max(0, min(12, n))


def _youtube_player_client_chain() -> list[str]:
    """
    Цепочка клиентов: сначала YOUTUBE_PLAYER_CLIENT (или android), потом YOUTUBE_PLAYER_CLIENT_FALLBACK.
    Дубликаты убираем. Следующий клиент — по ошибке extract_info/скачивания или таймауту сокета
    (YOUTUBE_STALL_READ_SEC на скачивании, см. _youtube_stall_read_sec).
    """
    head = (os.getenv("YOUTUBE_PLAYER_CLIENT") or "android").strip()
    head_list = [c.strip().lower() for c in head.split(",") if c.strip()]
    # Порядок: часто web/ios/mweb дают другой endpoint; tv — лишний раунт плеера (часто дольше), убрали по умолчанию.
    # Подогнать под ваш IP: YBENCH_URL=... .venv/bin/python3 scripts/benchmark_youtube_clients.py
    tail = (os.getenv("YOUTUBE_PLAYER_CLIENT_FALLBACK") or "web,ios,mweb").strip()
    tail_list = [c.strip().lower() for c in tail.split(",") if c.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for c in head_list + tail_list:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out or ["android", "web"]


def _youtube_format_chain() -> list[str]:
    """
    Форматы по убыванию предпочтения.
    На части роликов/клиентов конкретный selector может быть недоступен
    (Requested format is not available), поэтому нужен fallback по форматам.
    """
    return [
        "bestaudio[ext=m4a]/bestaudio/best",
        "bestaudio/best",
        "best",
    ]


def _youtube_error_is_format_unavailable(err: BaseException) -> bool:
    msg = str(err or "").lower()
    return "requested format is not available" in msg


def _youtube_pick_audio_format_id(formats: list[dict]) -> str | None:
    """
    Выбирает format_id среди реально доступных:
    1) аудио-only m4a, 2) аудио-only любой, 3) любой с acodec != none.
    """
    if not isinstance(formats, list):
        return None

    def _is_audio_only(f: dict) -> bool:
        return str(f.get("acodec") or "none") != "none" and str(f.get("vcodec") or "none") == "none"

    def _has_audio(f: dict) -> bool:
        return str(f.get("acodec") or "none") != "none"

    preferred: list[dict] = []
    fallback_audio_only: list[dict] = []
    fallback_any_audio: list[dict] = []
    for f in formats:
        if not isinstance(f, dict):
            continue
        if _is_audio_only(f):
            if str(f.get("ext") or "").lower() == "m4a":
                preferred.append(f)
            else:
                fallback_audio_only.append(f)
        elif _has_audio(f):
            fallback_any_audio.append(f)

    for bucket in (preferred, fallback_audio_only, fallback_any_audio):
        if not bucket:
            continue
        # выше abr/tbr — выше приоритет
        sorted_bucket = sorted(
            bucket,
            key=lambda x: float(x.get("abr") or x.get("tbr") or 0.0),
            reverse=True,
        )
        for f in sorted_bucket:
            fid = str(f.get("format_id") or "").strip()
            if fid:
                return fid
    return None


def _youtube_probe_audio_format_id(url: str, cname: str, socket_timeout: int) -> str | None:
    opts: dict[str, Any] = {
        **_YOUTUBE_YDL_BASE,
        **_youtube_cookiefile_opts(),
        "socket_timeout": socket_timeout,
        "retries": 0,
        "fragment_retries": 0,
        "skip_download": True,
        **_ytdl_youtube_extractor_player_client(cname),
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        return None
    return _youtube_pick_audio_format_id(info.get("formats") or [])


def _rewrite_youtube_clear_partial_downloads(media_dir: Path) -> None:
    """Следы неудачной попытки yt-dlp, чтобы не мешать следующему player_client."""
    for pat in ("youtube_audio_*", "*.part", "*.ytdl", "*.temp"):
        for p in media_dir.glob(pat):
            if p.is_file():
                p.unlink(missing_ok=True)


def _rewrite_youtube_set_runtime_state(rewrite_id: str, *, processing: bool, phase: str = "", status: str = "") -> None:
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return
    rw["youtube_processing"] = bool(processing)
    rw["youtube_phase"] = str(phase or "")
    rw["youtube_status"] = str(status or "")
    save_rewrite_job(rewrite_id, rw)


def _rewrite_youtube_resolve_audio_path(rewrite_id: str, rw: dict) -> Path | None:
    """Нормализует путь к аудио: относительный/абсолютный/legacy, с fallback на newest youtube_audio_* в media."""
    base = BASE_DIR.resolve()
    rel = str(rw.get("youtube_audio_file") or "").strip()
    candidates: list[Path] = []

    if rel:
        rp = Path(rel)
        if rp.is_absolute():
            candidates.append(rp.resolve())
            # legacy migration: старый корень проекта /srv/vision_video -> текущий BASE_DIR
            if rel.startswith('/srv/vision_video/'):
                tail = rel[len('/srv/vision_video/'):]
                candidates.append((base / tail).resolve())
        else:
            candidates.append((base / rel).resolve())

    media_dir = _rewrite_media_dir(rewrite_id)
    if media_dir.is_dir():
        newest_mp3 = sorted(media_dir.glob('youtube_audio_*.mp3'), key=lambda x: x.stat().st_mtime, reverse=True)
        newest_any = sorted(media_dir.glob('youtube_audio_*'), key=lambda x: x.stat().st_mtime, reverse=True)
        candidates.extend(newest_mp3)
        candidates.extend(newest_any)

    seen: set[str] = set()
    for ap in candidates:
        k = str(ap)
        if k in seen:
            continue
        seen.add(k)
        try:
            ap.relative_to(base)
        except ValueError:
            continue
        if ap.is_file():
            return ap
    return None


def _rewrite_youtube_audio_exists(rw: dict) -> bool:
    rewrite_id = str(rw.get("rewrite_id") or "")
    if not rewrite_id_ok(rewrite_id):
        return False
    return _rewrite_youtube_resolve_audio_path(rewrite_id, rw) is not None


def _rewrite_youtube_progress_hooks_with_stall(stall_sec: int, user_hooks: list | None) -> list:
    """Сначала вызывает пользовательские progress_hooks; при отсутствии роста скачанных байт N с — RuntimeError (смена client)."""
    inners: list = list(user_hooks or [])
    state = {"last_b": None, "at": 0.0}

    def _stall_hook(d: dict) -> None:
        for h in inners:
            h(d)
        if d.get("status") != "downloading":
            return
        b = d.get("downloaded_bytes")
        if b is None:
            return
        try:
            b = int(b)
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        if state["last_b"] is None or b != state["last_b"]:
            state["last_b"] = b
            state["at"] = now
        elif now - state["at"] > float(stall_sec):
            raise RuntimeError(f"yt-dlp: {stall_sec} c без роста скачанных байт — смена YouTube client")

    return [_stall_hook]


def _safe_rewrite_basename(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9._-]+", "_", (name or "").strip())
    base = base.strip("._-")
    return base[:80] or "audio"


def _probe_audio_duration_seconds(audio_path: Path) -> float | None:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    raw = (p.stdout or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _split_audio_for_transcription(
    audio_path: Path,
    segment_seconds: int = 180,
    *,
    progress: Callable[[dict], None] | None = None,
) -> list[Path]:
    """Нарезает длинное/тяжелое аудио на части; сначала быстрый copy-segment, fallback — re-encode в mp3."""

    def _emit(ev: dict) -> None:
        if progress:
            progress(ev)

    try:
        file_bytes = int(audio_path.stat().st_size) if audio_path.is_file() else 0
    except OSError:
        file_bytes = 0

    max_chunk_bytes = 24 * 1024 * 1024
    duration = _probe_audio_duration_seconds(audio_path)
    need_split_by_duration = duration is not None and duration > float(segment_seconds)
    need_split_by_size = file_bytes > max_chunk_bytes

    _emit(
        {
            "phase": "split",
            "action": "probe",
            "file_bytes": file_bytes,
            "duration_seconds": duration,
            "segment_seconds": segment_seconds,
            "max_chunk_bytes": max_chunk_bytes,
            "message": "Анализ аудио перед нарезкой…",
        }
    )

    if not need_split_by_duration and not need_split_by_size:
        _emit({"phase": "split", "action": "skip", "message": "Нарезка не требуется: аудио достаточно короткое/легкое."})
        return [audio_path]

    ext = (audio_path.suffix or '.m4a').lower()

    def _run_segment(cmd: list[str], pattern: str, progress_prefix: str) -> list[Path] | None:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = time.monotonic()
            last_emit = started
            while True:
                rc = proc.poll()
                if rc is not None:
                    if rc != 0:
                        return None
                    break
                now = time.monotonic()
                if now - last_emit >= 4.0:
                    last_emit = now
                    _emit(
                        {
                            "phase": "split",
                            "action": "progress",
                            "elapsed_seconds": int(now - started),
                            "message": f"{progress_prefix}… {int(now - started)} с",
                        }
                    )
                time.sleep(0.25)
        except FileNotFoundError:
            return None
        return sorted(Path(pattern).parent.glob(Path(pattern).name.replace('%03d', '*')))

    with tempfile.TemporaryDirectory(prefix="rw_transcribe_") as td:
        tmp = Path(td)

        # Fast path: segment copy без перекодирования (обычно в разы быстрее)
        fast_pattern = str(tmp / f"chunk_%03d{ext}")
        _emit(
            {
                "phase": "split",
                "action": "start",
                "segment_seconds": segment_seconds,
                "message": "Нарезка ffmpeg (быстрый режим без перекодирования)…",
            }
        )
        fast_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-c",
            "copy",
            fast_pattern,
        ]
        chunks = _run_segment(fast_cmd, fast_pattern, "Нарезка ffmpeg (copy) в процессе") or []

        # Fallback: re-encode в mp3 только если copy не сработал
        if not chunks:
            slow_pattern = str(tmp / "chunk_%03d.mp3")
            _emit(
                {
                    "phase": "split",
                    "action": "fallback",
                    "message": "Быстрый режим не удался, включаем re-encode в mp3…",
                }
            )
            slow_cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(audio_path),
                "-f",
                "segment",
                "-segment_time",
                str(segment_seconds),
                "-acodec",
                "libmp3lame",
                "-q:a",
                "4",
                slow_pattern,
            ]
            chunks = _run_segment(slow_cmd, slow_pattern, "Нарезка ffmpeg (mp3) в процессе") or []

        if not chunks:
            _emit({"phase": "split", "action": "error", "message": "Нарезка не удалась; продолжаем без нарезки."})
            return [audio_path]

        persisted: list[Path] = []
        persist_dir = audio_path.parent / "_transcribe_chunks"
        persist_dir.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(chunks, start=1):
            out_ext = c.suffix or ext
            pp = persist_dir / f"{audio_path.stem}_chunk_{i:03d}{out_ext}"
            shutil.copy2(c, pp)
            persisted.append(pp)

        _emit(
            {
                "phase": "split",
                "action": "done",
                "chunks": len(persisted),
                "message": f"Нарезка завершена: частей {len(persisted)}.",
            }
        )
        return persisted


def _transcribe_error_requires_reencode(err: str) -> bool:
    t = (err or '').lower()
    needles = [
        'audio file might be corrupted or unsupported',
        'unsupported',
        'could not decode',
        'invalid data found when processing input',
        'failed to read audio',
        'invalid audio',
    ]
    return any(n in t for n in needles)


def _transcode_audio_to_mp3_temp(audio_path: Path) -> Path | None:
    """Пытается перекодировать входное аудио в temp mp3 для fallback-транскрибации."""
    tmp_dir = audio_path.parent / '_transcribe_fallback'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = tmp_dir / f"{audio_path.stem}_fallback.mp3"
    try:
        subprocess.run(
            [
                'ffmpeg',
                '-hide_banner',
                '-loglevel',
                'error',
                '-y',
                '-i',
                str(audio_path),
                '-acodec',
                'libmp3lame',
                '-q:a',
                '3',
                str(out),
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out if out.is_file() else None


def _rewrite_transcription_text(
    api_key: str,
    audio_path: Path,
    *,
    progress: Callable[[dict], None] | None = None,
) -> tuple[str | None, str | None]:
    """Расшифровка; progress — опциональные события для UI (поток NDJSON)."""

    def _p(ev: dict) -> None:
        if progress:
            progress(ev)

    if not audio_path.is_file():
        return None, "Аудиофайл не найден на сервере."
    _p(
        {
            "phase": "split",
            "message": "Проверка длительности; длинный ролик нарезается на части (~3 мин каждая)…",
        }
    )
    chunks = _split_audio_for_transcription(audio_path, segment_seconds=180, progress=_p)
    n = len(chunks)
    _p(
        {
            "phase": "plan",
            "total_chunks": n,
            "segment_seconds": 180,
            "message": "Один запрос к API" if n == 1 else f"Будет {n} запросов к API (по одному на часть).",
        }
    )
    parts: list[str] = []
    for i, ap in enumerate(chunks, start=1):
        try:
            sz = int(ap.stat().st_size) if ap.is_file() else 0
        except OSError:
            sz = 0
        _p(
            {
                "phase": "chunk",
                "action": "request",
                "index": i,
                "total": n,
                "file_bytes": sz,
            }
        )
        try:
            with open(ap, "rb") as f:
                mime = (mimetypes.guess_type(ap.name)[0] or "application/octet-stream")
                files = {
                    "file": (ap.name, f, mime),
                }
                data = {
                    "model": "gpt-4o-mini-transcribe",
                    "response_format": "text",
                }
                r = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key.strip()}"},
                    files=files,
                    data=data,
                    timeout=900,
                )
        except requests.RequestException as e:
            return None, f"Сеть / таймаут (chunk {i}/{n}): {e}"
        if not r.ok:
            try:
                err = r.json().get("error", {}).get("message") or ""
            except Exception:
                err = ""
            msg = err or (r.text or "")[:500] or f"HTTP {r.status_code}"
            return None, f"{msg} (chunk {i}/{n})"
        txt = (r.text or "").strip()
        _p(
            {
                "phase": "chunk",
                "action": "done",
                "index": i,
                "total": n,
                "text_chars": len(txt),
            }
        )
        if txt:
            parts.append(txt)
    if not parts:
        return None, "Пустой ответ транскрибации."
    if n > 1:
        _p(
            {
                "phase": "join",
                "message": f"Склейка {n} сегментов в один текст…",
            }
        )
    # Убираем временные чанки после успешной склейки
    if len(chunks) > 1:
        for p in chunks:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (audio_path.parent / "_transcribe_chunks").rmdir()
        except OSError:
            pass
    return "\n\n".join(parts).strip(), None


def new_rewrite_payload(rewrite_id: str, project_name: str) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "rewrite_id": rewrite_id,
        "project_name": (project_name or "").strip() or rewrite_id,
        "created_at": now,
        "updated_at": now,
        "source_text": "",
        "source_title": "",
        "stages": new_stages_dict(),
        "model": REWRITE_DEFAULT_MODEL,
        "last_prompt": "",
        "last_text": "",
        "last_result": "",
        "source_locked": False,
        "source_text_ru_locked": False,
        "voiceover_final_text": "",
        "voiceover_final_locked": True,
        "voiceover_final_text_ru": "",
        "voiceover_final_text_ru_locked": False,
        "master_prompt": "",
        "master_prompt_locked": False,
        "target_chars": clamp_target_chars(5 * 344),
        "duration_minutes": 5,
        "hero_prompt": "",
        "chars_per_minute": 344,
        "rewrite_template": "",
        "hero_prompt_locked": False,
        "audio_timing_locked": False,
        "youtube_url": "",
        "youtube_verified": False,
        "youtube_title": "",
        "youtube_audio_file": "",
        "youtube_transcript_text": "",
        "youtube_transcript_url": "",
        "youtube_processing": False,
        "youtube_phase": "",
        "youtube_status": "",
    }


def create_rewrite_job(project_name: str) -> str:
    REWRITE_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    rewrite_id = f"rewrite_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    payload = new_rewrite_payload(rewrite_id, project_name)
    save_rewrite_job(rewrite_id, payload)
    return rewrite_id


def load_rewrite_job(rewrite_id: str) -> dict | None:
    if not rewrite_id_ok(rewrite_id):
        return None
    fp = _rewrite_project_json_path(rewrite_id)
    legacy_fp = _rewrite_legacy_filepath(rewrite_id)
    if not fp.is_file() and not legacy_fp.is_file():
        return None
    try:
        target_fp = fp if fp.is_file() else legacy_fp
        with open(target_fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        normalize_rewrite_job_data(data)
        # Миграция артефакта: voice_flow_editor_2.result.txt → title_strategist.result.txt
        pdir = _rewrite_project_dir(rewrite_id)
        _old_v2_res = pdir / "voice_flow_editor_2.result.txt"
        _new_ts_res = pdir / "title_strategist.result.txt"
        if _old_v2_res.is_file() and not _new_ts_res.is_file():
            try:
                _old_v2_res.rename(_new_ts_res)
            except OSError:
                pass
        # Source of truth for stage results: separate files per stage in project folder.
        for sk in REWRITE_STAGE_KEYS:
            rf = _rewrite_stage_result_path(rewrite_id, sk)
            if not rf.is_file():
                continue
            try:
                txt = rf.read_text(encoding="utf-8")
            except OSError:
                continue
            data.setdefault("stages", {})
            data["stages"].setdefault(sk, {})
            data["stages"][sk]["last_result"] = txt
        # Block Writer check: если в project.json ещё нет, но есть block_writer/all_blocks.json,
        # строим проверку на лету, чтобы UI не показывал "ожидание данных" для уже готового draft1.
        st = data.get("stages") if isinstance(data.get("stages"), dict) else {}
        d1 = st.get("draft1") if isinstance(st.get("draft1"), dict) else {}
        if isinstance(d1, dict) and not isinstance(d1.get("block_writer_check"), dict):
            completed_blocks = _load_block_writer_completed_blocks(rewrite_id)
            if completed_blocks:
                d1["block_writer_check"] = _build_block_writer_check(completed_blocks)
        data.setdefault("youtube_url", "")
        data["youtube_url"] = str(data.get("youtube_url") or "")
        data.setdefault("youtube_verified", False)
        data["youtube_verified"] = bool(data.get("youtube_verified"))
        data.setdefault("youtube_title", "")
        data["youtube_title"] = str(data.get("youtube_title") or "")
        data.setdefault("youtube_audio_file", "")
        data["youtube_audio_file"] = str(data.get("youtube_audio_file") or "")
        data.setdefault("youtube_transcript_text", "")
        data["youtube_transcript_text"] = str(data.get("youtube_transcript_text") or "")
        data.setdefault("youtube_transcript_url", "")
        data["youtube_transcript_url"] = str(data.get("youtube_transcript_url") or "")
        data.setdefault("youtube_processing", False)
        data["youtube_processing"] = bool(data.get("youtube_processing"))
        data.setdefault("youtube_phase", "")
        data["youtube_phase"] = str(data.get("youtube_phase") or "")
        data.setdefault("youtube_status", "")
        data["youtube_status"] = str(data.get("youtube_status") or "")
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_rewrite_job(rewrite_id: str, data: dict) -> None:
    if not rewrite_id_ok(rewrite_id):
        raise ValueError("bad rewrite_id")
    REWRITE_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    proj_dir = _rewrite_project_dir(rewrite_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["rewrite_id"] = rewrite_id
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    stages = data.get("stages")
    if isinstance(stages, dict):
        for sk in REWRITE_STAGE_KEYS:
            cell = stages.get(sk) if isinstance(stages.get(sk), dict) else {}
            res_text = str((cell or {}).get("last_result") or "")
            _rewrite_stage_result_path(rewrite_id, sk).write_text(res_text, encoding="utf-8")

    # Keep project JSON lean: stage results are persisted in separate files.
    project_data = json.loads(json.dumps(data, ensure_ascii=False))
    p_stages = project_data.get("stages")
    if isinstance(p_stages, dict):
        for sk in REWRITE_STAGE_KEYS:
            if isinstance(p_stages.get(sk), dict):
                p_stages[sk]["last_result"] = ""
    project_data["last_result"] = ""

    with open(_rewrite_project_json_path(rewrite_id), "w", encoding="utf-8") as f:
        json.dump(project_data, f, ensure_ascii=False, indent=2)

    # One-time migration cleanup: old single-file format is no longer used.
    legacy_fp = _rewrite_legacy_filepath(rewrite_id)
    if legacy_fp.is_file():
        legacy_fp.unlink(missing_ok=True)


def list_rewrite_jobs() -> list[dict]:
    rows = []
    if not REWRITE_JOBS_DIR.is_dir():
        return rows
    ids: set[str] = set()
    for d in REWRITE_JOBS_DIR.glob("rewrite_*"):
        if d.is_dir() and rewrite_id_ok(d.name):
            ids.add(d.name)
    for f in REWRITE_JOBS_DIR.glob("rewrite_*.json"):
        if rewrite_id_ok(f.stem):
            ids.add(f.stem)

    def _rid_mtime(rid: str) -> float:
        p = _rewrite_project_json_path(rid)
        if p.is_file():
            return p.stat().st_mtime
        lf = _rewrite_legacy_filepath(rid)
        if lf.is_file():
            return lf.stat().st_mtime
        d = _rewrite_project_dir(rid)
        if d.is_dir():
            return d.stat().st_mtime
        return 0.0

    for rid in sorted(ids, key=_rid_mtime, reverse=True):
        if not rewrite_id_ok(rid):
            continue
        try:
            data = load_rewrite_job(rid)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        normalize_rewrite_job_data(data)
        rows.append(
            {
                "rewrite_id": rid,
                "project_name": data.get("project_name", "") or rid,
                "updated_at": data.get("updated_at", data.get("created_at", "")),
                "has_result": any_stage_has_result(data),
            }
        )
    return rows


def compute_summary(scenes: list[dict]) -> dict:
    """Вычисляет summary по сценам."""
    start_count = sum(1 for s in scenes if s.get("start", {}).get("prompt"))
    end_count = sum(1 for s in scenes if s.get("end", {}).get("prompt"))
    video_count = sum(1 for s in scenes if s.get("video", {}).get("prompt"))
    return {
        "total": len(scenes),
        "with_start_prompt": start_count,
        "with_end_prompt": end_count,
        "with_video_prompt": video_count,
    }


def normalize_video_model(value: str | None) -> str:
    """Normalize UI video model ids to canonical values."""
    normalized = (value or "").strip().lower()
    if normalized in {"veo3-fast", "veo3_fast", "veo 3.1 fast"}:
        return "veo3_fast"
    if normalized in {"veo3", "veo 3.1 quality"}:
        return "veo3"
    if normalized in {"grok-imagine/image-to-video", "grok imagine image to video", "grok-imagine"}:
        return "grok-imagine/image-to-video"
    return "veo3_fast"


def video_model_label(value: str | None) -> str:
    model_id = normalize_video_model(value)
    if model_id == "grok-imagine/image-to-video":
        return "Grok Imagine Image to Video"
    return "Veo 3.1 Fast" if model_id == "veo3_fast" else "Veo 3.1 Quality"


def _kie_gen_extra(task_meta: dict) -> dict:
    """Поля трассировки запроса Kie для сохранения в scene[slot].generation."""
    out: dict = {}
    km = (task_meta.get("kie_api_model") or "").strip()
    kp = (task_meta.get("kie_request_path") or "").strip()
    if km:
        out["kie_api_model"] = km
    if kp:
        out["kie_request_path"] = kp
    return out


IMAGE_MODELS_REQUIRE_REFERENCE_URLS: frozenset[str] = frozenset(
    (
        "gpt-image-2-image-to-image",
        "grok-imagine/image-to-image",
        "qwen2/image-edit",
    )
)


def normalize_image_model(value: str | None) -> str:
    """Значение модели для Kie createTask (известные id из UI)."""
    raw = (value or "").strip().lower()
    mid = raw.replace(" ", "-")
    if mid == "nano-banana-2":
        return "nano-banana-2"
    if mid == "gpt-image-2-image-to-image":
        return "gpt-image-2-image-to-image"
    if raw == "grok-imagine/image-to-image":
        return "grok-imagine/image-to-image"
    if raw == "wan/2-7-image":
        return "wan/2-7-image"
    if raw == "qwen2/image-edit":
        return "qwen2/image-edit"
    return "nano-banana-pro"


def image_model_label(value: str | None) -> str:
    """Короткая подпись для UI (Nano Banana Pro и т.д.)."""
    raw = (value or "").strip().lower()
    mid = raw.replace(" ", "-")
    if mid == "nano-banana-pro":
        return "Nano Banana Pro"
    if mid == "nano-banana-2":
        return "Google - Nano Banana 2"
    if mid == "gpt-image-2-image-to-image":
        return "GPT Image 2 - Image To Image"
    if raw == "grok-imagine/image-to-image":
        return "Grok Imagine — Image To Image"
    if raw == "wan/2-7-image":
        return "Wan 2.7 Image"
    if raw == "qwen2/image-edit":
        return "Qwen2 - Image Edit"
    return (value or "").strip() or "Nano Banana Pro"


# --- Routes ---

def render_index(**kwargs):
    """Рендер главной с общим контекстом (список проектов)."""
    ctx = {"jobs": list_jobs(), "image_templates": templates_ui_rows()}
    ctx.update(kwargs)
    resp = make_response(render_template("index.html", **ctx))
    # Avoid stale HTML (settings form) after deploy — some browsers cache aggressively.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/")
def index():
    resp = make_response(render_template("home.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/video")
def video_index():
    return render_index()


@app.route("/video", methods=["POST"])
def video_create():
    project_name = request.form.get("project_name", "").strip()
    payload = new_video_job_payload(project_name)
    _filepath, job_id = save_job_file(payload)
    flash("Video-проект создан.", "success")
    return redirect(url_for("job_page", job_id=job_id))


@app.route("/rewrite", methods=["GET", "POST"])
def rewrite_index():
    """Список проектов ReWrite Master + создание нового."""
    if request.method == "POST":
        project_name = request.form.get("project_name", "").strip()
        rid = create_rewrite_job(project_name)
        flash("Проект создан.", "success")
        return redirect(url_for("rewrite_project_page", rewrite_id=rid))

    resp = make_response(
        render_template(
            "rewrite_index.html",
            rewrite_jobs=list_rewrite_jobs(),
            openai_key_set=bool((os.getenv("OPENAI_API_KEY") or "").strip()),
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/rewrite/<rewrite_id>")
def rewrite_project_page(rewrite_id: str):
    """Страница одного проекта ReWrite (форма + статусы + ответ)."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        flash("Проект ReWrite не найден.", "error")
        return redirect(url_for("rewrite_index"))
    key_set = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    st = rw.get("stages")
    if not isinstance(st, dict):
        st = {}
    rewrite_preset_current = normalize_rewrite_preset(rw.get("rewrite_preset"))
    rewrite_stage_run_ok = {
        sk: stage_run_prerequisites_met(sk, st, preset=rewrite_preset_current)
        for sk in REWRITE_STAGE_KEYS
    }
    rewrite_stage_key_order = [k for k, _ in REWRITE_STAGES]
    voiceover_final_text = str(rw.get("voiceover_final_text") or "")
    if not voiceover_final_text.strip():
        voiceover_final_text = _extract_edited_text(
            str(((st.get("voiceover_editor") or {}).get("last_result")) or "")
        )
    try:
        resp = make_response(
            render_template(
                "rewrite_project.html",
                rw=rw,
                rewrite_stages=REWRITE_STAGES,
                rewrite_stage_send_hints=REWRITE_STAGE_SEND_HINTS,
                rewrite_stage_help_hints=REWRITE_STAGE_HELP_HINTS,
                rewrite_stage_subtitles=REWRITE_STAGE_SUBTITLES,
                rewrite_stage_run_ok=rewrite_stage_run_ok,
                rewrite_stage_key_order=rewrite_stage_key_order,
                rewrite_preset_current=rewrite_preset_current,
                rewrite_preset_labels=REWRITE_PRESET_LABELS,
                rewrite_preset_stage_keys=REWRITE_PRESET_STAGE_KEYS,
                rewrite_preset_default=REWRITE_PRESET_DEFAULT,
                rewrite_models=REWRITE_MODELS,
                rewrite_template_names=list_rewrite_template_names(),
                openai_key_set=key_set,
                voiceover_final_text=voiceover_final_text,
                youtube_cookies_status=youtube_cookies_status_dict(),
            )
        )
    except Exception:
        app.logger.exception("rewrite_project_page: render failed for %s", rewrite_id)
        return (
            "Ошибка отрисовки страницы проекта. Проверьте логи сервиса (например, journalctl -u json-video -n 80).",
            500,
        )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/rewrite/api/templates", methods=["GET"])
def rewrite_api_templates_list():
    return jsonify({"ok": True, "templates": list_rewrite_template_names()})


@app.route("/rewrite/api/templates", methods=["POST"])
def rewrite_api_templates_create():
    """Создать новый rewrite-шаблон из текущих данных формы."""
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "bad_name", "message": "Введите название шаблона."}), 400
    if any(ch in name for ch in ('/', '\\')) or name.startswith('.'):
        return jsonify({"ok": False, "error": "bad_name", "message": "Недопустимое имя шаблона."}), 400
    known = set(list_rewrite_template_names())
    if name in known:
        return jsonify({"ok": False, "error": "already_exists", "message": "Шаблон с таким именем уже существует."}), 409

    d = REWRITE_TEMPLATES_DIR / name
    try:
        d.mkdir(parents=True, exist_ok=False)
    except OSError:
        return jsonify({"ok": False, "error": "mkdir_failed", "message": "Не удалось создать папку шаблона."}), 400

    stages = body.get("stages")
    if not isinstance(stages, dict):
        stages = {}
    tc_raw = body.get("target_chars")
    if tc_raw is not None and str(tc_raw).strip() != "":
        try:
            target_chars = clamp_target_chars(int(tc_raw))
        except (TypeError, ValueError):
            target_chars = clamp_target_chars(5 * 344)
    else:
        try:
            cpm = int(body.get("chars_per_minute", 344))
        except (TypeError, ValueError):
            cpm = 344
        try:
            dm = int(body.get("duration_minutes", 5))
        except (TypeError, ValueError):
            dm = 5
        target_chars = clamp_target_chars(cpm * dm)

    ok, err = save_rewrite_template_to_disk(
        name,
        hero_prompt=str(body.get("hero_prompt") or ""),
        master_prompt=str(body.get("master_prompt") or ""),
        target_chars=target_chars,
        stages=stages,
    )
    if not ok:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"ok": False, "error": err or "save_failed", "message": "Не удалось записать файлы шаблона."}), 400
    return jsonify({"ok": True, "name": name})


@app.route("/rewrite/api/templates/<name>", methods=["DELETE"])
def rewrite_api_template_delete(name: str):
    nn = str(name or "").strip()
    if not nn:
        return jsonify({"ok": False, "error": "bad_name"}), 400
    if nn.lower() == "base template":
        return jsonify({"ok": False, "error": "protected", "message": "Base Template нельзя удалить."}), 400
    known = set(list_rewrite_template_names())
    if nn not in known:
        return jsonify({"ok": False, "error": "not_found"}), 404
    d = REWRITE_TEMPLATES_DIR / nn
    if not d.is_dir():
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        shutil.rmtree(d)
    except OSError:
        return jsonify({"ok": False, "error": "delete_failed", "message": "Не удалось удалить шаблон."}), 400
    return jsonify({"ok": True})


@app.route("/rewrite/api/templates/<name>", methods=["GET"])
def rewrite_api_template_get(name: str):
    data = load_rewrite_template(name)
    if data is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, **data})


@app.route("/rewrite/api/templates/<name>/save", methods=["POST"])
def rewrite_api_template_save(name: str):
    """Сохранить текущие поля промптов и Config в подпапку rewrite_templates/<name>/."""
    body = request.get_json(silent=True) or {}
    known = set(list_rewrite_template_names())
    if name.strip() not in known:
        return jsonify({"ok": False, "error": "not_found"}), 404
    stages = body.get("stages")
    if not isinstance(stages, dict):
        stages = {}
    tc_raw = body.get("target_chars")
    if tc_raw is not None and str(tc_raw).strip() != "":
        try:
            target_chars = clamp_target_chars(int(tc_raw))
        except (TypeError, ValueError):
            target_chars = clamp_target_chars(5 * 344)
    else:
        try:
            cpm = int(body.get("chars_per_minute", 344))
        except (TypeError, ValueError):
            cpm = 344
        try:
            dm = int(body.get("duration_minutes", 5))
        except (TypeError, ValueError):
            dm = 5
        target_chars = clamp_target_chars(cpm * dm)
    ok, err = save_rewrite_template_to_disk(
        name.strip(),
        hero_prompt=str(body.get("hero_prompt") or ""),
        master_prompt=str(body.get("master_prompt") or ""),
        target_chars=target_chars,
        stages=stages,
    )
    if not ok:
        return jsonify({"ok": False, "error": err or "save_failed"}), 400
    return jsonify({"ok": True})


@app.route("/rewrite/<rewrite_id>/rename", methods=["POST"])
def rewrite_project_rename(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        flash("Проект не найден.", "error")
        return redirect(url_for("rewrite_index"))
    name = request.form.get("project_name", "").strip()
    rw["project_name"] = name or rewrite_id
    save_rewrite_job(rewrite_id, rw)
    flash("Название обновлено.", "success")
    return redirect(url_for("rewrite_project_page", rewrite_id=rewrite_id))


@app.route("/rewrite/<rewrite_id>/delete", methods=["POST"])
def rewrite_project_delete(rewrite_id: str):
    fp = _rewrite_project_json_path(rewrite_id)
    legacy_fp = _rewrite_legacy_filepath(rewrite_id)
    d = _rewrite_project_dir(rewrite_id)
    if rewrite_id_ok(rewrite_id) and (fp.is_file() or legacy_fp.is_file() or d.is_dir()):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        if legacy_fp.is_file():
            legacy_fp.unlink(missing_ok=True)
        flash("Проект ReWrite удалён.", "success")
    else:
        flash("Проект не найден.", "error")
    return redirect(url_for("rewrite_index"))


@app.route("/rewrite/<rewrite_id>/save", methods=["POST"])
def rewrite_project_save(rewrite_id: str):
    """Сохранение: исходный текст, настройки и результаты по этапам."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    locked_in_body = body.get("source_locked") if "source_locked" in body else None
    # Снимок с формы — источник истины; lock только режим UI, не отбрасываем значения.
    if "source_text" in body:
        rw["source_text"] = str(body.get("source_text") or "")
    if "source_title" in body:
        rw["source_title"] = str(body.get("source_title") or "")
    if "source_text_ru" in body:
        rw["source_text_ru"] = str(body.get("source_text_ru") or "")
    ru_lock_in = body.get("source_text_ru_locked") if "source_text_ru_locked" in body else None
    if ru_lock_in is not None:
        rw["source_text_ru_locked"] = bool(ru_lock_in)
    if "voiceover_final_text" in body:
        rw["voiceover_final_text"] = str(body.get("voiceover_final_text") or "")
    vf_lock_in = body.get("voiceover_final_locked") if "voiceover_final_locked" in body else None
    if vf_lock_in is not None:
        rw["voiceover_final_locked"] = bool(vf_lock_in)
    if "voiceover_final_text_ru" in body:
        rw["voiceover_final_text_ru"] = str(body.get("voiceover_final_text_ru") or "")
    vfr_lock_in = body.get("voiceover_final_text_ru_locked") if "voiceover_final_text_ru_locked" in body else None
    if vfr_lock_in is not None:
        rw["voiceover_final_text_ru_locked"] = bool(vfr_lock_in)
    if locked_in_body is not None:
        rw["source_locked"] = bool(locked_in_body)
    m_lock_in = body.get("master_prompt_locked") if "master_prompt_locked" in body else None
    if "master_prompt" in body:
        rw["master_prompt"] = str(body.get("master_prompt") or "")
    if m_lock_in is not None:
        rw["master_prompt_locked"] = bool(m_lock_in)
    h_lock_in = body.get("hero_prompt_locked") if "hero_prompt_locked" in body else None
    if "hero_prompt" in body:
        rw["hero_prompt"] = str(body.get("hero_prompt") or "")
    if h_lock_in is not None:
        rw["hero_prompt_locked"] = bool(h_lock_in)

    at_lock_in = body.get("audio_timing_locked") if "audio_timing_locked" in body else None
    if "target_chars" in body and body.get("target_chars") is not None and str(body.get("target_chars", "")).strip() != "":
        try:
            rw["target_chars"] = clamp_target_chars(int(body.get("target_chars")))
        except (TypeError, ValueError):
            pass
    elif "duration_minutes" in body or "chars_per_minute" in body:
        try:
            dm = int(body.get("duration_minutes", rw.get("duration_minutes", 5)))
            rw["duration_minutes"] = max(1, min(30, dm))
        except (TypeError, ValueError):
            pass
        try:
            cpm = int(body.get("chars_per_minute", rw.get("chars_per_minute", 344)))
            rw["chars_per_minute"] = max(1, min(2000, cpm))
        except (TypeError, ValueError):
            pass
        rw["target_chars"] = clamp_target_chars(int(rw["duration_minutes"]) * int(rw["chars_per_minute"]))
    if at_lock_in is not None:
        rw["audio_timing_locked"] = bool(at_lock_in)
    if "rewrite_template" in body:
        rw["rewrite_template"] = str(body.get("rewrite_template") or "").strip()
    if "rewrite_preset" in body:
        rw["rewrite_preset"] = normalize_rewrite_preset(body.get("rewrite_preset"))
    merge_stages_from_request(rw, body.get("stages"))
    if "model" in body:
        rw["model"] = normalize_rewrite_model(str(body.get("model") or ""))
    if "last_prompt" in body:
        rw["last_prompt"] = str(body.get("last_prompt") or "")
    if "last_text" in body:
        rw["last_text"] = str(body.get("last_text") or "")
    if "last_result" in body:
        rw["last_result"] = str(body.get("last_result") or "")
    save_rewrite_job(rewrite_id, rw)
    return jsonify({"ok": True})


@app.route("/rewrite/<rewrite_id>/translate-source-ru", methods=["POST"])
def rewrite_translate_source_ru(rewrite_id: str):
    """Перевод исходного текста на русский (батчи ~5000 симв., OpenAI). Ответ — NDJSON стрим."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    source_text = str(body.get("source_text") if "source_text" in body else rw.get("source_text") or "")
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    stages = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    ana = stages.get("analysis") if isinstance(stages.get("analysis"), dict) else {}
    model = normalize_rewrite_model(str(body.get("model") or rw.get("model") or ana.get("model") or ""))
    batches = _split_text_into_translation_batches(source_text, 5000)

    def gen():
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not source_text.strip():
            yield json.dumps({"type": "error", "message": "Нет текста для перевода."}, ensure_ascii=False) + "\n"
            return
        if not api_key_present or not api_key:
            yield json.dumps({"type": "error", "message": "Не задан OPENAI_API_KEY."}, ensure_ascii=False) + "\n"
            return
        if not batches:
            yield json.dumps({"type": "error", "message": "Нет текста для перевода."}, ensure_ascii=False) + "\n"
            return
        nb = len(batches)
        yield json.dumps(
            {
                "type": "status",
                "message": f"Разбиение текста на батчи готово: {nb} шт. (≤5000 симв./батч).",
            },
            ensure_ascii=False,
        ) + "\n"
        yield json.dumps({"type": "status", "message": f"Модель: {model}"}, ensure_ascii=False) + "\n"
        parts: list[str] = []
        for bi, chunk in enumerate(batches):
            tag = f"[Батч {bi + 1}/{nb}, {len(chunk)} симв.] "
            yield json.dumps(
                {"type": "status", "message": tag + "Старт перевода батча…"},
                ensure_ascii=False,
            ) + "\n"
            user_msg = "ТЕКСТ ДЛЯ ПЕРЕВОДА:\n\n" + chunk
            got_result = False
            err_text: str | None = None
            for ev in iter_rewrite_completion(
                api_key,
                model,
                REWRITE_SOURCE_RU_TRANSLATE_SYSTEM_PROMPT,
                user_msg,
            ):
                etype = str(ev.get("type") or "")
                if etype == "status":
                    yield json.dumps(
                        {"type": "status", "message": tag + str(ev.get("message") or "")},
                        ensure_ascii=False,
                    ) + "\n"
                elif etype == "error":
                    err_text = str(ev.get("message") or "Ошибка OpenAI")
                    break
                elif etype == "result":
                    parts.append(str(ev.get("content") or ""))
                    got_result = True
                    yield json.dumps(
                        {
                            "type": "status",
                            "message": tag + f"Готово: получено {len(parts[-1])} симв.",
                        },
                        ensure_ascii=False,
                    ) + "\n"
            if err_text is not None:
                yield json.dumps({"type": "error", "message": tag + err_text}, ensure_ascii=False) + "\n"
                return
            if not got_result:
                yield json.dumps(
                    {"type": "error", "message": tag + "Пустой ответ модели."},
                    ensure_ascii=False,
                ) + "\n"
                return
        combined = "".join(parts).strip()
        try:
            rw["source_text_ru"] = combined
            save_rewrite_job(rewrite_id, rw)
            yield json.dumps(
                {"type": "status", "message": "Сохранено в project.json (поле source_text_ru)."},
                ensure_ascii=False,
            ) + "\n"
        except Exception as e:
            yield json.dumps(
                {"type": "status", "message": f"Не удалось сохранить project.json: {e}"},
                ensure_ascii=False,
            ) + "\n"
        yield json.dumps(
            {
                "type": "result",
                "content": combined,
                "batches": nb,
                "chars": len(combined),
            },
            ensure_ascii=False,
        ) + "\n"

    return Response(stream_with_context(gen()), mimetype="application/x-ndjson")


@app.route("/rewrite/<rewrite_id>/translate-voiceover-final-ru", methods=["POST"])
def rewrite_translate_voiceover_final_ru(rewrite_id: str):
    """Перевод «Итогового текста» (Voiceover final) на русский. NDJSON-стрим, та же логика, что и у Source."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    src_text_in = str(body.get("text") or "")
    if not src_text_in.strip():
        src_text_in = str(rw.get("voiceover_final_text") or "")
    if not src_text_in.strip():
        st_local = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
        ve = st_local.get("voiceover_editor") if isinstance(st_local.get("voiceover_editor"), dict) else {}
        raw = str(ve.get("last_result") or "")
        if not raw.strip():
            p = _rewrite_stage_result_path(rewrite_id, "voiceover_editor")
            if p.exists():
                try:
                    raw = p.read_text(encoding="utf-8")
                except OSError:
                    raw = ""
        src_text_in = _extract_edited_text(raw)
    stages_dict = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    ana = stages_dict.get("analysis") if isinstance(stages_dict.get("analysis"), dict) else {}
    model = normalize_rewrite_model(str(body.get("model") or rw.get("model") or ana.get("model") or ""))
    batches = _split_text_into_translation_batches(src_text_in, 5000)

    def gen():
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not src_text_in.strip():
            yield json.dumps(
                {"type": "error", "message": "Нет итогового текста. Сначала получите Result в Voiceover Editor."},
                ensure_ascii=False,
            ) + "\n"
            return
        if not api_key_present or not api_key:
            yield json.dumps({"type": "error", "message": "Не задан OPENAI_API_KEY."}, ensure_ascii=False) + "\n"
            return
        if not batches:
            yield json.dumps({"type": "error", "message": "Нет текста для перевода."}, ensure_ascii=False) + "\n"
            return
        nb = len(batches)
        yield json.dumps(
            {
                "type": "status",
                "message": f"Разбиение текста на батчи готово: {nb} шт. (≤5000 симв./батч).",
            },
            ensure_ascii=False,
        ) + "\n"
        yield json.dumps({"type": "status", "message": f"Модель: {model}"}, ensure_ascii=False) + "\n"
        parts: list[str] = []
        for bi, chunk in enumerate(batches):
            tag = f"[Батч {bi + 1}/{nb}, {len(chunk)} симв.] "
            yield json.dumps(
                {"type": "status", "message": tag + "Старт перевода батча…"},
                ensure_ascii=False,
            ) + "\n"
            user_msg = "ТЕКСТ ДЛЯ ПЕРЕВОДА:\n\n" + chunk
            got_result = False
            err_text: str | None = None
            for ev in iter_rewrite_completion(
                api_key,
                model,
                REWRITE_SOURCE_RU_TRANSLATE_SYSTEM_PROMPT,
                user_msg,
            ):
                etype = str(ev.get("type") or "")
                if etype == "status":
                    yield json.dumps(
                        {"type": "status", "message": tag + str(ev.get("message") or "")},
                        ensure_ascii=False,
                    ) + "\n"
                elif etype == "error":
                    err_text = str(ev.get("message") or "Ошибка OpenAI")
                    break
                elif etype == "result":
                    parts.append(str(ev.get("content") or ""))
                    got_result = True
                    yield json.dumps(
                        {
                            "type": "status",
                            "message": tag + f"Готово: получено {len(parts[-1])} симв.",
                        },
                        ensure_ascii=False,
                    ) + "\n"
            if err_text is not None:
                yield json.dumps({"type": "error", "message": tag + err_text}, ensure_ascii=False) + "\n"
                return
            if not got_result:
                yield json.dumps(
                    {"type": "error", "message": tag + "Пустой ответ модели."},
                    ensure_ascii=False,
                ) + "\n"
                return
        combined = "".join(parts).strip()
        try:
            rw["voiceover_final_text_ru"] = combined
            rw["voiceover_final_text_ru_locked"] = True
            save_rewrite_job(rewrite_id, rw)
            yield json.dumps(
                {"type": "status", "message": "Сохранено в project.json (поле voiceover_final_text_ru)."},
                ensure_ascii=False,
            ) + "\n"
        except Exception as e:
            yield json.dumps(
                {"type": "status", "message": f"Не удалось сохранить project.json: {e}"},
                ensure_ascii=False,
            ) + "\n"
        yield json.dumps(
            {"type": "result", "content": combined, "batches": nb, "chars": len(combined)},
            ensure_ascii=False,
        ) + "\n"

    return Response(stream_with_context(gen()), mimetype="application/x-ndjson")


@app.route("/rewrite/<rewrite_id>/youtube/verify", methods=["POST"])
def rewrite_youtube_verify(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    prev_url = _youtube_url_normalize(str(rw.get("youtube_url") or ""))
    url = _youtube_url_normalize(str(body.get("youtube_url") or ""))
    if not _youtube_url_is_valid(url):
        rw["youtube_url"] = url
        rw["youtube_verified"] = False
        rw["youtube_title"] = ""
        save_rewrite_job(rewrite_id, rw)
        return jsonify({"ok": False, "message": _youtube_url_rejection_message(url)}), 400
    title = ""
    try:
        info: dict | None = None
        last_verify_err: Exception | None = None
        clients = _youtube_player_client_chain()
        for ci, cname in enumerate(clients):
            try:
                v_socket = _youtube_verify_socket_sec()
                with YoutubeDL(
                    {
                        **_YOUTUBE_YDL_BASE,
                        **_youtube_cookiefile_opts(),
                        "socket_timeout": v_socket,
                        "retries": 1,
                        "fragment_retries": 1,
                        **_ytdl_youtube_extractor_player_client(cname),
                        "skip_download": True,
                    }
                ) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not isinstance(info, dict):
                        raise RuntimeError("yt-dlp: пустой ответ об видео.")
                    if ci > 0:
                        app.logger.info("youtube verify ok with player_client=%s (after %d fallback(s))", cname, ci)
                    try:
                        pdir = _rewrite_project_dir(rewrite_id)
                        pdir.mkdir(parents=True, exist_ok=True)
                        with open(_youtube_info_cache_path(rewrite_id), "w", encoding="utf-8") as f:
                            f.write(json.dumps(ydl.sanitize_info(info), ensure_ascii=False, indent=2))
                    except (OSError, TypeError) as e:
                        app.logger.warning("youtube_info_cache write %s: %s", rewrite_id, e)
                last_verify_err = None
                break
            except Exception as e:
                last_verify_err = e
                if ci + 1 < len(clients):
                    app.logger.warning("youtube verify player_client=%s failed: %s, try next", cname, e)
                else:
                    app.logger.warning("youtube verify player_client=%s failed: %s", cname, e)
                if ci + 1 >= len(clients):
                    raise last_verify_err from e
        if not isinstance(info, dict):
            raise (last_verify_err or RuntimeError("yt-dlp: нет ответа об видео.")) from last_verify_err
        title = str((info or {}).get("title") or "").strip()
    except Exception as e:
        rw["youtube_url"] = url
        rw["youtube_verified"] = False
        rw["youtube_title"] = ""
        save_rewrite_job(rewrite_id, rw)
        return jsonify({"ok": False, "message": f"Не удалось проверить ссылку: {e}"}), 400
    rw["youtube_url"] = url
    rw["youtube_verified"] = True
    rw["youtube_title"] = title
    if prev_url != url:
        rw["youtube_audio_file"] = ""
        rw["youtube_transcript_text"] = ""
        rw["youtube_transcript_url"] = ""
        rw["youtube_processing"] = False
        rw["youtube_phase"] = ""
        rw["youtube_status"] = ""
    save_rewrite_job(rewrite_id, rw)
    return jsonify({"ok": True, "youtube_title": title})


@app.route("/rewrite/<rewrite_id>/youtube/cookies/status", methods=["GET"])
def rewrite_youtube_cookies_status(rewrite_id: str):
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, **youtube_cookies_status_dict()})


@app.route("/rewrite/<rewrite_id>/youtube/cookies", methods=["POST"])
def rewrite_youtube_cookies_upload(rewrite_id: str):
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    f = request.files.get("file")
    if f is None or not getattr(f, "filename", None):
        return jsonify({"ok": False, "message": "Выберите файл cookies.txt."}), 400
    raw = f.read()
    err = _youtube_validate_cookies_upload(raw)
    if err:
        return jsonify({"ok": False, "message": err}), 400
    dest = _youtube_cookies_file_path()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
    except OSError as e:
        return jsonify({"ok": False, "message": f"Не удалось сохранить файл: {e}"}), 500
    return jsonify({"ok": True, **youtube_cookies_status_dict()})


def _rewrite_youtube_perform_download(
    rewrite_id: str,
    rw: dict,
    *,
    progress_hooks: list | None = None,
    postprocessor_hooks: list | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """
    Скачивает лучший аудиопоток через yt-dlp (запросы идут на CDN YouTube, чаще всего *.googlevideo.com).
    Возвращает (относительный путь к mp3 от BASE_DIR, заголовок).
    При сбоях (таймаут, SSL, обрыв CDN) перебирает цепочку player_client (YOUTUBE_PLAYER_CLIENT
    + YOUTUBE_PLAYER_CLIENT_FALLBACK).
    """
    url = _youtube_url_normalize(str(rw.get("youtube_url") or ""))
    if not rw.get("youtube_verified") or not _youtube_url_is_valid(url):
        raise ValueError("Сначала проверьте ссылку YouTube.")
    media_dir = _rewrite_media_dir(rewrite_id)
    media_dir.mkdir(parents=True, exist_ok=True)
    for old in media_dir.glob("youtube_audio_*"):
        if old.is_file():
            old.unlink(missing_ok=True)
    outtmpl = str(media_dir / "youtube_audio_%(id)s.%(ext)s")
    stall_read_sec = _youtube_stall_read_sec()
    clients = _youtube_player_client_chain()
    n_cli = len(clients)
    info: dict | None = None
    last_err: BaseException | None = None
    format_chain = _youtube_format_chain()
    n_fmt = len(format_chain)
    for ci, cname in enumerate(clients):
        for fi, fmt in enumerate(format_chain):
            if status_callback is not None:
                status_callback(
                    f"YT-DLP: YouTube client «{cname}» ({ci + 1}/{n_cli}), формат ({fi + 1}/{n_fmt}), "
                    f"тайм-аут сокета {stall_read_sec} с; если нет ответа/формата — следующий вариант…"
                )
            same_re = _youtube_same_client_retries()
            ydl_opts: dict = {
                **_YOUTUBE_YDL_BASE,
                **_youtube_cookiefile_opts(),
                "socket_timeout": stall_read_sec,
                "retries": same_re,
                "fragment_retries": same_re,
                **_ytdl_youtube_extractor_player_client(cname),
                # Без принудительной ffmpeg-конвертации в MP3: это место иногда зависало.
                # OpenAI принимает m4a/webm/mp3, поэтому берем bestaudio и отдаем как есть.
                "format": fmt,
                "outtmpl": outtmpl,
            }
            ydl_opts["progress_hooks"] = _rewrite_youtube_progress_hooks_with_stall(stall_read_sec, progress_hooks)
            if postprocessor_hooks:
                ydl_opts["postprocessor_hooks"] = list(postprocessor_hooks)
            # bestaudio — только аудио (m4a/webm), без принудительной ffmpeg-конвертации.
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    out = ydl.extract_info(url, download=True)
                if not isinstance(out, dict):
                    raise RuntimeError("yt-dlp: не single-video (ожидался один ролик, noplaylist).")
                info = out
                if ci > 0 or fi > 0:
                    app.logger.info(
                        "youtube download ok rewrite_id=%s player_client=%s format=%s after fallback",
                        rewrite_id,
                        cname,
                        fmt,
                    )
                break
            except BaseException as e:  # noqa: BLE001 — смена format/client после любой сбойной попытки
                last_err = e
                app.logger.warning(
                    "youtube download rewrite_id=%s player_client=%s format=%s failed: %s",
                    rewrite_id,
                    cname,
                    fmt,
                    e,
                )
                if _youtube_error_is_format_unavailable(e):
                    try:
                        dynamic_format_id = _youtube_probe_audio_format_id(url, cname, stall_read_sec)
                    except BaseException as probe_err:  # noqa: BLE001
                        dynamic_format_id = None
                        app.logger.warning(
                            "youtube probe formats rewrite_id=%s player_client=%s failed: %s",
                            rewrite_id,
                            cname,
                            probe_err,
                        )
                    if dynamic_format_id:
                        if status_callback is not None:
                            status_callback(
                                f"Формат недоступен для client «{cname}». Нашли доступный format_id={dynamic_format_id}, повторяем…"
                            )
                        _rewrite_youtube_clear_partial_downloads(media_dir)
                        try:
                            ydl_opts_dynamic = dict(ydl_opts)
                            ydl_opts_dynamic["format"] = dynamic_format_id
                            with YoutubeDL(ydl_opts_dynamic) as ydl:
                                out = ydl.extract_info(url, download=True)
                            if not isinstance(out, dict):
                                raise RuntimeError("yt-dlp: не single-video (ожидался один ролик, noplaylist).")
                            info = out
                            app.logger.info(
                                "youtube download ok rewrite_id=%s player_client=%s dynamic_format_id=%s",
                                rewrite_id,
                                cname,
                                dynamic_format_id,
                            )
                            break
                        except BaseException as dyn_err:  # noqa: BLE001
                            last_err = dyn_err
                            app.logger.warning(
                                "youtube download rewrite_id=%s player_client=%s dynamic_format_id=%s failed: %s",
                                rewrite_id,
                                cname,
                                dynamic_format_id,
                                dyn_err,
                            )
                has_next_format = fi + 1 < n_fmt
                has_next_client = ci + 1 < n_cli
                short = (str(e) or "")[:220]
                _rewrite_youtube_clear_partial_downloads(media_dir)
                if has_next_format:
                    if status_callback is not None:
                        status_callback(
                            f"Формат недоступен/ошибка для client «{cname}» ({short}). Пробуем другой формат…"
                        )
                    continue
                if has_next_client:
                    if status_callback is not None:
                        status_callback(
                            f"Ошибка с YouTube client «{cname}» ({short}). Следующий client из цепочки…"
                        )
                    break
                raise
        if isinstance(info, dict):
            break
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp не вернул сведения о ролике.") from last_err
    video_id = str((info or {}).get("id") or "").strip()
    title = str((info or {}).get("title") or "").strip()
    if not video_id:
        raise RuntimeError("Не удалось определить id видео.")
    audio_files = sorted(
        [
            p
            for p in media_dir.glob("youtube_audio_*")
            if p.is_file() and not p.name.endswith(".part") and not p.name.endswith(".ytdl")
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not audio_files:
        raise RuntimeError("Аудиофайл не найден после скачивания.")
    audio_path = audio_files[0]
    rw["youtube_audio_file"] = str(audio_path.relative_to(BASE_DIR))
    if title:
        rw["youtube_title"] = title
    save_rewrite_job(rewrite_id, rw)
    return rw["youtube_audio_file"], rw.get("youtube_title", "")


@app.route("/rewrite/<rewrite_id>/youtube/download", methods=["POST"])
def rewrite_youtube_download(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        rel, title = _rewrite_youtube_perform_download(rewrite_id, rw)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"Не удалось скачать аудио: {e}"}), 400
    return jsonify({"ok": True, "audio_file": rel, "youtube_title": title})


@app.route("/rewrite/<rewrite_id>/youtube/download_stream", methods=["POST"])
def rewrite_youtube_download_stream(rewrite_id: str):
    """Тот же скачиватель yt-dlp, но NDJSON-стрим с прогрессом (байты для UI в КиБ)."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    url = _youtube_url_normalize(str(rw.get("youtube_url") or ""))
    if not rw.get("youtube_verified") or not _youtube_url_is_valid(url):
        return jsonify({"ok": False, "message": "Сначала проверьте ссылку YouTube."}), 400

    event_q: queue.Queue[str | None] = queue.Queue()
    result_holder: dict[str, str | None] = {}
    last_progress_mono = [0.0]
    last_state_save = [0.0]

    _rewrite_youtube_set_runtime_state(
        rewrite_id,
        processing=True,
        phase="download",
        status="YT-DLP скачивание: только аудио (bestaudio), скоро появится прогресс…",
    )

    def persist_runtime_status(msg: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - last_state_save[0] < 1.0):
            return
        last_state_save[0] = now
        _rewrite_youtube_set_runtime_state(rewrite_id, processing=True, phase="download", status=msg)

    def emit(obj: dict) -> None:
        event_q.put(json.dumps(obj, ensure_ascii=False))

    def progress_hook(d: dict) -> None:
        st = d.get("status")
        if st == "downloading":
            now = time.monotonic()
            if now - last_progress_mono[0] < 0.22:
                return
            last_progress_mono[0] = now
            got = d.get("downloaded_bytes")
            emit(
                {
                    "type": "progress",
                    "phase": "download",
                    "downloaded_bytes": got,
                    "total_bytes": d.get("total_bytes"),
                    "total_bytes_estimate": d.get("total_bytes_estimate"),
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                }
            )
            try:
                persist_runtime_status(f"Скачивание аудио: {int(got or 0)} байт…")
            except Exception:
                pass
        elif st == "finished":
            emit(
                {
                    "type": "progress",
                    "phase": "fragment_done",
                    "filename": d.get("filename") or "",
                }
            )

    def postprocessor_hook(d: dict) -> None:
        if d.get("status") != "started":
            return
        emit(
            {
                "type": "progress",
                "phase": "postprocess",
                "postprocessor": d.get("postprocessor") or "",
                "status": d.get("status") or "",
            }
        )

    def worker() -> None:
        try:
            def stream_status(msg: str) -> None:
                emit(
                    {
                        "type": "progress",
                        "phase": "status",
                        "message": msg,
                    }
                )
                persist_runtime_status(msg)

            rel, title = _rewrite_youtube_perform_download(
                rewrite_id,
                rw,
                progress_hooks=[progress_hook],
                postprocessor_hooks=[postprocessor_hook],
                status_callback=stream_status,
            )
            result_holder["rel"] = rel
            result_holder["title"] = title
            _rewrite_youtube_set_runtime_state(
                rewrite_id,
                processing=False,
                phase="download_done",
                status="Скачивание аудио завершено.",
            )
        except Exception as e:
            result_holder["error"] = str(e)
            _rewrite_youtube_set_runtime_state(
                rewrite_id,
                processing=False,
                phase="download_error",
                status=f"Ошибка скачивания: {e}",
            )
        finally:
            event_q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        yield (
            json.dumps(
                {
                    "type": "progress",
                    "phase": "status",
                    "message": "YT-DLP скачивание: только аудио (bestaudio), скоро появится прогресс…",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        while True:
            line = event_q.get()
            if line is None:
                break
            yield line + "\n"
        err = result_holder.get("error")
        if err:
            yield json.dumps({"type": "error", "message": err}, ensure_ascii=False) + "\n"
        elif result_holder.get("rel"):
            yield (
                json.dumps(
                    {
                        "type": "done",
                        "ok": True,
                        "audio_file": result_holder["rel"],
                        "youtube_title": result_holder.get("title") or "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        else:
            yield json.dumps({"type": "error", "message": "Скачивание завершилось без результата."}, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.route("/rewrite/<rewrite_id>/youtube/transcribe", methods=["POST"])
def rewrite_youtube_transcribe(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "message": "Не задан OPENAI_API_KEY в .env"}), 400
    ap = _rewrite_youtube_resolve_audio_path(rewrite_id, rw)
    if ap is None:
        return jsonify({"ok": False, "message": "Сначала скачайте аудио."}), 400
    try:
        rw["youtube_audio_file"] = str(ap.relative_to(BASE_DIR.resolve()))
    except ValueError:
        pass
    txt, err = _rewrite_transcription_text(api_key, ap)
    if err and _transcribe_error_requires_reencode(err):
        ap2 = _transcode_audio_to_mp3_temp(ap)
        if ap2 is not None:
            txt, err = _rewrite_transcription_text(api_key, ap2)
    if err:
        return jsonify({"ok": False, "message": err}), 400
    rw["youtube_transcript_text"] = txt or ""
    rw["youtube_transcript_url"] = _youtube_url_normalize(str(rw.get("youtube_url") or ""))
    save_rewrite_job(rewrite_id, rw)
    return jsonify({"ok": True, "chars": len(rw["youtube_transcript_text"]), "words": len(rw["youtube_transcript_text"].split())})


@app.route("/rewrite/<rewrite_id>/youtube/transcribe_stream", methods=["POST"])
def rewrite_youtube_transcribe_stream(rewrite_id: str):
    """Та же расшифровка, что POST /transcribe, но NDJSON: прогресс по нарезке и чанкам OpenAI."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "message": "Не задан OPENAI_API_KEY в .env"}), 400
    ap = _rewrite_youtube_resolve_audio_path(rewrite_id, rw)
    if ap is None:
        return jsonify({"ok": False, "message": "Сначала скачайте аудио."}), 400
    try:
        rw["youtube_audio_file"] = str(ap.relative_to(BASE_DIR.resolve()))
    except ValueError:
        pass

    event_q: queue.Queue[str | None] = queue.Queue()
    result_holder: dict[str, str | None] = {}
    last_state_save = [0.0]

    _rewrite_youtube_set_runtime_state(
        rewrite_id,
        processing=True,
        phase="transcribe",
        status="Расшифровка: подготовка…",
    )

    def persist_runtime_status(msg: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - last_state_save[0] < 1.0):
            return
        last_state_save[0] = now
        _rewrite_youtube_set_runtime_state(rewrite_id, processing=True, phase="transcribe", status=msg)

    def put_progress(ev: dict) -> None:
        event_q.put(json.dumps({"type": "transcribe_progress", **ev}, ensure_ascii=False))
        msg = str(ev.get("message") or "").strip()
        if not msg and ev.get("phase") == "chunk" and ev.get("action") == "request":
            msg = f"Расшифровка: часть {ev.get('index')}/{ev.get('total')}…"
        if msg:
            persist_runtime_status(msg)

    def worker() -> None:
        try:
            txt, err = _rewrite_transcription_text(api_key, ap, progress=put_progress)
            if err and _transcribe_error_requires_reencode(err):
                put_progress({"phase": "transcode", "message": "Файл не подошёл для API, перекодируем в MP3 и повторяем…"})
                ap2 = _transcode_audio_to_mp3_temp(ap)
                if ap2 is not None:
                    txt, err = _rewrite_transcription_text(api_key, ap2, progress=put_progress)
            if err:
                result_holder["error"] = err
                _rewrite_youtube_set_runtime_state(
                    rewrite_id,
                    processing=False,
                    phase="transcribe_error",
                    status=f"Ошибка расшифровки: {err}",
                )
            else:
                result_holder["text"] = txt or ""
                rw2 = load_rewrite_job(rewrite_id)
                if rw2 is not None:
                    rw2["youtube_transcript_text"] = result_holder["text"]
                    rw2["youtube_transcript_url"] = _youtube_url_normalize(str(rw2.get("youtube_url") or ""))
                    rw2["youtube_processing"] = False
                    rw2["youtube_phase"] = "transcribe_done"
                    rw2["youtube_status"] = (
                        f"Готово текст {len(result_holder['text'])} символов · {len(result_holder['text'].split())} слов"
                    )
                    save_rewrite_job(rewrite_id, rw2)
        except Exception as e:
            result_holder["error"] = str(e)
            _rewrite_youtube_set_runtime_state(
                rewrite_id,
                processing=False,
                phase="transcribe_error",
                status=f"Ошибка расшифровки: {e}",
            )
        finally:
            event_q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            line = event_q.get()
            if line is None:
                break
            yield line + "\n"
        err = result_holder.get("error")
        if err:
            yield json.dumps({"type": "error", "message": err}, ensure_ascii=False) + "\n"
        elif result_holder.get("text") is not None:
            t = result_holder.get("text") or ""
            yield (
                json.dumps(
                    {
                        "type": "done",
                        "ok": True,
                        "chars": len(t),
                        "words": len(t.split()),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        else:
            yield json.dumps({"type": "error", "message": "Расшифровка завершилась без результата."}, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.route("/rewrite/<rewrite_id>/youtube/transcript", methods=["GET"])
def rewrite_youtube_transcript_get(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    txt = str(rw.get("youtube_transcript_text") or "")
    return jsonify({"ok": True, "text": txt})


@app.route("/rewrite/<rewrite_id>/youtube/state", methods=["GET"])
def rewrite_youtube_state_get(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    txt = str(rw.get("youtube_transcript_text") or "").strip()
    current_url = _youtube_url_normalize(str(rw.get("youtube_url") or ""))
    transcript_url = _youtube_url_normalize(str(rw.get("youtube_transcript_url") or ""))
    transcript_matches_url = bool(txt) and bool(current_url) and transcript_url == current_url
    return jsonify(
        {
            "ok": True,
            "youtube_processing": bool(rw.get("youtube_processing")),
            "youtube_phase": str(rw.get("youtube_phase") or ""),
            "youtube_status": str(rw.get("youtube_status") or ""),
            "youtube_title": str(rw.get("youtube_title") or ""),
            "youtube_url": current_url,
            "youtube_audio_ready": _rewrite_youtube_audio_exists(rw),
            "youtube_transcript_ready": transcript_matches_url,
            "transcript_chars": (len(txt) if transcript_matches_url else 0),
            "transcript_words": (len(txt.split()) if transcript_matches_url else 0),
        }
    )


def _iter_stage_run_event_strings(rewrite_id: str, body: dict[str, Any]) -> Iterator[str]:
    """Module-level generator: тот же gen(), что был внутри rewrite_project_run,
    но вынесен наружу — чтобы можно было запускать его как из обычного NDJSON-стрима,
    так и из фоновой задачи (task_manager).

    Yields NDJSON-строки (json + '\\n').
    """
    rw_job = load_rewrite_job(rewrite_id)
    if rw_job is None:
        yield json.dumps({"type": "error", "message": "not_found"}, ensure_ascii=False) + "\n"
        return
    original_title = snapshot_original_title_from_body(body, rw_job)
    stage_key = str(body.get("stage") or "").strip().lower()
    source_text, stages_snap = snapshot_stages_from_body(body)
    master_prompt = snapshot_master_prompt_from_body(body)
    hero_prompt, target_chars, duration_minutes, chars_per_minute = snapshot_pipeline_extras_from_body(body)
    preset = snapshot_rewrite_preset_from_body(body, rw_job)
    api_key = os.getenv("OPENAI_API_KEY") or ""

    # Author: в user уходит Result Distiller — подмешиваем с диска, если в снимке пусто.
    if stage_key == "author" and isinstance(stages_snap, dict):
        _snap = dict(stages_snap)
        _dist_cell = dict(_snap.get("distiller") or {}) if isinstance(_snap.get("distiller"), dict) else {}
        _dres = str(_dist_cell.get("last_result") or "").strip()
        if not _dres:
            _dp = _rewrite_stage_result_path(rewrite_id, "distiller")
            if _dp.exists():
                try:
                    _dres = _dp.read_text(encoding="utf-8")
                except OSError:
                    _dres = ""
        if _dres:
            _dist_cell["last_result"] = _dres
            _snap["distiller"] = _dist_cell
        stages_snap = _snap

    # Voiceover Editor / Title Strategist / Structure Splitter в пресете
    # «Я уже ЗАrewriteИЛ» (prewritten) все читают исходник из inbox.last_result.
    # Если в снимке inbox пришёл пустым (рестарт вкладки и т.п.), подтягиваем
    # последнее сохранённое значение из JSON проекта.
    if (
        preset == REWRITE_PRESET_PREWRITTEN
        and stage_key in ("voiceover_editor", "title_strategist", "structure_splitter")
        and isinstance(stages_snap, dict)
    ):
        _snap = dict(stages_snap)
        _ibx_cell = dict(_snap.get("inbox") or {}) if isinstance(_snap.get("inbox"), dict) else {}
        _ibx = str(_ibx_cell.get("last_result") or "").strip()
        if not _ibx:
            _ibx = str(((rw_job.get("stages") or {}).get("inbox") or {}).get("last_result") or "").strip()
        if _ibx:
            _ibx_cell["last_result"] = _ibx
            _snap["inbox"] = _ibx_cell
        stages_snap = _snap

    def gen():
        if stage_key not in REWRITE_STAGE_KEYS:
            yield json.dumps(
                {"type": "error", "message": "Неизвестный этап. Обновите страницу."},
                ensure_ascii=False,
            ) + "\n"
            return
        if stage_key not in (
            "structure",
            "retention_editor",
            "hook_editor",
            "flow_editor",
            "persona_editor",
            "voiceover_editor",
            "title_strategist",
            "structure_splitter",
            "scene_writer",
            "scene_writer_live",
            "youtube_packaging",
            "author",
        ) and not (source_text or "").strip():
            yield json.dumps(
                {"type": "error", "message": "Введите исходный текст в верхнем поле."},
                ensure_ascii=False,
            ) + "\n"
            return
        block_writer_full_text = ""
        if stage_key == "retention_editor":
            # В Мягком пресете Retention Editor читает не block_writer/full_text.txt,
            # а Result этапа Author (он играет роль склейки full_text).
            if preset == "soft":
                author_path = _rewrite_stage_result_path(rewrite_id, "author")
                if author_path.exists():
                    try:
                        block_writer_full_text = author_path.read_text(encoding="utf-8")
                    except OSError:
                        block_writer_full_text = ""
                else:
                    block_writer_full_text = str((stages_snap.get("author") or {}).get("last_result") or "")
            else:
                full_text_path = _rewrite_block_writer_dir(rewrite_id) / "full_text.txt"
                if full_text_path.exists():
                    try:
                        block_writer_full_text = full_text_path.read_text(encoding="utf-8")
                    except OSError:
                        block_writer_full_text = ""
        retention_editor_text = ""
        if stage_key == "hook_editor":
            p = _rewrite_stage_result_path(rewrite_id, "retention_editor")
            if p.exists():
                try:
                    retention_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    retention_editor_text = ""
        hook_editor_text = ""
        if stage_key == "flow_editor":
            p = _rewrite_stage_result_path(rewrite_id, "hook_editor")
            if p.exists():
                try:
                    hook_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    hook_editor_text = ""
        flow_editor_text = ""
        if stage_key == "persona_editor":
            p = _rewrite_stage_result_path(rewrite_id, "flow_editor")
            if p.exists():
                try:
                    flow_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    flow_editor_text = ""
        persona_editor_text = ""
        if stage_key == "voiceover_editor":
            p = _rewrite_stage_result_path(rewrite_id, "persona_editor")
            if p.exists():
                try:
                    persona_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    persona_editor_text = ""
        voiceover_editor_text = ""
        if stage_key in ("title_strategist", "structure_splitter"):
            voiceover_editor_text = str((stages_snap.get("voiceover_editor") or {}).get("last_result") or "")
            if not voiceover_editor_text.strip():
                p = _rewrite_stage_result_path(rewrite_id, "voiceover_editor")
                if p.exists():
                    try:
                        voiceover_editor_text = p.read_text(encoding="utf-8")
                    except OSError:
                        voiceover_editor_text = ""
            voiceover_editor_text = _extract_voiceover_plain_text(voiceover_editor_text)
        structure_splitter_text = ""
        if stage_key == "scene_writer":
            structure_splitter_text = str((stages_snap.get("structure_splitter") or {}).get("last_result") or "")
            if not structure_splitter_text.strip():
                p = _rewrite_stage_result_path(rewrite_id, "structure_splitter")
                if p.exists():
                    try:
                        structure_splitter_text = p.read_text(encoding="utf-8")
                    except OSError:
                        structure_splitter_text = ""
        title_strategist_result_text = ""
        if stage_key == "youtube_packaging":
            title_strategist_result_text = str((stages_snap.get("title_strategist") or {}).get("last_result") or "")
            if not title_strategist_result_text.strip():
                p = _rewrite_stage_result_path(rewrite_id, "title_strategist")
                if p.exists():
                    try:
                        title_strategist_result_text = p.read_text(encoding="utf-8")
                    except OSError:
                        title_strategist_result_text = ""
        scene_writer_result_text = ""
        if stage_key == "scene_writer_live":
            scene_writer_result_text = str((stages_snap.get("scene_writer") or {}).get("last_result") or "")
            if not scene_writer_result_text.strip():
                p = _rewrite_stage_result_path(rewrite_id, "scene_writer")
                if p.exists():
                    try:
                        scene_writer_result_text = p.read_text(encoding="utf-8")
                    except OSError:
                        scene_writer_result_text = ""
        payload, compose_err = compose_rewrite_openai_request_body(
            stage_key,
            source_text=source_text,
            stages_snap=stages_snap,
            master_prompt=master_prompt,
            hero_prompt=hero_prompt,
            target_chars=target_chars,
            duration_minutes=duration_minutes,
            chars_per_minute=chars_per_minute,
            block_writer_full_text=block_writer_full_text,
            retention_editor_text=retention_editor_text,
            hook_editor_text=hook_editor_text,
            flow_editor_text=flow_editor_text,
            persona_editor_text=persona_editor_text,
            voiceover_editor_text=voiceover_editor_text,
            structure_splitter_text=structure_splitter_text,
            title_strategist_result_text=title_strategist_result_text,
            scene_writer_result_text=scene_writer_result_text,
            original_title=original_title,
            preset=preset,
        )
        if compose_err:
            yield json.dumps({"type": "error", "message": compose_err}, ensure_ascii=False) + "\n"
            return
        # Достаём prompt/user_text устойчиво к двум форматам payload:
        # OpenAI: messages=[{role:system}, {role:user}]; Claude (Kie.ai): top-level
        # "system" + messages=[{role:user}]. Никогда не обращаемся по индексу,
        # чтобы не получить IndexError на Claude-формате.
        msgs_list = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        prompt = str(payload.get("system") or "")
        user_text = ""
        user_msg_obj = None
        for _m in msgs_list:
            if not isinstance(_m, dict):
                continue
            _role = str(_m.get("role") or "").lower()
            if _role == "system" and not prompt:
                prompt = str(_m.get("content") or "")
            elif _role == "user" and user_msg_obj is None:
                user_msg_obj = _m
                user_text = str(_m.get("content") or "")
        if stage_key == "title_strategist":
            user_text = apply_title_strategist_original_title_to_user_json(user_text, original_title)
            if isinstance(user_msg_obj, dict):
                user_msg_obj["content"] = user_text
        model = str(payload.get("model") or "")
        if stage_key == "draft1":
            analysis_res = str((stages_snap.get("analysis") or {}).get("last_result") or "")
            structure_res = str((stages_snap.get("structure") or {}).get("last_result") or "")
            block_writer_user_prompt = str((stages_snap.get("draft1") or {}).get("user_prompt") or "")
            bw_dir = _rewrite_block_writer_dir(rewrite_id)
            bw_dir.mkdir(parents=True, exist_ok=True)
            for old in bw_dir.glob("block_*.json"):
                old.unlink(missing_ok=True)
            (bw_dir / "all_blocks.json").unlink(missing_ok=True)
            (bw_dir / "full_text.txt").unlink(missing_ok=True)

            completed_blocks: list[dict] = []

            def on_block_completed(block_data: dict) -> None:
                idx = int(block_data.get("block_index") or 0)
                p = bw_dir / f"block_{idx:03d}.json"
                p.write_text(json.dumps(block_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                completed_blocks.append(dict(block_data))

            def on_all_completed(all_data: dict) -> None:
                blocks = all_data.get("blocks") if isinstance(all_data, dict) else []
                full_text = str((all_data or {}).get("full_text") or "")
                if isinstance(blocks, list):
                    (bw_dir / "all_blocks.json").write_text(
                        json.dumps({"blocks": blocks}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                (bw_dir / "full_text.txt").write_text(full_text, encoding="utf-8")
                if isinstance(blocks, list):
                    check_payload = _build_block_writer_check([b for b in blocks if isinstance(b, dict)])
                    rw2 = load_rewrite_job(rewrite_id)
                    if rw2 is not None:
                        st = rw2.setdefault("stages", {})
                        if isinstance(st, dict):
                            d1 = st.setdefault("draft1", {})
                            if isinstance(d1, dict):
                                d1["block_writer_check"] = check_payload
                        save_rewrite_job(rewrite_id, rw2)

            for item in iter_draft1_blockwise_completion(
                api_key,
                model,
                prompt,
                analysis_res,
                structure_res,
                hero_prompt=hero_prompt,
                block_writer_user_prompt=block_writer_user_prompt,
                on_block_completed=on_block_completed,
                on_all_completed=on_all_completed,
            ):
                yield json.dumps(item, ensure_ascii=False) + "\n"
        elif stage_key == "structure_splitter":
            split_result = ""
            for item in iter_rewrite_completion(api_key, model, prompt, user_text):
                t = str(item.get("type") or "")
                if t == "result":
                    split_result = str(item.get("content") or "")
                elif t == "error":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    return
                else:
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            yield json.dumps(
                _build_structure_splitter_check(voiceover_editor_text, split_result),
                ensure_ascii=False,
            ) + "\n"
            yield json.dumps({"type": "result", "content": split_result}, ensure_ascii=False) + "\n"
        elif stage_key == "scene_writer":
            raw_blocks = str(structure_splitter_text or "").strip()
            scene_writer_past_prompt = str(
                ((stages_snap.get("scene_writer") or {}).get("past_prompt") or "")
            ).strip()
            # Fallback: if frontend snapshot missed past_prompt (stale JS/cache),
            # take persisted value from rewrite job JSON.
            if not scene_writer_past_prompt:
                rw_saved = load_rewrite_job(rewrite_id)
                if isinstance(rw_saved, dict):
                    st_saved = rw_saved.get("stages") if isinstance(rw_saved.get("stages"), dict) else {}
                    sw_saved = st_saved.get("scene_writer") if isinstance(st_saved, dict) else {}
                    if isinstance(sw_saved, dict):
                        scene_writer_past_prompt = str(sw_saved.get("past_prompt") or "").strip()
            blocks, parse_err = _parse_structure_splitter_blocks_with_error(raw_blocks)
            if not blocks:
                human_reason = "пустой или невалидный JSON"
                if parse_err == "empty_result":
                    human_reason = "Result пустой"
                elif parse_err and parse_err.startswith("json_decode_error:"):
                    human_reason = parse_err.replace("json_decode_error:", "")
                elif parse_err == "json_is_not_list_or_blocks_object":
                    human_reason = "ожидался JSON-массив блоков или объект с полем blocks"
                yield json.dumps(
                    {
                        "type": "error",
                        "message": (
                            "Structure Splitter не вернул список блоков. "
                            f"Причина: {human_reason}."
                        ),
                    },
                    ensure_ascii=False,
                ) + "\n"
                return
            total = len(blocks)
            acc_parts: list[str] = []
            block_checks: list[dict[str, Any]] = []
            for i, block in enumerate(blocks, start=1):
                step_user = json.dumps(
                    {
                        "scene_index": i,
                        "scene_count": total,
                        "scene_block": block,
                        "notes": "Пиши только для этого блока, не пересказывай остальные.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                joined_user = f"{user_text}\n\n{step_user}"
                yield json.dumps({"type": "status", "message": f"Scene Writer: блок {i}/{total}…"}, ensure_ascii=False) + "\n"
                part = ""
                for item in iter_rewrite_completion(api_key, model, prompt, joined_user):
                    t = str(item.get("type") or "")
                    if t == "result":
                        part = str(item.get("content") or "").strip()
                    elif t == "error":
                        err = str(item.get("message") or "Ошибка Scene Writer.")
                        yield json.dumps({"type": "error", "message": f"Блок {i}/{total}: {err}"}, ensure_ascii=False) + "\n"
                        return
                    elif t == "status":
                        yield json.dumps({"type": "status", "message": f"[{i}/{total}] {str(item.get('message') or '')}"}, ensure_ascii=False) + "\n"
                part = _inject_past_prompt_into_scene_json_lines(part, scene_writer_past_prompt)
                acc_parts.append(part)
                block_checks.append(_scene_writer_block_check(block, part, i))
            full = "\n\n".join([p for p in acc_parts if p]).strip()
            # Сохраняем переносы между блоками, но scene_id делаем сквозными: scene_001..scene_N.
            scene_idx = [0]
            def _renum_scene_id(m: re.Match[str]) -> str:
                scene_idx[0] += 1
                return f'{m.group(1)}scene_{scene_idx[0]:03d}{m.group(2)}'
            full = re.sub(r'("scene_id"\s*:\s*")scene_\d+(")', _renum_scene_id, full)
            responses = len([p for p in acc_parts if str(p or "").strip()])
            total_scenes = sum(int(x.get("scenes") or 0) for x in block_checks)
            total_with_start = sum(int(x.get("with_start") or 0) for x in block_checks)
            total_with_end = sum(int(x.get("with_end") or 0) for x in block_checks)
            total_with_video = sum(int(x.get("with_video") or 0) for x in block_checks)
            total_scene_chars = sum((float(x.get("avg_scene_chars") or 0.0) * int(x.get("scenes") or 0)) for x in block_checks)
            avg_scene_chars = round((total_scene_chars / total_scenes), 1) if total_scenes else 0.0
            all_ok = (responses == total) and all(bool(x.get("ok")) for x in block_checks)
            yield json.dumps(
                {
                    "type": "scene_writer_check",
                    "summary": {
                        "blocks": total,
                        "responses": responses,
                        "ok": all_ok,
                        "scenes": total_scenes,
                        "with_start": total_with_start,
                        "with_end": total_with_end,
                        "with_video": total_with_video,
                        "avg_scene_chars": avg_scene_chars,
                    },
                    "blocks_info": block_checks,
                },
                ensure_ascii=False,
            ) + "\n"
            yield json.dumps({"type": "result", "content": full}, ensure_ascii=False) + "\n"
        elif stage_key == "scene_writer_live":
            raw_scenes = str(scene_writer_result_text or "").strip()
            scenes_in, parse_errors = parse_scene_blocks(raw_scenes)
            if not scenes_in:
                reason = "; ".join(parse_errors[:3]) if parse_errors else "пустой или невалидный Scene Writer Result"
                yield json.dumps(
                    {"type": "error", "message": f"Scene Writer Live: нет валидных сцен во входе ({reason})."},
                    ensure_ascii=False,
                ) + "\n"
                return
            swl_cell = stages_snap.get("scene_writer_live") if isinstance(stages_snap.get("scene_writer_live"), dict) else {}
            content_type = str((swl_cell or {}).get("style_prompt") or "photos").strip().lower()
            if content_type not in ("photos", "videos", "mixed"):
                content_type = "photos"
            try:
                target_percent = int(str((swl_cell or {}).get("past_prompt") or "50").strip())
            except (TypeError, ValueError):
                target_percent = 50
            target_percent = max(1, min(100, target_percent))
            batch_size = 50
            total_batches = (len(scenes_in) + batch_size - 1) // batch_size
            acc_parts: list[str] = []
            batch_checks: list[dict[str, Any]] = []
            for bi in range(total_batches):
                start = bi * batch_size
                end = min(start + batch_size, len(scenes_in))
                chunk = scenes_in[start:end]
                step_user = json.dumps(
                    {
                        "batch_index": bi + 1,
                        "batch_count": total_batches,
                        "scenes_offset_start": start + 1,
                        "scenes_offset_end": end,
                        "scenes_batch": chunk,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                joined_user = f"{user_text}\n\n{step_user}"
                yield json.dumps(
                    {"type": "status", "message": f"Scene Writer Live: batch {bi + 1}/{total_batches}…"},
                    ensure_ascii=False,
                ) + "\n"
                part = ""
                for item in iter_rewrite_completion(api_key, model, prompt, joined_user):
                    t = str(item.get("type") or "")
                    if t == "result":
                        part = str(item.get("content") or "").strip()
                    elif t == "error":
                        err = str(item.get("message") or "Ошибка Scene Writer Live.")
                        yield json.dumps(
                            {"type": "error", "message": f"Batch {bi + 1}/{total_batches}: {err}"},
                            ensure_ascii=False,
                        ) + "\n"
                        return
                    elif t == "status":
                        yield json.dumps(
                            {"type": "status", "message": f"[{bi + 1}/{total_batches}] {str(item.get('message') or '')}"},
                            ensure_ascii=False,
                        ) + "\n"
                acc_parts.append(part)
                batch_checks.append(
                    _scene_media_batch_check(
                        chunk,
                        part,
                        bi + 1,
                        content_type=content_type,
                    )
                )
            full = "\n\n".join([p for p in acc_parts if p]).strip()
            responses = len([p for p in acc_parts if str(p or "").strip()])
            in_scenes = sum(int(x.get("input_scenes") or 0) for x in batch_checks)
            out_scenes = sum(int(x.get("output_scenes") or 0) for x in batch_checks)
            with_target_content = sum(int(x.get("with_target_content") or 0) for x in batch_checks)
            avg_scene_chars = 0.0
            if out_scenes > 0:
                total_out_chars = sum(int(x.get("output_chars") or 0) for x in batch_checks)
                avg_scene_chars = round(total_out_chars / out_scenes, 1)
            all_ok = (responses == total_batches) and all(bool(x.get("ok")) for x in batch_checks)
            yield json.dumps(
                {
                    "type": "scene_writer_live_check",
                    "summary": {
                        "batches": total_batches,
                        "responses": responses,
                        "ok": all_ok,
                        "scenes_in": in_scenes,
                        "scenes_out": out_scenes,
                        "with_target_content": with_target_content,
                        "content_type": content_type,
                        "target_percent": target_percent,
                        "avg_scene_chars": avg_scene_chars,
                    },
                    "batches_info": batch_checks,
                },
                ensure_ascii=False,
            ) + "\n"
            yield json.dumps({"type": "result", "content": full}, ensure_ascii=False) + "\n"
        elif stage_key == "author":
            for item in iter_rewrite_completion_stream(api_key, model, prompt, user_text):
                t_item = str(item.get("type") or "")
                if t_item == "delta":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    continue
                if t_item == "status":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    continue
                if t_item == "error":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    return
                if t_item == "result":
                    raw = str(item.get("content") or "")
                    cleaned = strip_author_stream_end_marker(strip_markdown_code_fence(raw))
                    out_item = dict(item)
                    out_item["content"] = cleaned
                    yield json.dumps(out_item, ensure_ascii=False) + "\n"
                    return
            yield json.dumps(
                {"type": "error", "message": "Author: поток завершился без итогового result."},
                ensure_ascii=False,
            ) + "\n"
        else:
            for item in iter_rewrite_completion(api_key, model, prompt, user_text):
                t_item = str(item.get("type") or "")
                if t_item == "result" and isinstance(item.get("content"), str):
                    item = dict(item)
                    item["content"] = strip_markdown_code_fence(str(item.get("content") or ""))
                    if stage_key in (
                        "retention_editor",
                        "hook_editor",
                        "flow_editor",
                        "persona_editor",
                        "voiceover_editor",
                        "title_strategist",
                        "youtube_packaging",
                    ):
                        item["content"] = _sanitize_editor_result_json(str(item.get("content") or ""))
                yield json.dumps(item, ensure_ascii=False) + "\n"

    # Финальный фильтр: для любого result-события снимаем markdown-обёртку
    # (Claude часто оборачивает ответ в ```json … ```). Для не-result строк
    # пропускаем как есть, без overhead на лишний JSON-roundtrip.
    for _line in gen():
        try:
            _ev = json.loads(_line.rstrip("\n"))
        except (json.JSONDecodeError, ValueError, TypeError):
            yield _line
            continue
        if (
            isinstance(_ev, dict)
            and _ev.get("type") == "result"
            and isinstance(_ev.get("content"), str)
        ):
            _orig = _ev["content"]
            _stripped = strip_markdown_code_fence(_orig)
            if _stripped != _orig:
                _ev = dict(_ev)
                _ev["content"] = _stripped
                yield json.dumps(_ev, ensure_ascii=False) + "\n"
                continue
        yield _line


def _stage_run_target_factory(rewrite_id: str):
    """Factory: возвращает TaskTarget (emit, cancel_event, payload) → None для
    запуска одного этапа ReWrite в фоновой задаче (без HTTP-стрима браузеру)."""
    def target(emit, cancel_event, payload):
        cancelled_local = {"v": False}
        for line in _iter_stage_run_event_strings(rewrite_id, payload):
            if cancel_event.is_set() and not cancelled_local["v"]:
                cancelled_local["v"] = True
                emit({"type": "status", "message": "Отмена пользователем — задача будет остановлена после ближайшего ответа модели."})
            try:
                ev = json.loads(line.rstrip("\n"))
            except (json.JSONDecodeError, ValueError, TypeError):
                ev = {"type": "status", "message": str(line).strip()}
            if not isinstance(ev, dict):
                ev = {"type": "status", "message": str(ev)}
            emit(ev)
            if cancelled_local["v"]:
                # Прерываем текущий пайплайн, как только успели сообщить об отмене.
                emit({"type": "error", "message": "Задача отменена пользователем."})
                return
    return target


@app.route("/rewrite/<rewrite_id>/run", methods=["POST"])
def rewrite_project_run(rewrite_id: str):
    """Старый стрим NDJSON: оставлен для обратной совместимости (UI давно мигрирует на /run/start)."""
    rw_job = load_rewrite_job(rewrite_id)
    if rw_job is None:
        return jsonify({"error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    return Response(
        stream_with_context(_iter_stage_run_event_strings(rewrite_id, body)),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )


@app.route("/rewrite/<rewrite_id>/run/start", methods=["POST"])
def rewrite_project_run_start(rewrite_id: str):
    """Браузер-независимый запуск этапа: создаёт фоновую задачу и возвращает task_id.

    Body — тот же snapshot формы, что и у legacy /run.
    """
    rw_job = load_rewrite_job(rewrite_id)
    if rw_job is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    stage_key = str(body.get("stage") or "").strip().lower()
    if stage_key not in REWRITE_STAGE_KEYS:
        return jsonify({"ok": False, "error": "unknown_stage"}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    meta = _tm_start_task(
        proj_dir,
        kind="stage",
        ref_id=stage_key,
        target=_stage_run_target_factory(rewrite_id),
        request_payload=body,
    )
    return jsonify({"ok": True, "task": meta})


@app.route("/rewrite/<rewrite_id>/tasks/active", methods=["GET"])
def rewrite_project_tasks_active(rewrite_id: str):
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    if not proj_dir.is_dir():
        return jsonify({"ok": True, "tasks": []})
    tasks = _tm_list_active_tasks_for_project(proj_dir)
    return jsonify({"ok": True, "tasks": tasks})


@app.route("/rewrite/<rewrite_id>/tasks/<task_id>/meta", methods=["GET"])
def rewrite_project_task_meta(rewrite_id: str, task_id: str):
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    meta = _tm_get_task_meta(proj_dir, task_id)
    if not meta:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "task": meta})


@app.route("/rewrite/<rewrite_id>/tasks/<task_id>/events", methods=["GET"])
def rewrite_project_task_events(rewrite_id: str, task_id: str):
    """NDJSON-поток событий задачи: история (seq > since) + live tail.

    Query: since=<int>  (default -1, т.е. с самого начала).
    """
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    try:
        since = int(request.args.get("since", "-1"))
    except (TypeError, ValueError):
        since = -1

    def gen_events():
        for ev in _tm_subscribe_events(proj_dir, task_id, since_seq=since):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(gen_events()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )


@app.route("/rewrite/<rewrite_id>/tasks/<task_id>/cancel", methods=["POST"])
def rewrite_project_task_cancel(rewrite_id: str, task_id: str):
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    ok = _tm_cancel_task(proj_dir, task_id)
    return jsonify({"ok": bool(ok)})


@app.route("/rewrite/<rewrite_id>/api-payload", methods=["POST"])
def rewrite_project_api_payload(rewrite_id: str):
    """Скачивание JSON тела запроса к OpenAI для этапа (как при запуске ↻)."""
    rw_job = load_rewrite_job(rewrite_id)
    if rw_job is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    original_title = snapshot_original_title_from_body(body, rw_job)
    stage_key = str(body.get("stage") or "").strip().lower()
    source_text, stages_snap = snapshot_stages_from_body(body)
    master_prompt = snapshot_master_prompt_from_body(body)
    hero_prompt, target_chars, duration_minutes, chars_per_minute = snapshot_pipeline_extras_from_body(body)
    block_writer_full_text = ""
    if stage_key == "retention_editor":
        full_text_path = _rewrite_block_writer_dir(rewrite_id) / "full_text.txt"
        if full_text_path.exists():
            try:
                block_writer_full_text = full_text_path.read_text(encoding="utf-8")
            except OSError:
                block_writer_full_text = ""
    retention_editor_text = ""
    if stage_key == "hook_editor":
        p = _rewrite_stage_result_path(rewrite_id, "retention_editor")
        if p.exists():
            try:
                retention_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                retention_editor_text = ""
    hook_editor_text = ""
    if stage_key == "flow_editor":
        p = _rewrite_stage_result_path(rewrite_id, "hook_editor")
        if p.exists():
            try:
                hook_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                hook_editor_text = ""
    flow_editor_text = ""
    if stage_key == "persona_editor":
        p = _rewrite_stage_result_path(rewrite_id, "flow_editor")
        if p.exists():
            try:
                flow_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                flow_editor_text = ""
    persona_editor_text = ""
    if stage_key == "voiceover_editor":
        p = _rewrite_stage_result_path(rewrite_id, "persona_editor")
        if p.exists():
            try:
                persona_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                persona_editor_text = ""
    voiceover_editor_text = ""
    if stage_key in ("title_strategist", "structure_splitter"):
        voiceover_editor_text = str((stages_snap.get("voiceover_editor") or {}).get("last_result") or "")
        if not voiceover_editor_text.strip():
            p = _rewrite_stage_result_path(rewrite_id, "voiceover_editor")
            if p.exists():
                try:
                    voiceover_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    voiceover_editor_text = ""
        voiceover_editor_text = _extract_voiceover_plain_text(voiceover_editor_text)
    structure_splitter_text = ""
    if stage_key == "scene_writer":
        structure_splitter_text = str((stages_snap.get("structure_splitter") or {}).get("last_result") or "")
        if not structure_splitter_text.strip():
            p = _rewrite_stage_result_path(rewrite_id, "structure_splitter")
            if p.exists():
                try:
                    structure_splitter_text = p.read_text(encoding="utf-8")
                except OSError:
                    structure_splitter_text = ""
    scene_writer_result_text = ""
    if stage_key == "scene_writer_live":
        scene_writer_result_text = str((stages_snap.get("scene_writer") or {}).get("last_result") or "")
        if not scene_writer_result_text.strip():
            p = _rewrite_stage_result_path(rewrite_id, "scene_writer")
            if p.exists():
                try:
                    scene_writer_result_text = p.read_text(encoding="utf-8")
                except OSError:
                    scene_writer_result_text = ""
    title_strategist_result_text = ""
    if stage_key == "youtube_packaging":
        title_strategist_result_text = str((stages_snap.get("title_strategist") or {}).get("last_result") or "")
        if not title_strategist_result_text.strip():
            p = _rewrite_stage_result_path(rewrite_id, "title_strategist")
            if p.exists():
                try:
                    title_strategist_result_text = p.read_text(encoding="utf-8")
                except OSError:
                    title_strategist_result_text = ""
    if stage_key == "author" and isinstance(stages_snap, dict):
        _snap_ap = dict(stages_snap)
        _dist_cell_ap = dict(_snap_ap.get("distiller") or {}) if isinstance(_snap_ap.get("distiller"), dict) else {}
        _dres_ap = str(_dist_cell_ap.get("last_result") or "").strip()
        if not _dres_ap:
            _dp_ap = _rewrite_stage_result_path(rewrite_id, "distiller")
            if _dp_ap.exists():
                try:
                    _dres_ap = _dp_ap.read_text(encoding="utf-8")
                except OSError:
                    _dres_ap = ""
        if _dres_ap:
            _dist_cell_ap["last_result"] = _dres_ap
            _snap_ap["distiller"] = _dist_cell_ap
        stages_snap = _snap_ap
    preset_ap = snapshot_rewrite_preset_from_body(body, rw_job)
    # api-payload в пресете «Я уже ЗАrewriteИЛ»: то же тело, что при запуске стадии.
    # Voiceover Editor / Title Strategist / Structure Splitter все читают inbox.
    if (
        preset_ap == REWRITE_PRESET_PREWRITTEN
        and stage_key in ("voiceover_editor", "title_strategist", "structure_splitter")
        and isinstance(stages_snap, dict)
    ):
        _snap_ib = dict(stages_snap)
        _ibx_cell_ap = dict(_snap_ib.get("inbox") or {}) if isinstance(_snap_ib.get("inbox"), dict) else {}
        _ibx_ap = str(_ibx_cell_ap.get("last_result") or "").strip()
        if not _ibx_ap:
            _ibx_ap = str(((rw_job.get("stages") or {}).get("inbox") or {}).get("last_result") or "").strip()
        if _ibx_ap:
            _ibx_cell_ap["last_result"] = _ibx_ap
            _snap_ib["inbox"] = _ibx_cell_ap
        stages_snap = _snap_ib
    payload, err = compose_rewrite_openai_request_body(
        stage_key,
        source_text=source_text,
        stages_snap=stages_snap,
        master_prompt=master_prompt,
        hero_prompt=hero_prompt,
        target_chars=target_chars,
        duration_minutes=duration_minutes,
        chars_per_minute=chars_per_minute,
        block_writer_full_text=block_writer_full_text,
        retention_editor_text=retention_editor_text,
        hook_editor_text=hook_editor_text,
        flow_editor_text=flow_editor_text,
        persona_editor_text=persona_editor_text,
        voiceover_editor_text=voiceover_editor_text,
        structure_splitter_text=structure_splitter_text,
        title_strategist_result_text=title_strategist_result_text,
        scene_writer_result_text=scene_writer_result_text,
        original_title=original_title,
        preset=preset_ap,
    )
    if err:
        return jsonify({"ok": False, "message": err}), 400
    if stage_key == "title_strategist":
        _msgs_fix = payload.get("messages")
        if isinstance(_msgs_fix, list) and len(_msgs_fix) > 1:
            _uc = _msgs_fix[1].get("content") if isinstance(_msgs_fix[1], dict) else None
            if isinstance(_uc, str):
                _msgs_fix[1]["content"] = apply_title_strategist_original_title_to_user_json(
                    _uc, original_title
                )
    msgs = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    sys_c = str((msgs[0] or {}).get("content") or "") if msgs else ""
    usr_c = str((msgs[1] or {}).get("content") or "") if len(msgs) > 1 else ""
    model_m = str(payload.get("model") or "")

    if stage_key == "draft1":
        structure_raw = str((stages_snap.get("structure") or {}).get("last_result") or "").strip()
        block_writer_user_prompt = str((stages_snap.get("draft1") or {}).get("user_prompt") or "")
        saved = _load_block_writer_saved_short_summaries(rewrite_id)
        wire_bodies, ctx_exact = list_draft1_wire_chat_payloads_for_export(
            model_m,
            sys_c,
            structure_raw,
            hero_prompt,
            block_writer_user_prompt,
            saved,
        )
        hdr: list[str] = []
        if not wire_bodies:
            hdr.append(
                "[Block Writer] В Architect Result нет валидного списка blocks — тело POST не формируется."
            )
        elif not ctx_exact:
            hdr.append(
                "Ниже — те же JSON-тела, что собирает Block Writer перед POST. "
                "Контекст short_summary для следующих блоков взят из сохранённых block_*.json там, где они есть; "
                "если файлов нет или не хватает — подставлены пустые списки (так не будет при живом первом прогоне)."
            )
        txt = _format_openai_wire_payloads_txt(wire_bodies, header_lines=hdr or None)
    elif stage_key == "scene_writer":
        raw_blocks = str(structure_splitter_text or "").strip()
        blocks_sw, _parse_err_sw = _parse_structure_splitter_blocks_with_error(raw_blocks)
        wire_bodies_sw: list[dict[str, Any]] = []
        total_sw = len(blocks_sw)
        for i, block in enumerate(blocks_sw, start=1):
            step_user = json.dumps(
                {
                    "scene_index": i,
                    "scene_count": total_sw,
                    "scene_block": block,
                    "notes": "Пиши только для этого блока, не пересказывай остальные.",
                },
                ensure_ascii=False,
                indent=2,
            )
            joined_user = f"{usr_c}\n\n{step_user}"
            wire_bodies_sw.append(rewrite_chat_completion_wire_payload(model_m, sys_c, joined_user))
        if not wire_bodies_sw:
            txt = _format_openai_wire_payloads_txt(
                [],
                header_lines=["[Scene Writer] Нет блоков из Structure Splitter — POST не формируется."],
            )
        else:
            txt = _format_openai_wire_payloads_txt(wire_bodies_sw)
    else:
        txt = _format_openai_wire_payloads_txt([rewrite_chat_completion_wire_payload(model_m, sys_c, usr_c)])
    stage_export_name = stage_key
    fname = f"{rewrite_id}_{stage_export_name}_openai_request.json"
    resp = make_response(txt)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.route("/rewrite/<rewrite_id>/block-writer-check", methods=["GET"])
def rewrite_block_writer_check_get(rewrite_id: str):
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    stages = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    d1 = stages.get("draft1") if isinstance(stages.get("draft1"), dict) else {}
    check = d1.get("block_writer_check") if isinstance(d1, dict) else None
    if not isinstance(check, dict):
        completed_blocks = _load_block_writer_completed_blocks(rewrite_id)
        if completed_blocks:
            check = _build_block_writer_check(completed_blocks)
    return jsonify({"ok": True, "block_writer_check": check if isinstance(check, dict) else None})


@app.route("/rewrite-master")
def rewrite_master_legacy_redirect():
    """Старый URL → хаб проектов ReWrite."""
    return redirect(url_for("rewrite_index"), code=301)


@app.route("/reright-master")
def rewrite_reright_legacy_redirect():
    """Старый URL /reright-master → /rewrite."""
    return redirect(url_for("rewrite_index"), code=301)


@app.route("/parse", methods=["POST"])
def parse():
    # Legacy endpoint: parsing moved to /job/<id>/parse.
    flash("Парсинг сцен перенесен в страницу проекта.", "error")
    return redirect(url_for("video_index"))


@app.route("/save", methods=["POST"])
def save():
    # Legacy endpoint: save-on-create replaced by dedicated project creation on /video.
    flash("Создание проекта перенесено в верхний блок страницы Video.", "error")
    return redirect(url_for("video_index"))


@app.route("/job/<job_id>/parse", methods=["POST"])
def parse_for_job(job_id: str):
    raw_text = request.form.get("json_input", "")
    aspect_ratio = normalize_aspect_ratio(request.form.get("aspect_ratio", "16:9"), "16:9")
    resolution = request.form.get("resolution", "2K")
    image_model = normalize_image_model(request.form.get("image_model"))
    video_model = normalize_video_model(request.form.get("video_model", "veo3_fast"))
    image_template = request.form.get("image_template", "").strip()
    if image_template and not safe_template_dir(IMAGE_TEMPLATES_DIR, image_template):
        flash("Выбранный шаблон не найден в data/image_templates/.", "error")
        return redirect(url_for("job_page", job_id=job_id))

    scenes, errors = parse_scene_blocks(raw_text)
    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("job_page", job_id=job_id))

    timings_applied = False
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            flash("Проект не найден.", "error")
            return redirect(url_for("video_index"))

        # Keep existing generated media for same scene_id+slot when prompt hasn't changed.
        old_scenes = job.get("scenes", [])
        old_map: dict[tuple[str, str], dict] = {}
        if isinstance(old_scenes, list):
            for old in old_scenes:
                if not isinstance(old, dict):
                    continue
                sid = str(old.get("scene_id") or "")
                for slot in ("start", "end", "video"):
                    slot_obj = old.get(slot)
                    if isinstance(slot_obj, dict):
                        old_map[(sid, slot)] = slot_obj

        for scene in scenes:
            sid = str(scene.get("scene_id") or "")
            for slot in ("start", "end", "video"):
                new_slot = scene.get(slot) if isinstance(scene.get(slot), dict) else {"prompt": None}
                old_slot = old_map.get((sid, slot))
                if not isinstance(old_slot, dict):
                    continue
                if (new_slot.get("prompt") or "") != (old_slot.get("prompt") or ""):
                    continue
                for k in ("image_url", "video_url", "video_quality", "generation"):
                    if k in old_slot:
                        new_slot[k] = old_slot[k]
                scene[slot] = new_slot

        meta = job.get("job_meta") if isinstance(job.get("job_meta"), dict) else {}
        pm = meta.get("montage")
        prev_montage = dict(pm) if isinstance(pm, dict) and pm else None
        meta["aspect_ratio"] = aspect_ratio
        meta["video_duration"] = 10
        meta["image_model"] = image_model
        meta["image_model_label"] = image_model_label(image_model)
        meta["video_model"] = video_model
        meta["video_model_label"] = video_model_label(video_model)
        meta["resolution"] = resolution
        meta["output_format"] = "jpg"
        meta["image_template"] = image_template
        if prev_montage is not None:
            meta["montage"] = prev_montage

        job["raw_input"] = raw_text
        job["parsed_scenes"] = scenes
        job["scenes"] = scenes
        job["selected_aspect_ratio"] = aspect_ratio
        job["selected_video_duration"] = 10
        job["selected_image_model"] = image_model
        job["selected_video_model"] = video_model
        job["selected_resolution"] = resolution
        job["selected_image_template"] = image_template
        job["job_meta"] = meta
        job["status"] = "ready" if scenes else "draft"
        _apply_tts_word_timings_to_scenes(job_id, scenes)
        timings_applied = any(
            isinstance(s, dict) and isinstance(s.get("audio_timing"), dict) and (s["audio_timing"].get("badge"))
            for s in scenes
        )
        save_job(job_id, job)

    flash_ok = f"Сцены обновлены: {len(scenes)}."
    if timings_applied:
        flash_ok += " Тайминги сопоставлены с последней озвучкой (.words.json)."
    flash(flash_ok, "success")
    return redirect(url_for("job_page", job_id=job_id))


@app.route("/job/<job_id>/scenes/apply-tts-timings", methods=["POST"])
def job_scenes_apply_tts_timings(job_id: str):
    """Пересчитывает audio_timing у сцен по последнему `<stem>.words.json`.

    Используется кнопкой «Сгенерировать JSON-код сцен с таймингами»: если у
    проекта уже есть сцены и есть пословные тайминги озвучки, повторно
    добавлять сцены не нужно — этот эндпоинт берёт `.words.json`, выравнивает
    сцены и записывает audio_timing на месте.
    """
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        scenes = job.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            return jsonify({"ok": False, "error": "В проекте нет сцен."}), 400

        words_doc, audio_fname = _latest_tts_words_doc_for_job(job_id)
        if not words_doc or not audio_fname:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "У проекта нет пословных таймингов (.words.json). "
                        "Сгенерируйте озвучку моделью, которая возвращает timestamps."
                    ),
                }
            ), 400

        _apply_tts_word_timings_to_scenes(job_id, scenes)
        timings_applied = sum(
            1
            for s in scenes
            if isinstance(s, dict)
            and isinstance(s.get("audio_timing"), dict)
            and s["audio_timing"].get("start_ms") is not None
        )
        if timings_applied == 0:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Не удалось сопоставить тайминги: проверьте, что текст сцен "
                        "совпадает с озвучкой."
                    ),
                    "words_filename": audio_fname.replace(".mp3", ".words.json"),
                }
            ), 400

        save_job(job_id, job)
        rendered = _render_scenes_stripped_with_timing(scenes)

    return jsonify(
        {
            "ok": True,
            "scenes_count": len(scenes),
            "timings_applied": timings_applied,
            "scenes_stripped_text": rendered,
            "audio_filename": audio_fname,
            "words_filename": audio_fname.replace(".mp3", ".words.json"),
            "message": (
                f"Тайминги сопоставлены: {timings_applied}/{len(scenes)} сцен "
                f"(озвучка: {audio_fname})."
            ),
        }
    )


@app.route("/job/<job_id>/pexels/search", methods=["POST"])
def job_pexels_search(job_id: str):
    data = request.get_json(silent=True) or {}
    try:
        scene_idx = int(data.get("scene_index", -1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_scene_index"}), 400
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "job_not_found"}), 404
        meta = job.get("job_meta") if isinstance(job.get("job_meta"), dict) else {}
        aspect_ratio = str(meta.get("aspect_ratio") or "16:9")
        scenes = job.get("scenes")
        if not isinstance(scenes, list) or scene_idx < 0 or scene_idx >= len(scenes):
            return jsonify({"ok": False, "error": "scene_not_found"}), 404
        scene = scenes[scene_idx] if isinstance(scenes[scene_idx], dict) else None
        if not isinstance(scene, dict):
            return jsonify({"ok": False, "error": "invalid_scene"}), 400
        keywords = str(scene.get("keywords") or "").strip()
        content_type = str(scene.get("content_type") or "photos").strip().lower()
        incoming_excluded = _normalize_keyword_list(data.get("excluded_keywords"))
        if incoming_excluded:
            scene["excluded_keywords"] = incoming_excluded
            save_job(job_id, job)
            excluded_keywords = incoming_excluded
        else:
            excluded_keywords = _normalize_keyword_list(scene.get("excluded_keywords"))
    kws = _split_keywords(keywords)
    if not kws:
        return jsonify({"ok": False, "error": "Пустые keywords."}), 400
    if excluded_keywords:
        ex = {_norm_kw_key(x) for x in excluded_keywords}
        kws = [k for k in kws if _norm_kw_key(k) not in ex]
    if not kws:
        return jsonify({"ok": False, "error": "Все keywords исключены. Верните хотя бы один keyword."}), 400
    items: list[dict[str, Any]] = []
    seen_urls_local: set[str] = set()
    by_kw: dict[str, list[dict[str, Any]]] = {}
    for kw in kws:
        chunk, err = _pexels_search_assets(
            keywords=kw,
            content_type=content_type,
            target_aspect_ratio=aspect_ratio,
            per_page=20,
        )
        if err:
            continue
        uniq_chunk: list[dict[str, Any]] = []
        local_seen: set[str] = set()
        for it in chunk:
            url_key = str(it.get("media_url") or it.get("thumbnail_url") or "").strip()
            if not url_key or url_key in local_seen:
                continue
            local_seen.add(url_key)
            row = dict(it)
            row["found_by_keyword"] = kw
            uniq_chunk.append(row)
        if uniq_chunk:
            by_kw[kw] = uniq_chunk

    # 1) Round-robin: по 1 элементу с ключа для разнообразия.
    for round_idx in range(20):
        progressed = False
        for kw in kws:
            pool = by_kw.get(kw) or []
            if round_idx >= len(pool):
                continue
            row = pool[round_idx]
            url_key = str(row.get("media_url") or row.get("thumbnail_url") or "").strip()
            if not url_key or url_key in seen_urls_local:
                continue
            seen_urls_local.add(url_key)
            items.append(row)
            progressed = True
            if len(items) >= 8:
                break
        if len(items) >= 8 or not progressed:
            break

    # 2) Добор до 8 из любого оставшегося пула.
    if len(items) < 8:
        for kw in kws:
            pool = by_kw.get(kw) or []
            for row in pool:
                url_key = str(row.get("media_url") or row.get("thumbnail_url") or "").strip()
                if not url_key or url_key in seen_urls_local:
                    continue
                seen_urls_local.add(url_key)
                items.append(row)
                if len(items) >= 8:
                    break
            if len(items) >= 8:
                break
    if not items:
        return jsonify({"ok": False, "error": "По указанным keywords ничего не найдено с текущими фильтрами."}), 400
    with _job_file_lock(job_id):
        job2 = load_job(job_id)
        if job2 is None:
            return jsonify({"ok": False, "error": "job_not_found"}), 404
        scenes2 = job2.get("scenes")
        if not isinstance(scenes2, list) or scene_idx < 0 or scene_idx >= len(scenes2):
            return jsonify({"ok": False, "error": "scene_not_found"}), 404
        sc2 = scenes2[scene_idx]
        if not isinstance(sc2, dict):
            return jsonify({"ok": False, "error": "invalid_scene"}), 400
        sc2["pexels_results"] = items
        pdir = _job_pexels_dir(job_id)
        pdir.mkdir(parents=True, exist_ok=True)
        search_nonce = int(time.time() * 1000)
        saved_items: list[dict[str, Any]] = []
        for i, it in enumerate(items, start=1):
            row = dict(it or {})
            media_src = str(row.get("media_url") or "").strip()
            if media_src:
                bts = _fetch_url_bytes_capped(media_src)
                if bts:
                    ext = _media_ext_from_url(media_src, "video" if str(row.get("type") or "") == "video" else "start")
                    fname = f"s{scene_idx:03d}_{search_nonce}_{i:02d}{ext}"
                    fp = pdir / fname
                    try:
                        fp.write_bytes(bts)
                        row["local_url"] = f"/job/{job_id}/pexels/{fname}"
                    except OSError:
                        pass
            if "local_url" not in row:
                row["local_url"] = media_src
            row["search_keywords"] = keywords
            row["found_by_keyword"] = str(row.get("found_by_keyword") or "")
            saved_items.append(row)
        sc2["pexels_results"] = saved_items
        sc2["excluded_keywords"] = excluded_keywords
        sc2["pexels_selected_indices"] = []
        save_job(job_id, job2)
    return jsonify(
        {
            "ok": True,
            "items": saved_items,
            "content_type": content_type,
            "keywords": keywords,
            "excluded_keywords": excluded_keywords,
        }
    )


@app.route("/job/<job_id>/pexels/select", methods=["POST"])
def job_pexels_select(job_id: str):
    """Сохраняет выбор пользователя из Pexels-результатов сцены: до 2 индексов."""
    body = request.get_json(silent=True) or {}
    try:
        scene_index = int(body.get("scene_index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad scene_index"}), 400
    expected_id = str(body.get("scene_id") or "").strip()
    raw_indices = body.get("selected_indices")
    if not isinstance(raw_indices, list):
        raw_indices = []

    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        scenes = job.get("scenes")
        if not isinstance(scenes, list) or scene_index < 0 or scene_index >= len(scenes):
            return jsonify({"ok": False, "error": "Scene index out of range"}), 400
        scene = scenes[scene_index]
        if not isinstance(scene, dict):
            return jsonify({"ok": False, "error": "Scene is invalid"}), 400
        if expected_id and expected_id != str(scene.get("scene_id") or "").strip():
            return jsonify({"ok": False, "error": "scene_id mismatch"}), 409

        results = scene.get("pexels_results")
        n_results = len(results) if isinstance(results, list) else 0

        seen: set[int] = set()
        cleaned: list[int] = []
        for x in raw_indices:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v < 0 or v >= n_results or v in seen:
                continue
            seen.add(v)
            cleaned.append(v)
            if len(cleaned) >= 2:
                break

        scene["pexels_selected_indices"] = cleaned
        save_job(job_id, job)

    return jsonify({"ok": True, "selected_indices": cleaned})


@app.route("/job/<job_id>/generate/start", methods=["POST"])
def generate_slot_start(job_id: str):
    """Старт генерации для слота (start/end/video). Возвращает task_id."""
    data = request.get_json() or {}
    try:
        scene_idx = int(data.get("scene_index", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid scene index"}), 400
    slot = data.get("slot", "start")  # start | end | video

    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404

        scenes = job.get("scenes", [])
        if scene_idx < 0 or scene_idx >= len(scenes):
            return jsonify({"error": "Invalid scene index"}), 400

        scene = scenes[scene_idx]
        if not isinstance(scene, dict):
            return jsonify({"error": "Invalid scene"}), 400

        scene_id_key = str(scene.get("scene_id") or "").strip()
        meta = job.get("job_meta", {}) if isinstance(job.get("job_meta"), dict) else {}
        aspect_ratio = normalize_aspect_ratio(meta.get("aspect_ratio", "16:9"), "16:9")
        resolution = meta.get("resolution", "2K")
        output_format = meta.get("output_format", "jpg")
        # Выбор из выпадающих списков на странице (fetch ниже); иначе последнее сохранённое в job JSON.
        body_im = data.get("image_model")
        body_vm = data.get("video_model")
        if isinstance(body_im, str) and body_im.strip():
            image_model = normalize_image_model(body_im.strip())
        else:
            image_model = normalize_image_model(meta.get("image_model"))
        if isinstance(body_vm, str) and body_vm.strip():
            video_model = normalize_video_model(body_vm.strip())
        else:
            video_model = normalize_video_model(meta.get("video_model", "veo3_fast"))
        video_duration = int(meta.get("video_duration", 10) or 10)
        image_template_id = (meta.get("image_template") or "").strip()

        prompt = None
        if slot == "start":
            prompt = scene.get("start", {}).get("prompt")
        elif slot == "end":
            prompt = scene.get("end", {}).get("prompt")
        elif slot == "video":
            prompt = scene.get("video", {}).get("prompt")
        else:
            return jsonify({"error": "Bad slot"}), 400

        if not prompt:
            return jsonify({"error": f"No prompt for {slot}"}), 400

        video_image_urls: list[str] = []
        video_generation_type = "TEXT_2_VIDEO"
        if slot == "video":
            start_prompt_exists = bool(scene.get("start", {}).get("prompt"))
            end_prompt_exists = bool(scene.get("end", {}).get("prompt"))
            start_image_url = scene.get("start", {}).get("image_url")
            end_image_url = scene.get("end", {}).get("image_url")

            if start_prompt_exists and not start_image_url:
                return jsonify({"error": "Generate Start image first"}), 400
            if end_prompt_exists and not end_image_url:
                return jsonify({"error": "Generate End image first"}), 400

            if start_image_url:
                video_image_urls.append(start_image_url)
            if end_image_url:
                video_image_urls.append(end_image_url)

            if video_image_urls:
                video_generation_type = "FIRST_AND_LAST_FRAMES_2_VIDEO"

    kie_api_model = ""
    kie_request_path = ""

    try:
        if slot == "video":
            if video_model == "grok-imagine/image-to-video":
                task_id, kie_api_model = create_grok_image_to_video_task(
                    prompt=prompt,
                    image_urls=video_image_urls or None,
                    aspect_ratio=aspect_ratio,
                    duration_seconds=video_duration,
                    nsfw_checker=False,
                )
                kie_request_path = "/api/v1/jobs/createTask"
            else:
                task_id, kie_api_model = create_video_task(
                    prompt=prompt,
                    model=video_model,
                    aspect_ratio=aspect_ratio,
                    image_urls=video_image_urls,
                    generation_type=video_generation_type,
                )
                kie_request_path = "/api/v1/veo/generate"
        else:
            image_input_urls: list[str] = []
            tid = image_template_id
            if tid:
                td = safe_template_dir(IMAGE_TEMPLATES_DIR, tid)
                if not td:
                    return jsonify({"error": "Image template not found"}), 400
                base = public_base_url_for_kie()
                if not base:
                    return jsonify(
                        {
                            "error": "Укажите PUBLIC_BASE_URL в .env — Kie.ai должен скачать картинки шаблона по HTTP"
                        }
                    ), 500
                image_input_urls = build_image_input_urls(base, tid, td)
                if not image_input_urls:
                    return jsonify(
                        {
                            "error": "В шаблоне нет референс-изображений: добавьте 1–5 файлов .jpg/.png/.webp (logo.png не считается)"
                        }
                    ), 400
            if image_model in IMAGE_MODELS_REQUIRE_REFERENCE_URLS and not image_input_urls:
                return jsonify(
                    {
                        "error": "Эта модель (image-to-image) требует референсы: выберите шаблон с изображениями в блоке «Шаблон изображений»."
                    }
                ), 400
            task_id, kie_api_model = create_image_task(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                output_format=output_format,
                image_input=image_input_urls if image_input_urls else None,
                model=image_model,
            )
            kie_request_path = "/api/v1/jobs/createTask"
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    started_ts = datetime.now().timestamp()
    resolved_idx = scene_idx
    GENERATION_TASKS[task_id] = {
        "job_id": job_id,
        "scene_idx": resolved_idx,
        "slot": slot,
        "started_at": started_ts,
        "video_model": video_model if slot == "video" else "",
        "image_model": image_model if slot != "video" else "",
        "kie_api_model": kie_api_model,
        "kie_request_path": kie_request_path,
    }
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            GENERATION_TASKS.pop(task_id, None)
            return jsonify({"error": "Job not found"}), 404
        scenes = job.get("scenes", [])
        scene = None
        if scene_id_key:
            for i, sc in enumerate(scenes):
                if isinstance(sc, dict) and str(sc.get("scene_id") or "").strip() == scene_id_key:
                    resolved_idx = i
                    scene = sc
                    break
        if scene is None and 0 <= scene_idx < len(scenes):
            resolved_idx = scene_idx
            scene = scenes[scene_idx]
        if not isinstance(scene, dict):
            GENERATION_TASKS.pop(task_id, None)
            return jsonify({"error": "Scene list changed; retry"}), 409

        # Persist in job file to survive page reload/restart.
        scene[slot] = scene.get(slot) or {"prompt": prompt}
        # If user starts regeneration, hide previous media until new result is ready.
        scene[slot].pop("image_url", None)
        scene[slot].pop("video_url", None)
        if slot == "video":
            scene[slot].pop("video_quality", None)
        scene[slot]["generation"] = {
            "task_id": task_id,
            "state": "submitted",
            "started_at": started_ts,
            "canceled": False,
            "kie_api_model": kie_api_model,
            "kie_request_path": kie_request_path,
        }
        job["scenes"] = scenes
        meta_save = job.get("job_meta") if isinstance(job.get("job_meta"), dict) else {}
        meta_save["image_model"] = image_model
        meta_save["image_model_label"] = image_model_label(image_model)
        meta_save["video_model"] = video_model
        meta_save["video_model_label"] = video_model_label(video_model)
        job["job_meta"] = meta_save
        job["selected_image_model"] = image_model
        job["selected_video_model"] = video_model
        save_job(job_id, job)

    GENERATION_TASKS[task_id]["scene_idx"] = resolved_idx

    return jsonify({"task_id": task_id, "state": "submitted", "elapsed_seconds": 0})


@app.route("/job/<job_id>/generate/status", methods=["GET"])
def generate_slot_status(job_id: str):
    """Проверка статуса генерации по task_id."""
    task_id = request.args.get("task_id", "")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    task_meta = GENERATION_TASKS.get(task_id)
    if task_meta and task_meta.get("job_id") != job_id:
        return jsonify({"error": "task_id does not belong to this job"}), 403

    # Recover task meta from persisted job if server memory lost.
    if not task_meta:
        with _job_file_lock(job_id):
            job = load_job(job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            for idx, scene in enumerate(job.get("scenes", [])):
                for slot_name in ("start", "end", "video"):
                    slot_obj = scene.get(slot_name, {})
                    gen = slot_obj.get("generation", {}) if isinstance(slot_obj, dict) else {}
                    if gen.get("task_id") == task_id and not gen.get("canceled"):
                        meta_j = job.get("job_meta") if isinstance(job.get("job_meta"), dict) else {}
                        task_meta = {
                            "job_id": job_id,
                            "scene_idx": idx,
                            "slot": slot_name,
                            "started_at": gen.get("started_at", datetime.now().timestamp()),
                            "video_model": (
                                normalize_video_model(meta_j.get("video_model"))
                                if slot_name == "video"
                                else ""
                            ),
                            "image_model": (
                                normalize_image_model(meta_j.get("image_model"))
                                if slot_name != "video"
                                else ""
                            ),
                            "kie_api_model": (gen.get("kie_api_model") or "").strip(),
                            "kie_request_path": (gen.get("kie_request_path") or "").strip(),
                        }
                        GENERATION_TASKS[task_id] = task_meta
                        break
                if task_meta:
                    break
            if not task_meta:
                return jsonify({"error": "task_id not found"}), 404

    elapsed_seconds = int(datetime.now().timestamp() - task_meta["started_at"])

    try:
        slot_name = task_meta.get("slot", "start")
        if slot_name == "video":
            job_for_model = load_job(job_id) or {}
            vm = normalize_video_model(
                task_meta.get("video_model")
                or ((job_for_model.get("job_meta") or {}).get("video_model") or "veo3_fast")
            )
            if vm == "grok-imagine/image-to-video":
                result = get_task_result(task_id)
                task_meta["video_model"] = vm
                GENERATION_TASKS[task_id] = task_meta
            else:
                result = get_video_task_result(task_id)
                task_meta["video_model"] = vm
                GENERATION_TASKS[task_id] = task_meta
        else:
            job_for_model = load_job(job_id) or {}
            im = normalize_image_model(
                task_meta.get("image_model")
                or ((job_for_model.get("job_meta") or {}).get("image_model"))
            )
            task_meta["image_model"] = im
            GENERATION_TASKS[task_id] = task_meta
            result = get_task_result(task_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    state = result.get("state", "unknown")
    slot_nm = task_meta.get("slot", "start")
    job_for_m = load_job(job_id) or {}
    meta_j = job_for_m.get("job_meta") if isinstance(job_for_m.get("job_meta"), dict) else {}
    # Поле model из JSON тела запроса при createTask / veo generate (сохранено в задаче).
    kie_model_truth = (task_meta.get("kie_api_model") or "").strip()
    kie_path_truth = (task_meta.get("kie_request_path") or "").strip()
    if kie_model_truth:
        status_model_line = kie_model_truth
    else:
        if slot_nm == "video":
            guess = (task_meta.get("video_model") or "").strip() or normalize_video_model(
                meta_j.get("video_model")
            )
        else:
            guess = (task_meta.get("image_model") or "").strip() or normalize_image_model(
                meta_j.get("image_model")
            )
        status_model_line = f"{guess} (оценка из job_meta, задача без сохранённого kie_api_model)"
    path_suffix = f" → {kie_path_truth}" if kie_path_truth else ""
    model_suffix = f" · {status_model_line}{path_suffix}"

    slot_label = "video" if slot_nm == "video" else "image"
    state_text = {
        "waiting": f"В очереди Kie.ai (задача принята){model_suffix}",
        "queuing": f"В очереди на генерацию{model_suffix}",
        "generating": f"Генерация {slot_label}{model_suffix}",
        "success": f"Готово{model_suffix}",
        "fail": f"Ошибка генерации{model_suffix}",
    }.get(state, f"Обработка…{model_suffix}")

    # Долгое waiting — обычно перегрузка/очередь на стороне провайдера, не баг UI.
    if state in ("waiting", "queuing") and elapsed_seconds >= 180:
        state_text += (
            " · Уже долго — так бывает при загрузке у Kie. Можно Cancel и снова ↻, "
            "или подождать; другие сцены могут обработаться быстрее."
        )

    response = {
        "task_id": task_id,
        "state": state,
        "state_text": state_text,
        "elapsed_seconds": elapsed_seconds,
        "kie_api_model": kie_model_truth or None,
        "kie_request_path": kie_path_truth or None,
        "api_model_id": status_model_line,
    }

    if state == "success":
        urls = result.get("result_urls", [])
        url = urls[0] if urls else ""
        slot = task_meta["slot"]

        if slot == "video":
            vm = normalize_video_model(task_meta.get("video_model") or "veo3_fast")
            if vm == "grok-imagine/image-to-video":
                response["url"] = url
                if url:
                    with _job_file_lock(job_id):
                        job = load_job(job_id)
                        if job is not None:
                            scene_idx = task_meta["scene_idx"]
                            scenes = job.get("scenes", [])
                            if 0 <= scene_idx < len(scenes):
                                scene = scenes[scene_idx]
                                scene[slot] = scene.get(slot) or {"prompt": None}
                                scene[slot]["video_url"] = url
                                scene[slot]["video_quality"] = "720p"
                                scene[slot]["generation"] = {
                                    "task_id": task_id,
                                    "state": "success",
                                    "started_at": task_meta["started_at"],
                                    "completed_at": datetime.now().timestamp(),
                                    "canceled": False,
                                    **_kie_gen_extra(task_meta),
                                }
                                job["scenes"] = scenes
                                save_job(job_id, job)
                GENERATION_TASKS.pop(task_id, None)
            else:
                # Show base video immediately, then keep polling until 1080p is ready.
                hd_started_at = task_meta.get("hd_started_at")
                if not hd_started_at:
                    hd_started_at = datetime.now().timestamp()
                    task_meta["hd_started_at"] = hd_started_at
                    GENERATION_TASKS[task_id] = task_meta

                response["url"] = url
                response["state"] = "upgrading_1080"
                response["state_text"] = f"720p waiting 1080p{model_suffix}"
                response["hd_elapsed_seconds"] = int(datetime.now().timestamp() - hd_started_at)
                response["hd_status_text"] = f"720p waiting 1080p ({response['hd_elapsed_seconds']} sec)"

                hd_done = False
                hd_url = ""
                hd_error = ""
                try:
                    hd_result = get_video_1080p_result(task_id=task_id, index=0)
                    hd_done = bool(hd_result.get("ready") and hd_result.get("url"))
                    hd_url = hd_result.get("url", "")
                except RuntimeError as e:
                    hd_error = str(e)

                with _job_file_lock(job_id):
                    job = load_job(job_id)
                    if job is not None:
                        scene_idx = task_meta["scene_idx"]
                        scenes = job.get("scenes", [])
                        if 0 <= scene_idx < len(scenes):
                            scene = scenes[scene_idx]
                            scene[slot] = scene.get(slot) or {"prompt": None}
                            if url:
                                scene[slot]["video_url"] = url
                                scene[slot]["video_quality"] = "720p"

                            if hd_done and hd_url:
                                scene[slot]["video_url"] = hd_url
                                scene[slot]["video_quality"] = "1080p"
                                scene[slot]["generation"] = {
                                    "task_id": task_id,
                                    "state": "success",
                                    "started_at": task_meta["started_at"],
                                    "completed_at": datetime.now().timestamp(),
                                    "hd_state": "done",
                                    "hd_started_at": hd_started_at,
                                    "canceled": False,
                                    **_kie_gen_extra(task_meta),
                                }
                                response["state"] = "success"
                                response["state_text"] = f"Generation complete{model_suffix}"
                                response["url"] = hd_url
                                response["hd_status_text"] = "1080p - done"
                                response["hd_elapsed_seconds"] = int(
                                    datetime.now().timestamp() - hd_started_at
                                )
                                GENERATION_TASKS.pop(task_id, None)
                            else:
                                scene[slot]["video_quality"] = "720p"
                                scene[slot]["generation"] = {
                                    "task_id": task_id,
                                    "state": "upgrading_1080",
                                    "started_at": task_meta["started_at"],
                                    "hd_state": "waiting",
                                    "hd_started_at": hd_started_at,
                                    "hd_error": hd_error,
                                    "canceled": False,
                                    **_kie_gen_extra(task_meta),
                                }
                            job["scenes"] = scenes
                            save_job(job_id, job)
        else:
            response["url"] = url
            if url:
                with _job_file_lock(job_id):
                    job = load_job(job_id)
                    if job is not None:
                        scene_idx = task_meta["scene_idx"]
                        scenes = job.get("scenes", [])
                        if 0 <= scene_idx < len(scenes):
                            scene = scenes[scene_idx]
                            scene[slot] = scene.get(slot) or {"prompt": None}
                            scene[slot]["image_url"] = url
                            scene[slot]["generation"] = {
                                "task_id": task_id,
                                "state": "success",
                                "started_at": task_meta["started_at"],
                                "completed_at": datetime.now().timestamp(),
                                "canceled": False,
                                **_kie_gen_extra(task_meta),
                            }
                            job["scenes"] = scenes
                            save_job(job_id, job)
            GENERATION_TASKS.pop(task_id, None)
    elif state == "fail":
        response["error"] = result.get("error", "Generation failed")
        with _job_file_lock(job_id):
            job = load_job(job_id)
            if job is not None:
                scene_idx = task_meta["scene_idx"]
                slot = task_meta["slot"]
                scenes = job.get("scenes", [])
                if 0 <= scene_idx < len(scenes):
                    scene = scenes[scene_idx]
                    scene[slot] = scene.get(slot) or {"prompt": None}
                    if slot == "video":
                        scene[slot].pop("video_quality", None)
                    scene[slot]["generation"] = {
                        "task_id": task_id,
                        "state": "fail",
                        "started_at": task_meta["started_at"],
                        "completed_at": datetime.now().timestamp(),
                        "canceled": False,
                        "error": response["error"],
                        **_kie_gen_extra(task_meta),
                    }
                    job["scenes"] = scenes
                    save_job(job_id, job)
        GENERATION_TASKS.pop(task_id, None)
    else:
        # Persist progress for refresh/recovery.
        with _job_file_lock(job_id):
            job = load_job(job_id)
            if job is not None:
                scene_idx = task_meta["scene_idx"]
                slot = task_meta["slot"]
                scenes = job.get("scenes", [])
                if 0 <= scene_idx < len(scenes):
                    scene = scenes[scene_idx]
                    scene[slot] = scene.get(slot) or {"prompt": None}
                    scene[slot]["generation"] = {
                        "task_id": task_id,
                        "state": state,
                        "started_at": task_meta["started_at"],
                        "hd_state": scene[slot].get("generation", {}).get("hd_state")
                        if slot == "video"
                        else None,
                        "hd_started_at": scene[slot].get("generation", {}).get("hd_started_at")
                        if slot == "video"
                        else None,
                        "canceled": False,
                        **_kie_gen_extra(task_meta),
                    }
                    job["scenes"] = scenes
                    save_job(job_id, job)

    return jsonify(response)


@app.route("/job/<job_id>/generate/cancel", methods=["POST"])
def generate_slot_cancel(job_id: str):
    """Локальная отмена трекинга генерации по task_id."""
    data = request.get_json() or {}
    task_id = data.get("task_id", "")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    task_meta = GENERATION_TASKS.get(task_id)
    if task_meta and task_meta.get("job_id") != job_id:
        return jsonify({"error": "task_id does not belong to this job"}), 403

    # Mark canceled in persisted job.
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job:
            scenes = job.get("scenes", [])
            for scene in scenes:
                for slot_name in ("start", "end", "video"):
                    slot_obj = scene.get(slot_name, {})
                    gen = slot_obj.get("generation", {}) if isinstance(slot_obj, dict) else {}
                    if gen.get("task_id") == task_id:
                        gen["canceled"] = True
                        gen["state"] = "canceled"
                        gen["completed_at"] = datetime.now().timestamp()
                        scene[slot_name]["generation"] = gen
            job["scenes"] = scenes
            save_job(job_id, job)

    GENERATION_TASKS.pop(task_id, None)
    return jsonify({"ok": True, "message": "Tracking canceled"})


@app.route("/job/<job_id>/rename", methods=["POST"])
def rename_job(job_id: str):
    """Обновляет название проекта."""
    new_name = request.form.get("project_name", "").strip()
    if update_job_field(job_id, "project_name", new_name):
        flash("Название обновлено.", "success")
    else:
        flash("Не удалось обновить название.", "error")
    return redirect(url_for("job_page", job_id=job_id))


@app.route("/job/<job_id>/prompt/update", methods=["POST"])
def update_job_scene_prompt(job_id: str):
    """Обновляет prompt у конкретной сцены/слота (start|end|video)."""
    body = request.get_json(silent=True) or {}
    slot = str(body.get("slot") or "").strip().lower()
    if slot not in ("start", "end", "video"):
        return jsonify({"ok": False, "error": "Bad slot"}), 400

    try:
        scene_index = int(body.get("scene_index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad scene_index"}), 400

    prompt = str(body.get("prompt") or "").strip()

    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404

        scenes = job.get("scenes")
        if not isinstance(scenes, list) or scene_index < 0 or scene_index >= len(scenes):
            return jsonify({"ok": False, "error": "Scene index out of range"}), 400

        scene = scenes[scene_index]
        if not isinstance(scene, dict):
            return jsonify({"ok": False, "error": "Scene is invalid"}), 400

        slot_obj = scene.get(slot)
        if not isinstance(slot_obj, dict):
            slot_obj = {"prompt": ""}
            scene[slot] = slot_obj

        slot_obj["prompt"] = prompt
        save_job(job_id, job)
    return jsonify({"ok": True, "prompt": prompt})


@app.route("/job/<job_id>/scene/delete", methods=["POST"])
def delete_job_scene(job_id: str):
    """Удаляет одну сцену из job по индексу. Подчищает локальные файлы Pexels этой сцены."""
    body = request.get_json(silent=True) or {}
    try:
        scene_index = int(body.get("scene_index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad scene_index"}), 400
    expected_id = str(body.get("scene_id") or "").strip()

    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404

        scenes = job.get("scenes")
        if not isinstance(scenes, list) or scene_index < 0 or scene_index >= len(scenes):
            return jsonify({"ok": False, "error": "Scene index out of range"}), 400

        scene = scenes[scene_index]
        if not isinstance(scene, dict):
            return jsonify({"ok": False, "error": "Scene is invalid"}), 400

        actual_id = str(scene.get("scene_id") or "").strip()
        if expected_id and expected_id != actual_id:
            return jsonify({"ok": False, "error": "scene_id mismatch"}), 409

        scenes.pop(scene_index)
        save_job(job_id, job)

    pdir = _job_pexels_dir(job_id)
    if pdir.is_dir():
        for fp in pdir.glob(f"s{scene_index:03d}_*"):
            try:
                fp.unlink(missing_ok=True)
            except OSError:
                pass

    return jsonify({"ok": True, "scene_id": actual_id})


@app.route("/job/<job_id>/delete", methods=["POST"])
def delete_job(job_id: str):
    """Удаляет проект."""
    filepath = JOBS_DIR / f"{job_id}.json"
    if filepath.exists():
        filepath.unlink()
        audio_dir = JOB_AUDIO_DIR / job_id
        if audio_dir.is_dir():
            shutil.rmtree(audio_dir, ignore_errors=True)
        flash("Проект удалён.", "success")
    else:
        flash("Проект не найден.", "error")
    return redirect(url_for("video_index"))


@app.route("/job/<job_id>/elevenlabs/voices", methods=["GET"])
def job_elevenlabs_voices(job_id: str):
    if load_job(job_id) is None:
        return jsonify({"error": "Job not found"}), 404
    try:
        voices = elevenlabs_list_voices()
        return jsonify({"voices": voices})
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/job/<job_id>/elevenlabs/templates", methods=["GET"])
def job_elevenlabs_templates(job_id: str):
    if load_job(job_id) is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, "templates": list_elevenlabs_template_names()})


@app.route("/job/<job_id>/elevenlabs/templates/<name>", methods=["GET"])
def job_elevenlabs_template_get(job_id: str, name: str):
    if load_job(job_id) is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    data = load_elevenlabs_template(name)
    if data is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "template": data})


@app.route("/job/<job_id>/elevenlabs/templates/<name>/save", methods=["POST"])
def job_elevenlabs_template_save(job_id: str, name: str):
    if load_job(job_id) is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    body = request.get_json(silent=True) or {}
    ok, err = save_elevenlabs_template(
        name,
        {
            "model_id": body.get("model_id"),
            "voice_id": body.get("voice_id"),
            "voice_name": body.get("voice_name"),
            "speed_pct": body.get("speed_pct"),
            "stability_pct": body.get("stability_pct"),
            "similarity_pct": body.get("similarity_pct"),
            "style_pct": body.get("style_pct"),
            "use_speaker_boost": body.get("use_speaker_boost"),
        },
    )
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


@app.route("/job/<job_id>/elevenlabs/defaults", methods=["POST"])
def job_elevenlabs_defaults_save(job_id: str):
    body = request.get_json(silent=True) or {}

    def _pct(key: str, default: int) -> int:
        try:
            return max(0, min(100, int(body.get(key, default))))
        except (TypeError, ValueError):
            return default

    voice_id = str(body.get("voice_id") or "").strip()
    model_id = str(body.get("model_id") or "eleven_v3").strip() or "eleven_v3"
    voice_name = str(body.get("voice_name") or "").strip()
    tts_template = str(body.get("tts_template") or "").strip()
    raw_boost = body.get("use_speaker_boost", True)
    if isinstance(raw_boost, str):
        use_speaker_boost = raw_boost.lower() in ("true", "1", "yes", "on")
    else:
        use_speaker_boost = bool(raw_boost)

    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404

        job["tts_defaults"] = {
            "voice_id": voice_id,
            "voice_name": voice_name,
            "model_id": model_id,
            "stability_pct": _pct("stability_pct", 50),
            "similarity_pct": _pct("similarity_pct", 75),
            "style_pct": _pct("style_pct", 0),
            "speed_pct": _pct("speed_pct", 50),
            "use_speaker_boost": use_speaker_boost,
        }
        if tts_template:
            job["tts_template"] = tts_template
        save_job(job_id, job)
    return jsonify({"ok": True})


@app.route("/job/<job_id>/elevenlabs/tts", methods=["POST"])
def job_elevenlabs_tts(job_id: str):
    """Генерация озвучки ElevenLabs, файл в data/job_audio/<job_id>/."""
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    voice_id = (data.get("voice_id") or "").strip()
    model_id = (data.get("model_id") or "eleven_multilingual_v2").strip()
    voice_name = (data.get("voice_name") or "").strip() or voice_id

    if not text:
        return jsonify({"error": "Введите текст"}), 400
    if not voice_id:
        return jsonify({"error": "Выберите голос"}), 400

    with _job_file_lock(job_id):
        if load_job(job_id) is None:
            return jsonify({"error": "Job not found"}), 404

    max_c = max_chars_for_model(model_id)
    chunks = split_tts_text_into_chunks(text, max_c)
    if not chunks:
        return jsonify({"error": "Пустой текст"}), 400

    def _pct(key: str, default: float) -> float:
        try:
            return float(data.get(key, default))
        except (TypeError, ValueError):
            return default

    stability_pct = _pct("stability_pct", 50)
    similarity_pct = _pct("similarity_pct", 75)
    style_pct = _pct("style_pct", 0)
    speed_pct = _pct("speed_pct", 50)
    raw_boost = data.get("use_speaker_boost", True)
    if isinstance(raw_boost, str):
        use_speaker_boost = raw_boost.lower() in ("true", "1", "yes", "on")
    else:
        use_speaker_boost = bool(raw_boost)

    tts_kw = dict(
        voice_id=voice_id,
        model_id=model_id,
        stability_pct=stability_pct,
        similarity_pct=similarity_pct,
        style_pct=style_pct,
        speed_pct=speed_pct,
        use_speaker_boost=use_speaker_boost,
    )

    JOB_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = JOB_AUDIO_DIR / job_id
    if out_dir.is_dir():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.mp3"
    out_path = out_dir / fname

    try:
        if len(chunks) == 1:
            audio = text_to_speech_bytes(text=chunks[0], **tts_kw)
            out_path.write_bytes(audio)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                part_paths: list[Path] = []
                for i, ch in enumerate(chunks):
                    part_bytes = text_to_speech_bytes(text=ch, **tts_kw)
                    p = Path(tmp) / f"part_{i:04d}.mp3"
                    p.write_bytes(part_bytes)
                    part_paths.append(p)
                merge_mp3_files_ffmpeg(part_paths, out_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    audio_url = url_for("job_audio_file", job_id=job_id, filename=fname)
    entry = {
        "filename": fname,
        "url": audio_url,
        "created_at": datetime.now().timestamp(),
        "voice_id": voice_id,
        "voice_name": voice_name,
        "model_id": model_id,
        "chars": len(text),
        "tts_chunks": len(chunks),
        "tts_chunk_limit": max_c,
        "text_preview": text[:120] + ("…" if len(text) > 120 else ""),
        "settings": {
            "stability_pct": stability_pct,
            "similarity_pct": similarity_pct,
            "style_pct": style_pct,
            "speed_pct": speed_pct,
            "use_speaker_boost": use_speaker_boost,
        },
    }
    tts_template = str(data.get("tts_template") or "").strip()
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        job.pop("tts_outputs", None)
        if tts_template:
            job["tts_template"] = tts_template
        job["tts_defaults"] = {
            "voice_id": voice_id,
            "model_id": model_id,
            "stability_pct": stability_pct,
            "similarity_pct": similarity_pct,
            "style_pct": style_pct,
            "speed_pct": speed_pct,
            "use_speaker_boost": use_speaker_boost,
        }
        job["tts_last_text"] = text
        save_job(job_id, job)
    return jsonify({"ok": True, **entry})


@app.route("/job/<job_id>/elevenlabs/tts/stream", methods=["POST"])
def job_elevenlabs_tts_stream(job_id: str):
    """Потоковая генерация озвучки ElevenLabs с детальным прогрессом (NDJSON)."""
    data = request.get_json(silent=True) or {}

    def _ev(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @stream_with_context
    def gen():
        started = time.monotonic()

        def elapsed() -> float:
            return round(max(0.0, time.monotonic() - started), 1)

        text = (data.get("text") or "").strip()
        voice_id = (data.get("voice_id") or "").strip()
        model_id = (data.get("model_id") or "eleven_multilingual_v2").strip()
        voice_name = (data.get("voice_name") or "").strip() or voice_id

        if not text:
            yield _ev({"type": "error", "error": "Введите текст", "elapsed_seconds": elapsed()})
            return
        if not voice_id:
            yield _ev({"type": "error", "error": "Выберите голос", "elapsed_seconds": elapsed()})
            return

        with _job_file_lock(job_id):
            if load_job(job_id) is None:
                yield _ev({"type": "error", "error": "Job not found", "elapsed_seconds": elapsed()})
                return

        max_c = max_chars_for_model(model_id)
        try:
            chunks = split_tts_text_into_chunks(text, max_c)
        except RuntimeError as e:
            yield _ev({"type": "error", "error": f"Сплиттер TTS: {e}", "elapsed_seconds": elapsed()})
            return
        if not chunks:
            yield _ev({"type": "error", "error": "Пустой текст", "elapsed_seconds": elapsed()})
            return

        def _pct(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        stability_pct = _pct("stability_pct", 50)
        similarity_pct = _pct("similarity_pct", 75)
        style_pct = _pct("style_pct", 0)
        speed_pct = _pct("speed_pct", 50)
        raw_boost = data.get("use_speaker_boost", True)
        if isinstance(raw_boost, str):
            use_speaker_boost = raw_boost.lower() in ("true", "1", "yes", "on")
        else:
            use_speaker_boost = bool(raw_boost)

        tts_kw = dict(
            voice_id=voice_id,
            model_id=model_id,
            stability_pct=stability_pct,
            similarity_pct=similarity_pct,
            style_pct=style_pct,
            speed_pct=speed_pct,
            use_speaker_boost=use_speaker_boost,
        )

        total_chars = len(text)
        total_chunks = len(chunks)
        # Диагностический отпечаток чанков — позволяет в логе видеть, что куски
        # действительно разные (хэши + длины), и быстро ловить регрессии.
        try:
            import hashlib as _hashlib
            chunks_fingerprint = [
                {
                    "i": _i,
                    "len": len(_c),
                    "head": _c[:40],
                    "tail": _c[-40:],
                    "sha1_8": _hashlib.sha1(_c.encode("utf-8")).hexdigest()[:8],
                }
                for _i, _c in enumerate(chunks)
            ]
        except Exception:  # noqa: BLE001 - diagnostics-only, never block run
            chunks_fingerprint = []
        sum_chunk_chars = sum(len(_c) for _c in chunks)
        yield _ev(
            {
                "type": "status",
                "phase": "prepare",
                "message": f"Подготовлено: {total_chunks} кусков, {total_chars} символов (лимит {max_c} на запрос).",
                "total_chunks": total_chunks,
                "total_chars": total_chars,
                "sum_chunk_chars": sum_chunk_chars,
                "chunk_limit": max_c,
                "chunks_fingerprint": chunks_fingerprint,
                "elapsed_seconds": elapsed(),
            }
        )

        JOB_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        out_dir = JOB_AUDIO_DIR / job_id
        if out_dir.is_dir():
            shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.mp3"
        out_path = out_dir / fname

        # Накопитель сквозных миллисекунд для всех чанков.
        all_words: list[dict[str, Any]] = []
        cumulative_offset_ms: int = 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                part_paths: list[Path] = []
                seen_paths: set[str] = set()
                for i, ch in enumerate(chunks, start=1):
                    chunk_started = time.monotonic()
                    yield _ev(
                        {
                            "type": "status",
                            "phase": "chunk_request",
                            "chunk_index": i,
                            "total_chunks": total_chunks,
                            "chunk_chars": len(ch),
                            "message": (
                                f"[{i}/{total_chunks}] Отправили в ElevenLabs (with-timestamps): "
                                f"{len(ch)} символов. Ожидание ответа…"
                            ),
                            "elapsed_seconds": elapsed(),
                        }
                    )
                    # Один общий путь: всегда with-timestamps. Так фронт стабильно
                    # получает words.json. Если модель не поддерживает timestamps,
                    # text_to_speech_with_timestamps поднимет RuntimeError с
                    # понятным сообщением (попадёт в `type:error` ниже).
                    part_bytes, alignment = text_to_speech_with_timestamps(text=ch, **tts_kw)
                    p = Path(tmp) / f"part_{i - 1:04d}.mp3"
                    p.write_bytes(part_bytes)
                    key = str(p.resolve())
                    if key in seen_paths:
                        yield _ev(
                            {
                                "type": "error",
                                "error": f"Внутренняя ошибка: повторный part_path {p.name} для chunk {i}.",
                                "elapsed_seconds": elapsed(),
                            }
                        )
                        return
                    seen_paths.add(key)
                    part_paths.append(p)

                    # Char→word + сдвиг на накопленную длительность предыдущих чанков.
                    try:
                        chunk_words = chars_to_words_ms(
                            ch,
                            list(alignment.get("character_start_times_seconds") or []),
                            list(alignment.get("character_end_times_seconds") or []),
                            time_offset_ms=cumulative_offset_ms,
                        )
                    except RuntimeError as e:
                        yield _ev(
                            {
                                "type": "error",
                                "error": f"Тайминги [{i}/{total_chunks}]: {e}",
                                "elapsed_seconds": elapsed(),
                            }
                        )
                        return
                    all_words.extend(chunk_words)

                    # Реальная длительность MP3 (ffprobe) — единственно корректный
                    # offset для следующего чанка. character_end_times[-1] этого
                    # не даёт: между концом речи и концом MP3 у ElevenLabs обычно
                    # есть «хвост» тишины 20–80 мс.
                    chunk_duration_sec = mp3_duration_seconds_ffprobe(p)
                    cumulative_offset_ms += int(round(chunk_duration_sec * 1000))

                    yield _ev(
                        {
                            "type": "status",
                            "phase": "chunk_done",
                            "chunk_index": i,
                            "total_chunks": total_chunks,
                            "chunk_chars": len(ch),
                            "chunk_words": len(chunk_words),
                            "chunk_duration_ms": int(round(chunk_duration_sec * 1000)),
                            "cumulative_offset_ms": cumulative_offset_ms,
                            "audio_bytes": len(part_bytes),
                            "chunk_wait_seconds": round(max(0.0, time.monotonic() - chunk_started), 1),
                            "message": (
                                f"[{i}/{total_chunks}] Ответ получен: {len(part_bytes)} байт, "
                                f"{len(chunk_words)} слов, длительность {chunk_duration_sec:.2f}с."
                            ),
                            "elapsed_seconds": elapsed(),
                        }
                    )

                if total_chunks == 1:
                    shutil.copyfile(part_paths[0], out_path)
                else:
                    yield _ev(
                        {
                            "type": "status",
                            "phase": "merge_start",
                            "total_parts": len(part_paths),
                            "message": f"Склейка {total_chunks} MP3-кусков ({len(part_paths)} файлов)…",
                            "elapsed_seconds": elapsed(),
                        }
                    )
                    merge_mp3_files_ffmpeg(part_paths, out_path)
                    yield _ev(
                        {
                            "type": "status",
                            "phase": "merge_done",
                            "message": "Склейка завершена.",
                            "elapsed_seconds": elapsed(),
                        }
                    )
        except ValueError as e:
            yield _ev({"type": "error", "error": str(e), "elapsed_seconds": elapsed()})
            return
        except RuntimeError as e:
            yield _ev({"type": "error", "error": str(e), "elapsed_seconds": elapsed()})
            return

        # Итоговая длительность смерженного MP3 — для UI и sanity-check.
        total_duration_sec = mp3_duration_seconds_ffprobe(out_path) or 0.0
        total_duration_ms = int(round(total_duration_sec * 1000))

        # Сохраняем words.json рядом с MP3 (тот же базовый stem).
        words_filename = fname[:-4] + ".words.json"
        words_path = out_dir / words_filename
        words_doc = {
            "schema": "elevenlabs_with_timestamps_words@1",
            "audio_filename": fname,
            "voice_id": voice_id,
            "voice_name": voice_name,
            "model_id": model_id,
            "total_chars": len(text),
            "total_chunks": total_chunks,
            "chunk_limit": max_c,
            "total_words": len(all_words),
            "total_duration_ms": total_duration_ms,
            "words": all_words,
        }
        try:
            tmp_words = words_path.with_suffix(words_path.suffix + ".tmp")
            tmp_words.write_text(
                json.dumps(words_doc, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_words, words_path)
        except OSError as e:
            yield _ev(
                {
                    "type": "error",
                    "error": f"Не удалось сохранить words.json: {e}",
                    "elapsed_seconds": elapsed(),
                }
            )
            return

        audio_url = url_for("job_audio_file", job_id=job_id, filename=fname)
        words_url = url_for("job_audio_file", job_id=job_id, filename=words_filename)
        first_word = all_words[0] if all_words else None
        last_word = all_words[-1] if all_words else None
        entry = {
            "filename": fname,
            "url": audio_url,
            "words_filename": words_filename,
            "words_url": words_url,
            "total_words": len(all_words),
            "total_duration_ms": total_duration_ms,
            "first_word": first_word,
            "last_word": last_word,
            "created_at": datetime.now().timestamp(),
            "voice_id": voice_id,
            "voice_name": voice_name,
            "model_id": model_id,
            "chars": len(text),
            "tts_chunks": len(chunks),
            "tts_chunk_limit": max_c,
            "text_preview": text[:120] + ("…" if len(text) > 120 else ""),
            "settings": {
                "stability_pct": stability_pct,
                "similarity_pct": similarity_pct,
                "style_pct": style_pct,
                "speed_pct": speed_pct,
                "use_speaker_boost": use_speaker_boost,
            },
        }

        tts_template = str(data.get("tts_template") or "").strip()
        with _job_file_lock(job_id):
            job = load_job(job_id)
            if job is None:
                yield _ev({"type": "error", "error": "Job not found", "elapsed_seconds": elapsed()})
                return
            job.pop("tts_outputs", None)
            if tts_template:
                job["tts_template"] = tts_template
            job["tts_defaults"] = {
                "voice_id": voice_id,
                "model_id": model_id,
                "stability_pct": stability_pct,
                "similarity_pct": similarity_pct,
                "style_pct": style_pct,
                "speed_pct": speed_pct,
                "use_speaker_boost": use_speaker_boost,
            }
            job["tts_last_text"] = text
            scene_audio_timings: list[dict[str, Any]] = []
            scenes_list = job.get("scenes")
            if isinstance(scenes_list, list) and scenes_list and all_words:
                try:
                    timings = align_scenes_to_word_timings(
                        scenes_list,
                        all_words,
                        total_duration_ms=total_duration_ms,
                    )
                    merge_audio_timing_into_scenes(
                        scenes_list, timings, audio_filename=fname
                    )
                    for i, t in enumerate(timings):
                        sc_i = scenes_list[i] if i < len(scenes_list) else None
                        sid = (sc_i or {}).get("scene_id") if isinstance(sc_i, dict) else None
                        row = dict(t)
                        row["scene_id"] = sid
                        scene_audio_timings.append(row)
                except Exception as exc:  # noqa: BLE001 — не рвём успешный TTS из-за разметки
                    try:
                        app.logger.warning(  # type: ignore[attr-defined]
                            "scene audio align failed job=%s: %s", job_id, exc
                        )
                    except Exception:
                        pass
            save_job(job_id, job)

        yield _ev(
            {
                "type": "result",
                "ok": True,
                **entry,
                "scene_audio_timings": scene_audio_timings,
                "message": f"Готово: {len(chunks)} кусков, {len(text)} символов, ожидание {elapsed()}с.",
                "elapsed_seconds": elapsed(),
            }
        )

    return Response(gen(), mimetype="application/x-ndjson; charset=utf-8")


# --- ZIP «скачать всё»: фон + статус (прогресс) и прежний одношаговый GET ---
_download_all_lock = threading.Lock()
_download_all_tasks: dict[str, dict[str, Any]] = {}
_DOWNLOAD_ALL_MAX_TASKS = 48


def _render_scenes_input_text(scenes: list[dict[str, Any]]) -> str:
    """Восстанавливает «JSON-код сцен» (построчные JSON-блоки) из текущего job["scenes"].
    Учитывает удалённые на странице сцены — их в списке уже нет.
    """
    lines: list[str] = []
    for sc in scenes or []:
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("scene_id") or "").strip()
        if not sid:
            continue
        lines.append(json.dumps({"scene_id": sid}, ensure_ascii=False))
        text_val = sc.get("text")
        if text_val is not None and str(text_val) != "":
            lines.append(json.dumps({"text": text_val}, ensure_ascii=False))
        text_ru_val = sc.get("text_ru")
        if text_ru_val is not None and str(text_ru_val) != "":
            lines.append(json.dumps({"text_ru": text_ru_val}, ensure_ascii=False))
        ct = str(sc.get("content_type") or "").strip()
        if ct:
            lines.append(json.dumps({"content_type": ct}, ensure_ascii=False))
            kw = str(sc.get("keywords") or "").strip()
            if kw:
                lines.append(json.dumps({"keywords": kw}, ensure_ascii=False))
        else:
            for slot in ("start", "end", "video"):
                blk = sc.get(slot)
                if isinstance(blk, dict):
                    lines.append(
                        json.dumps({slot: {"prompt": blk.get("prompt", None)}}, ensure_ascii=False)
                    )
    return ("\n".join(lines) + "\n") if lines else ""


def _archive_plan_steps(job_id: str, job: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    audio_dir = JOB_AUDIO_DIR / job_id
    if audio_dir.is_dir():
        mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if mp3s:
            steps.append({"type": "audio", "path": mp3s[0]})
    scenes = job.get("scenes") if isinstance(job.get("scenes"), list) else []
    scenes_input_text = _render_scenes_input_text(scenes)
    if scenes_input_text:
        steps.append(
            {
                "type": "scenes_json",
                "name": "scenes.json",
                "text": scenes_input_text,
            }
        )
    pdir = _job_pexels_dir(job_id)
    for idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        stem = _archive_scene_basename(scene, idx)
        for slot in ("start", "end", "video"):
            block = scene.get(slot)
            if not isinstance(block, dict):
                continue
            key = "video_url" if slot == "video" else "image_url"
            url = str(block.get(key) or "").strip()
            if url:
                steps.append({"type": "media", "slot": slot, "stem": stem, "url": url})
        # Live-media (Pexels): кладём ВЫБРАННЫЕ элементы в подпапку Extra/.
        results = scene.get("pexels_results") if isinstance(scene.get("pexels_results"), list) else []
        sel_raw = scene.get("pexels_selected_indices") if isinstance(scene.get("pexels_selected_indices"), list) else []
        sel: list[int] = []
        for v in sel_raw:
            try:
                vi = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= vi < len(results) and vi not in sel:
                sel.append(vi)
        for pos, sel_idx in enumerate(sel, start=1):
            row = results[sel_idx]
            if not isinstance(row, dict):
                continue
            is_video = str(row.get("type") or "").strip().lower() == "video"
            local_url = str(row.get("local_url") or "").strip()
            media_url = str(row.get("media_url") or "").strip()
            local_path: Path | None = None
            prefix = f"/job/{job_id}/pexels/"
            if local_url.startswith(prefix):
                fname = local_url[len(prefix):]
                cand = (pdir / fname).resolve()
                try:
                    cand.relative_to(pdir.resolve())
                    if cand.is_file():
                        local_path = cand
                except ValueError:
                    local_path = None
            steps.append(
                {
                    "type": "pexels",
                    "stem": stem,
                    "is_video": is_video,
                    "selected_pos": pos,
                    "local_path": local_path,
                    "url": media_url,
                }
            )
    return steps


def _archive_step_label(step: dict[str, Any]) -> str:
    if step["type"] == "audio":
        return "Озвучка (MP3)"
    if step["type"] == "scenes_json":
        return f"JSON-код сцен ({step.get('name') or 'scenes.json'})"
    if step["type"] == "pexels":
        stem = str(step.get("stem") or "")
        kind = "видео" if step.get("is_video") else "фото"
        pos = int(step.get("selected_pos") or 1)
        return f"{stem} — Extra/{kind} #{pos}"
    slot = str(step.get("slot") or "")
    stem = str(step.get("stem") or "")
    if slot == "start":
        return f"{stem} — старт (изображение)"
    if slot == "end":
        return f"{stem} — финальный кадр (изображение)"
    return f"{stem} — видео"


def _download_all_is_cancelled(task_id: str) -> bool:
    with _download_all_lock:
        st = _download_all_tasks.get(task_id)
        return bool(st and st.get("cancelled"))


def _run_archive_into_zipfile(
    job_id: str,
    job: dict[str, Any],
    zf: zipfile.ZipFile,
    steps: list[dict[str, Any]] | None = None,
    report: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[int, bool]:
    steps = list(steps) if steps is not None else _archive_plan_steps(job_id, job)
    total = len(steps)
    added = 0
    images_added = 0
    videos_added = 0
    audio_added = 0
    bytes_images = 0
    bytes_videos = 0
    bytes_audio = 0

    def push(**kw: Any) -> None:
        if report is None:
            return
        payload: dict[str, Any] = {
            "total_steps": total,
            "steps_done": int(kw.get("steps_done", 0)),
            "current": str(kw.get("current", "") or ""),
            "fetch_bytes": int(kw.get("fetch_bytes", 0)),
            "images_added": images_added,
            "videos_added": videos_added,
            "audio_added": audio_added,
            "bytes_images": bytes_images,
            "bytes_videos": bytes_videos,
            "bytes_audio": bytes_audio,
            "files_added": added,
        }
        payload.update(kw)
        payload["images_added"] = images_added
        payload["videos_added"] = videos_added
        payload["audio_added"] = audio_added
        payload["bytes_images"] = bytes_images
        payload["bytes_videos"] = bytes_videos
        payload["bytes_audio"] = bytes_audio
        payload["files_added"] = added
        report(payload)

    for i, step in enumerate(steps):
        if cancel_check is not None and cancel_check():
            push(steps_done=i, current="Отмена…", fetch_bytes=0)
            return added, True
        label = _archive_step_label(step)
        push(steps_done=i, current=label, fetch_bytes=0)
        if step["type"] == "audio":
            p = step["path"]
            try:
                sz = int(p.stat().st_size)
            except OSError:
                push(steps_done=i + 1, current=f"{label} — файл недоступен", fetch_bytes=0)
                continue
            zf.write(p, arcname="voiceover.mp3")
            added += 1
            audio_added = 1
            bytes_audio += sz
            push(steps_done=i + 1, current=label, fetch_bytes=0)
            continue
        if step["type"] == "scenes_json":
            arc = str(step.get("name") or "scenes.json")
            text = str(step.get("text") or "")
            try:
                zf.writestr(arc, text.encode("utf-8"))
                added += 1
            except Exception as exc:
                push(steps_done=i + 1, current=f"{label} — ошибка записи: {exc}", fetch_bytes=0)
                continue
            push(steps_done=i + 1, current=label, fetch_bytes=0)
            continue
        if step["type"] == "pexels":
            stem = str(step.get("stem") or "")
            is_video = bool(step.get("is_video"))
            pos = int(step.get("selected_pos") or 1)
            local_path = step.get("local_path")
            url = str(step.get("url") or "")
            data: bytes = b""
            ext = ""
            if isinstance(local_path, Path) and local_path.is_file():
                try:
                    data = local_path.read_bytes()
                    ext = local_path.suffix or ""
                except OSError:
                    data = b""
            if not data and url:
                def on_prog(n: int) -> None:
                    push(steps_done=i, current=label, fetch_bytes=int(n))

                data = _fetch_url_bytes_capped(url, on_progress=on_prog, should_abort=cancel_check)
                if cancel_check is not None and cancel_check():
                    push(steps_done=i + 1, current="Отмена во время скачивания", fetch_bytes=0)
                    return added, True
                if not ext:
                    ext = _media_ext_from_url(url, "video" if is_video else "start")
            if not data:
                push(steps_done=i + 1, current=f"{label} — не удалось получить файл", fetch_bytes=0)
                continue
            if not ext:
                ext = ".mp4" if is_video else ".jpg"
            arc = f"Extra/{stem}_extra_{pos:02d}{ext}"
            zf.writestr(arc, data)
            added += 1
            ln = len(data)
            if is_video:
                videos_added += 1
                bytes_videos += ln
            else:
                images_added += 1
                bytes_images += ln
            push(steps_done=i + 1, current=label, fetch_bytes=ln)
            continue
        slot = str(step.get("slot") or "")
        url = str(step.get("url") or "")
        stem = str(step.get("stem") or "")

        def on_prog(n: int) -> None:
            push(steps_done=i, current=label, fetch_bytes=int(n))

        data = _fetch_url_bytes_capped(url, on_progress=on_prog, should_abort=cancel_check)
        if cancel_check is not None and cancel_check():
            push(steps_done=i + 1, current="Отмена во время скачивания", fetch_bytes=0)
            return added, True
        if not data:
            push(steps_done=i + 1, current=f"{label} — не удалось скачать", fetch_bytes=0)
            continue
        ext = _media_ext_from_url(url, slot)
        arc = f"{stem}_{slot}{ext}"
        zf.writestr(arc, data)
        added += 1
        ln = len(data)
        if slot == "video":
            videos_added += 1
            bytes_videos += ln
        else:
            images_added += 1
            bytes_images += ln
        push(steps_done=i + 1, current=label, fetch_bytes=ln)
    return added, False


def _download_all_prune_locked() -> None:
    if len(_download_all_tasks) <= _DOWNLOAD_ALL_MAX_TASKS:
        return
    completed = [
        (tid, float(st.get("finished_at") or 0))
        for tid, st in _download_all_tasks.items()
        if st.get("done")
    ]
    completed.sort(key=lambda x: x[1])
    drop_n = max(1, len(_download_all_tasks) - _DOWNLOAD_ALL_MAX_TASKS // 2)
    for tid, _ in completed[:drop_n]:
        st = _download_all_tasks.pop(tid, None)
        if st and st.get("zip_path"):
            try:
                Path(str(st["zip_path"])).unlink(missing_ok=True)
            except OSError:
                pass


def _download_all_worker(task_id: str, job_id: str) -> None:
    zip_local: Path | None = None
    try:
        job = load_job(job_id)
        if job is None:
            with _download_all_lock:
                st = _download_all_tasks.get(task_id)
                if st:
                    st.update(
                        done=True,
                        error="not_found",
                        message="Проект не найден.",
                        finished_at=time.time(),
                    )
            return
        plan = _archive_plan_steps(job_id, job)
        if not plan:
            with _download_all_lock:
                st = _download_all_tasks.get(task_id)
                if st:
                    st.update(
                        done=True,
                        error="empty_plan",
                        message="В архиве нечего собрать.",
                        finished_at=time.time(),
                    )
            return

        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        zip_local = Path(tmp)

        def report(patch: dict[str, Any]) -> None:
            with _download_all_lock:
                st = _download_all_tasks.get(task_id)
                if not st:
                    return
                st.update(patch)
                st["updated_at"] = time.time()

        with zipfile.ZipFile(zip_local, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            added, cancelled = _run_archive_into_zipfile(
                job_id,
                job,
                zf,
                steps=plan,
                report=report,
                cancel_check=lambda: _download_all_is_cancelled(task_id),
            )

        if cancelled:
            with _download_all_lock:
                st = _download_all_tasks.get(task_id)
                if st:
                    st.update(
                        done=True,
                        error="cancelled",
                        message="Сборка архива отменена.",
                        finished_at=time.time(),
                        zip_path=None,
                        fetch_bytes=0,
                        current="Отменено",
                    )
            zip_local.unlink(missing_ok=True)
            zip_local = None
            return

        label = str(job.get("project_name") or "").strip()
        if label:
            try:
                label.encode("ascii")
            except UnicodeEncodeError:
                label = ""
        fname_base = _safe_zip_archive_basename(label, job_id)
        fname = f"{fname_base}.zip" if not fname_base.lower().endswith(".zip") else fname_base

        with _download_all_lock:
            st = _download_all_tasks.get(task_id)
            if not st:
                zip_local.unlink(missing_ok=True)
                return
            if added == 0:
                st.update(
                    done=True,
                    error="empty_archive",
                    message="Не удалось скачать ни один файл по ссылкам.",
                    finished_at=time.time(),
                    zip_path=None,
                )
                zip_local.unlink(missing_ok=True)
                zip_local = None
                return
            try:
                zsz = int(zip_local.stat().st_size)
            except OSError:
                zsz = 0
            st.update(
                done=True,
                error=None,
                message=None,
                zip_path=str(zip_local),
                download_filename=fname,
                zip_size=zsz,
                files_added=added,
                finished_at=time.time(),
                current="Архив собран",
                steps_done=len(plan),
                fetch_bytes=0,
            )
        zip_local = None
    except Exception as e:
        if zip_local is not None:
            zip_local.unlink(missing_ok=True)
        with _download_all_lock:
            st = _download_all_tasks.get(task_id)
            if st:
                st.update(
                    done=True,
                    error="exception",
                    message=str(e)[:500],
                    finished_at=time.time(),
                    zip_path=None,
                )


def _download_all_task_view(st: dict[str, Any]) -> dict[str, Any]:
    return {
        "done": bool(st.get("done")),
        "error": st.get("error"),
        "message": st.get("message"),
        "job_id": st.get("job_id"),
        "total_steps": int(st.get("total_steps") or 0),
        "steps_done": int(st.get("steps_done") or 0),
        "current": st.get("current") or "",
        "fetch_bytes": int(st.get("fetch_bytes") or 0),
        "images_added": int(st.get("images_added") or 0),
        "videos_added": int(st.get("videos_added") or 0),
        "audio_added": int(st.get("audio_added") or 0),
        "files_added": int(st.get("files_added") or 0),
        "bytes_images": int(st.get("bytes_images") or 0),
        "bytes_videos": int(st.get("bytes_videos") or 0),
        "bytes_audio": int(st.get("bytes_audio") or 0),
        "planned_audio": int(st.get("planned_audio") or 0),
        "planned_images": int(st.get("planned_images") or 0),
        "planned_videos": int(st.get("planned_videos") or 0),
        "download_filename": st.get("download_filename"),
        "zip_size": int(st.get("zip_size") or 0),
    }


@app.route("/job/<job_id>/download-all/start", methods=["POST"])
def job_download_all_start(job_id: str):
    job = load_job(job_id)
    if job is None:
        abort(404)
    plan = _archive_plan_steps(job_id, job)
    if not plan:
        return jsonify(
            {
                "error": "empty_plan",
                "message": "В архиве нечего собрать: нет сохранённой озвучки и нет ссылок start/end/video.",
            }
        ), 400
    planned_audio = 1 if any(s.get("type") == "audio" for s in plan) else 0
    planned_images = sum(
        1
        for s in plan
        if (s.get("type") == "media" and s.get("slot") != "video")
        or (s.get("type") == "pexels" and not s.get("is_video"))
    )
    planned_videos = sum(
        1
        for s in plan
        if (s.get("type") == "media" and s.get("slot") == "video")
        or (s.get("type") == "pexels" and s.get("is_video"))
    )
    task_id = uuid.uuid4().hex
    with _download_all_lock:
        _download_all_prune_locked()
        _download_all_tasks[task_id] = {
            "job_id": job_id,
            "done": False,
            "error": None,
            "message": None,
            "created_at": time.time(),
            "updated_at": time.time(),
            "finished_at": None,
            "total_steps": len(plan),
            "steps_done": 0,
            "current": "Старт…",
            "fetch_bytes": 0,
            "images_added": 0,
            "videos_added": 0,
            "audio_added": 0,
            "files_added": 0,
            "bytes_images": 0,
            "bytes_videos": 0,
            "bytes_audio": 0,
            "planned_audio": planned_audio,
            "planned_images": planned_images,
            "planned_videos": planned_videos,
            "zip_path": None,
            "download_filename": None,
            "zip_size": 0,
            "cancelled": False,
        }
    threading.Thread(target=_download_all_worker, args=(task_id, job_id), daemon=True).start()
    return jsonify(
        {
            "task_id": task_id,
            "total_steps": len(plan),
            "planned_audio": planned_audio,
            "planned_images": planned_images,
            "planned_videos": planned_videos,
        }
    )


@app.route("/job/<job_id>/download-all/cancel", methods=["POST"])
def job_download_all_cancel(job_id: str):
    data = request.get_json(silent=True) or {}
    task_id = str(data.get("task_id") or request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"error": "missing_task_id"}), 400
    with _download_all_lock:
        st = _download_all_tasks.get(task_id)
        if not st or st.get("job_id") != job_id:
            return jsonify({"error": "unknown_task"}), 404
        if st.get("done"):
            return jsonify({"ok": True, "already_finished": True})
        st["cancelled"] = True
    return jsonify({"ok": True})


@app.route("/job/<job_id>/download-all/status")
def job_download_all_status(job_id: str):
    task_id = (request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"error": "missing_task_id"}), 400
    with _download_all_lock:
        st = _download_all_tasks.get(task_id)
    if not st or st.get("job_id") != job_id:
        return jsonify({"error": "unknown_task"}), 404
    return jsonify(_download_all_task_view(st))


@app.route("/job/<job_id>/download-all/file")
def job_download_all_file(job_id: str):
    task_id = (request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"error": "missing_task_id"}), 400
    with _download_all_lock:
        st = _download_all_tasks.get(task_id)
    if not st or st.get("job_id") != job_id:
        abort(404)
    if not st.get("done"):
        return jsonify({"error": "not_ready", "message": "Архив ещё собирается."}), 409
    if st.get("error"):
        msg = str(st.get("message") or st.get("error") or "Ошибка")
        return Response(msg, status=400, mimetype="text/plain; charset=utf-8")
    zpath = st.get("zip_path")
    if not zpath:
        return Response("Архив недоступен.", status=400, mimetype="text/plain; charset=utf-8")
    path = Path(str(zpath))
    fname = str(st.get("download_filename") or "project.zip")
    if not path.is_file():
        return Response("Временный файл архива удалён. Запустите скачивание снова.", status=410, mimetype="text/plain; charset=utf-8")

    resp = send_file(path, as_attachment=True, download_name=fname, mimetype="application/zip", max_age=0)

    @resp.call_on_close
    def _cleanup_download_all_file() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        with _download_all_lock:
            _download_all_tasks.pop(task_id, None)

    return resp


@app.route("/job/<job_id>/audio/<filename>")
def job_audio_file(job_id: str, filename: str):
    if load_job(job_id) is None:
        abort(404)
    if not _safe_job_audio_filename(filename):
        abort(404)
    d = (JOB_AUDIO_DIR / job_id).resolve()
    if not d.is_dir():
        abort(404)
    target = (d / filename).resolve()
    try:
        target.relative_to(d)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    if filename.endswith(".words.json"):
        return send_from_directory(d, filename, mimetype="application/json", max_age=0)
    return send_from_directory(d, filename, mimetype="audio/mpeg", max_age=0)


@app.route("/job/<job_id>/pexels/<filename>")
def job_pexels_file(job_id: str, filename: str):
    if load_job(job_id) is None:
        abort(404)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", filename or ""):
        abort(404)
    d = _job_pexels_dir(job_id).resolve()
    if not d.is_dir():
        abort(404)
    target = (d / filename).resolve()
    try:
        target.relative_to(d)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    resp = send_from_directory(d, filename, max_age=0)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _montage_pct_clamp(value: Any) -> int:
    try:
        x = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, x))


_MONTAGE_ZOOM_MIN = 1.0
_MONTAGE_ZOOM_MAX = 1.5
_MONTAGE_ZOOM_STEP = 0.025
_MONTAGE_ZOOM_MODES = ("alternate", "all_in", "all_out", "random")
_MONTAGE_ZOOM_MODE_DEFAULT = "alternate"
_MONTAGE_ZOOM_REF_SEC_MIN = 1.0
_MONTAGE_ZOOM_REF_SEC_MAX = 30.0
_MONTAGE_ZOOM_REF_SEC_STEP = 0.5
_MONTAGE_ZOOM_REF_SEC_DEFAULT = 5.0


def _montage_zoom_ref_seconds_clamp(value: Any) -> float:
    """Базовая длительность для «плавного зума»: 1.0…30.0 с шагом 0.5 с."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        x = _MONTAGE_ZOOM_REF_SEC_DEFAULT
    x = max(_MONTAGE_ZOOM_REF_SEC_MIN, min(_MONTAGE_ZOOM_REF_SEC_MAX, x))
    n = int(round((x - _MONTAGE_ZOOM_REF_SEC_MIN) / _MONTAGE_ZOOM_REF_SEC_STEP))
    x = _MONTAGE_ZOOM_REF_SEC_MIN + n * _MONTAGE_ZOOM_REF_SEC_STEP
    return float(round(min(x, _MONTAGE_ZOOM_REF_SEC_MAX), 1))


def _montage_bool_clamp(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value or "").strip().lower()
    return s in ("1", "true", "yes", "on")


def _montage_zoom_mode_clamp(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in _MONTAGE_ZOOM_MODES:
        return s
    return _MONTAGE_ZOOM_MODE_DEFAULT


def _montage_zoom_scale_clamp(value: Any) -> float:
    """Масштаб пика Ken Burns (1 = без эффекта, 1.5 = +50% к размеру кадра), шаг 0.025."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        x = _MONTAGE_ZOOM_MIN
    x = max(_MONTAGE_ZOOM_MIN, min(_MONTAGE_ZOOM_MAX, x))
    n = int(round((x - _MONTAGE_ZOOM_MIN) / _MONTAGE_ZOOM_STEP))
    x = _MONTAGE_ZOOM_MIN + n * _MONTAGE_ZOOM_STEP
    return float(round(min(x, _MONTAGE_ZOOM_MAX), 3))


def _montage_zoom_scale_from_request_body(data: dict[str, Any]) -> float:
    """Тело POST: предпочтительно zoom_scale; иначе legacy zoom_pct 0…100 → 1.0…1.5."""
    if data.get("zoom_scale") is not None and str(data.get("zoom_scale")).strip() != "":
        return _montage_zoom_scale_clamp(data.get("zoom_scale"))
    zp = _montage_pct_clamp(data.get("zoom_pct"))
    return _montage_zoom_scale_clamp(_MONTAGE_ZOOM_MIN + (zp / 100.0) * (_MONTAGE_ZOOM_MAX - _MONTAGE_ZOOM_MIN))


def _montage_zoom_scale_from_meta(montage: dict[str, Any]) -> float:
    """Чтение из job_meta.montage: zoom_scale или legacy zoom_pct."""
    if montage.get("zoom_scale") is not None and str(montage.get("zoom_scale")).strip() != "":
        return _montage_zoom_scale_clamp(montage.get("zoom_scale"))
    try:
        zp = int(round(float(montage.get("zoom_pct") or 0)))
    except (TypeError, ValueError):
        zp = 0
    zp = max(0, min(100, zp))
    return _montage_zoom_scale_clamp(_MONTAGE_ZOOM_MIN + (zp / 100.0) * (_MONTAGE_ZOOM_MAX - _MONTAGE_ZOOM_MIN))


JOB_REMOTION_DIR = BASE_DIR / "data" / "job_remotion"


def _job_remotion_dir(job_id: str) -> Path:
    return JOB_REMOTION_DIR / job_id


def _remotion_studio_url_from_env() -> str | None:
    """Базовый URL Remotion Studio из REMOTION_STUDIO_URL (без завершающего /)."""
    raw = (os.getenv("REMOTION_STUDIO_URL") or "").strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    return raw


def _latest_audio_path_for_job(job_id: str) -> Path | None:
    audio_dir = JOB_AUDIO_DIR / job_id
    if not audio_dir.is_dir():
        return None
    mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp3s[0] if mp3s else None


@app.route("/job/<job_id>/montage/assemble", methods=["POST"])
def job_montage_assemble(job_id: str):
    """Сохраняет настройки монтажа и стримит подготовку ассетов для Remotion (NDJSON)."""
    data = request.get_json(silent=True) or {}
    zoom_scale = _montage_zoom_scale_from_request_body(data)
    zoom_mode = _montage_zoom_mode_clamp(data.get("zoom_mode"))
    zoom_smooth = _montage_bool_clamp(data.get("zoom_smooth"))
    zoom_ref_seconds = _montage_zoom_ref_seconds_clamp(data.get("zoom_ref_seconds"))
    fade_in = _montage_pct_clamp(data.get("fade_in_pct"))

    with _job_file_lock(job_id):
        job_check = load_job(job_id)
        if job_check is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        meta = job_check.get("job_meta") if isinstance(job_check.get("job_meta"), dict) else {}
        meta["montage"] = {
            "zoom_scale": zoom_scale,
            "zoom_mode": zoom_mode,
            "zoom_smooth": zoom_smooth,
            "zoom_ref_seconds": zoom_ref_seconds,
            "fade_in_pct": fade_in,
        }
        job_check["job_meta"] = meta
        save_job(job_id, job_check)

    def _ev(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @stream_with_context
    def gen():
        started = time.monotonic()

        def elapsed() -> float:
            return round(max(0.0, time.monotonic() - started), 1)

        from job_montage_prepare import prepare_montage  # late import (избегаем циклов)

        job = load_job(job_id)
        if job is None:
            yield _ev({"type": "error", "error": "Job not found", "elapsed_seconds": elapsed()})
            return

        scenes_total = len(job.get("scenes") or [])
        if scenes_total == 0:
            yield _ev({"type": "error", "error": "В проекте нет сцен", "elapsed_seconds": elapsed()})
            return

        audio_src = _latest_audio_path_for_job(job_id)
        if audio_src is None:
            yield _ev({"type": "error", "error": "В проекте нет озвучки (MP3).", "elapsed_seconds": elapsed()})
            return

        out_dir = _job_remotion_dir(job_id)
        if out_dir.is_dir():
            shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        yield _ev(
            {
                "type": "status",
                "phase": "prepare_start",
                "message": (
                    f"Подготовка ассетов: {scenes_total} сцен, "
                    f"озвучка {audio_src.name}, каталог data/job_remotion/{job_id}/"
                ),
                "scenes": scenes_total,
                "elapsed_seconds": elapsed(),
            }
        )

        event_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        def on_progress(p: dict[str, Any]) -> None:
            event_queue.put(p)

        worker_result: dict[str, Any] = {"props": None, "error": None}

        def worker() -> None:
            try:
                props = prepare_montage(
                    job_id=job_id,
                    job=job,
                    base_dir=out_dir,
                    audio_src=audio_src,
                    pexels_dir=_job_pexels_dir(job_id),
                    fetch_url_bytes=lambda url: _fetch_url_bytes_capped(url),
                    remotion_public_dir=(BASE_DIR / "remotion" / "public" / "jobs"),
                    progress=on_progress,
                )
                worker_result["props"] = props
            except Exception as exc:  # noqa: BLE001
                worker_result["error"] = str(exc)
            finally:
                event_queue.put({"stage": "__done__"})

        th = threading.Thread(target=worker, daemon=True)
        th.start()

        while True:
            try:
                ev = event_queue.get(timeout=30)
            except queue.Empty:
                yield _ev({"type": "status", "phase": "heartbeat", "elapsed_seconds": elapsed()})
                continue
            if ev.get("stage") == "__done__":
                break
            stage = str(ev.get("stage") or "")
            payload: dict[str, Any] = {"type": "status", "phase": stage, "elapsed_seconds": elapsed()}
            payload.update({k: v for k, v in ev.items() if k not in ("stage",)})
            if stage == "audio_prepare":
                payload["message"] = (
                    f"Озвучка: из «{ev.get('source') or '?'}» → «{ev.get('target') or 'voiceover.wav'}» — "
                    f"{ev.get('detail') or 'подготовка…'}"
                )
            elif stage == "audio_fallback":
                payload["message"] = (
                    f"Озвучка: {ev.get('detail') or 'переключение на копию MP3'} "
                    f"(«{ev.get('source') or '?'}» → «{ev.get('target') or 'voiceover.mp3'}»)."
                )
            elif stage == "audio_done":
                src = ev.get("source") or "—"
                fn = ev.get("filename") or "—"
                fmt = str(ev.get("format") or "").upper() or "—"
                ms = int(ev.get("duration_ms") or 0)
                if ev.get("transcoded"):
                    payload["message"] = (
                        f"Озвучка готова: «{src}» → «{fn}» ({fmt}, {ms} мс по ffprobe)."
                    )
                else:
                    payload["message"] = (
                        f"Озвучка записана (копия без перекодирования): «{src}» → «{fn}» ({fmt}, {ms} мс). "
                        f"В Studio волна на таймлайне может не отрисоваться."
                    )
            elif stage == "audio_missing":
                payload["message"] = (
                    f"Озвучка: {ev.get('detail') or 'файл не найден или запись не удалась'} "
                    f"(источник: {ev.get('source') or '—'})."
                )
            elif stage == "public_link_fail":
                payload["message"] = f"Симлинк public/jobs: {ev.get('error') or 'ошибка'}"
            elif stage == "scene_start":
                payload["message"] = (
                    f"[{int(ev.get('index') or 0) + 1}/{int(ev.get('total') or 0)}] "
                    f"{ev.get('scene_id') or ev.get('stem')}: {ev.get('source') or '—'} ({ev.get('kind') or '—'})"
                )
            elif stage == "scene_done":
                payload["message"] = (
                    f"[{int(ev.get('index') or 0) + 1}/{int(ev.get('total') or 0)}] "
                    f"готово: {ev.get('kind') or '—'}"
                )
            elif stage == "scene_fail":
                payload["message"] = (
                    f"[{int(ev.get('index') or 0) + 1}/{int(ev.get('total') or 0)}] "
                    f"медиа недоступно ({ev.get('reason') or 'unknown'})"
                )
            elif stage == "props_written":
                payload["message"] = f"props.json записан ({int(ev.get('scenes') or 0)} сцен)."
            yield _ev(payload)

        if worker_result["error"]:
            yield _ev({"type": "error", "error": worker_result["error"], "elapsed_seconds": elapsed()})
            return

        props = worker_result["props"] or {}
        scenes_with_media = sum(1 for sc in (props.get("scenes") or []) if isinstance(sc.get("media"), dict) and sc["media"].get("src"))
        yield _ev(
            {
                "type": "result",
                "ok": True,
                "message": (
                    f"Готово. Сцен с медиа: {scenes_with_media}/{len(props.get('scenes') or [])}, "
                    f"длительность {int(props.get('total_duration_ms') or 0)} мс."
                ),
                "props_url": url_for("job_montage_file", job_id=job_id, filename="props.json"),
                "studio_url": url_for("job_montage_open_studio", job_id=job_id),
                "remotion_open_url": url_for("job_montage_remotion_open", job_id=job_id),
                "remotion_studio_configured": _remotion_studio_url_from_env() is not None,
                "render_url": url_for("job_montage_render", job_id=job_id),
                "total_duration_ms": int(props.get("total_duration_ms") or 0),
                "scenes_total": len(props.get("scenes") or []),
                "scenes_with_media": scenes_with_media,
                "elapsed_seconds": elapsed(),
            }
        )

    return Response(gen(), mimetype="application/x-ndjson; charset=utf-8")


_SAFE_MONTAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.(json|mp3|wav|mp4|webm|png|jpg|jpeg|webp|gif|mov)$")


@app.route("/job/<job_id>/montage/file/<path:filename>")
def job_montage_file(job_id: str, filename: str):
    if load_job(job_id) is None:
        abort(404)
    d = _job_remotion_dir(job_id).resolve()
    if not d.is_dir():
        abort(404)
    parts = filename.split("/")
    if any(p in ("", "..", ".") for p in parts):
        abort(404)
    if len(parts) == 2 and parts[0] == "media":
        leaf = parts[1]
    elif len(parts) == 1:
        leaf = parts[0]
    else:
        abort(404)
    if not _SAFE_MONTAGE_NAME_RE.match(leaf):
        abort(404)
    target = (d / filename).resolve()
    try:
        target.relative_to(d)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    if leaf.endswith(".json"):
        return send_from_directory(d, filename, mimetype="application/json", max_age=0)
    # медиа сцен/озвучка/готовый mp4 — содержимое не меняется (cachebusting через имя файла сцены и пересборку каталога),
    # отдаём с долгоживущим immutable, чтобы браузер не перепрашивал каждый сик в превью
    return send_from_directory(d, filename, max_age=60 * 60 * 24 * 30)


@app.route("/job/<job_id>/montage/studio")
def job_montage_open_studio(job_id: str):
    """JSON-заглушка для API; для браузера используйте /montage/remotion-open."""
    return jsonify(
        {
            "ok": False,
            "error": "studio_not_running",
            "message": (
                "Remotion Studio запускается вручную из каталога remotion/ "
                "(см. REMOTION_STUDIO_URL в .env). Для открытия из интерфейса проекта "
                "используйте ссылку «Открыть в Remotion» на странице job."
            ),
        }
    )


@app.route("/job/<job_id>/montage/remotion-open")
def job_montage_remotion_open(job_id: str):
    """Открытие Studio в новой вкладке: редирект на REMOTION_STUDIO_URL или подсказка."""
    if load_job(job_id) is None:
        abort(404)
    props_path = _job_remotion_dir(job_id) / "props.json"
    if not props_path.is_file():
        abort(400)
    ext = _remotion_studio_url_from_env()
    if ext:
        studio_page = urljoin(ext.rstrip("/") + "/", "JobMontage")
        target = f"{studio_page}?{urlencode({'job': job_id})}"
        return redirect(target, code=302)

    jid = html_escape(job_id)
    body = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Remotion Studio</title></head>
<body style="font-family:system-ui,sans-serif;max-width:42rem;margin:2rem;line-height:1.5">
<h1>Remotion Studio не настроен</h1>
<p>Для кнопки «Открыть в Remotion» в <code>.env</code> задайте переменную
<code>REMOTION_STUDIO_URL</code> — полный URL, с которого в браузере открывается Studio
(например <code>http://127.0.0.1:3000</code> или <code>http://&lt;IP&gt;:3333</code> при
<code>npx remotion studio --host 0.0.0.0 --port 3333</code>).</p>
<p>Проект <code>{jid}</code>: ассеты в <code>data/job_remotion/{jid}/</code> и
<code>remotion/public/jobs/{jid}/</code>. После настройки <code>REMOTION_STUDIO_URL</code>
кнопка «Открыть в Remotion» ведёт на
<code>…/JobMontage?job={jid}</code> — Studio подгружает <code>props.json</code> автоматически.</p>
</body></html>"""
    return Response(body, mimetype="text/html; charset=utf-8")


_REMOTION_DIR = BASE_DIR / "remotion"
_REMOTION_NPX = shutil.which("npx") or "/usr/bin/npx"
_REMOTION_NODE = shutil.which("node") or "/usr/bin/node"


_montage_render_lock = threading.Lock()
_montage_render_tasks: dict[str, dict[str, Any]] = {}


def _montage_render_view(st: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": st.get("task_id"),
        "job_id": st.get("job_id"),
        "state": st.get("state"),
        "progress_pct": int(st.get("progress_pct") or 0),
        "stage": st.get("stage"),
        "message": st.get("message"),
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "error": st.get("error"),
        "output_url": st.get("output_url"),
        "output_filename": st.get("output_filename"),
    }


def _montage_render_worker(task_id: str, job_id: str) -> None:
    props_path = _job_remotion_dir(job_id) / "props.json"
    out_path = _job_remotion_dir(job_id) / "out.mp4"
    if not props_path.is_file():
        with _montage_render_lock:
            st = _montage_render_tasks.get(task_id)
            if st:
                st.update(state="error", error="no_props", message="props.json не найден", finished_at=time.time())
        return
    if out_path.exists():
        try:
            out_path.unlink()
        except OSError:
            pass

    env = dict(os.environ)
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")

    cmd = [
        _REMOTION_NPX,
        "--no",
        "remotion",
        "render",
        "src/index.ts",
        "JobMontage",
        str(out_path),
        f"--props={props_path.resolve()}",
        "--log=info",
    ]

    with _montage_render_lock:
        st = _montage_render_tasks.get(task_id)
        if st:
            st.update(state="running", stage="bundle", message="Запуск Remotion render…")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_REMOTION_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
    except OSError as exc:
        with _montage_render_lock:
            st = _montage_render_tasks.get(task_id)
            if st:
                st.update(
                    state="error",
                    error="spawn_failed",
                    message=f"Не удалось запустить npx remotion: {exc}",
                    finished_at=time.time(),
                )
        return

    pct_re = re.compile(r"(\d{1,3})\s*%")
    last_line = ""
    with _montage_render_lock:
        st = _montage_render_tasks.get(task_id)
        if st:
            st["pid"] = proc.pid

    if proc.stdout is not None:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            last_line = line
            stage = None
            pct = None
            lo = line.lower()
            if "bundl" in lo:
                stage = "bundle"
            elif "rendering frames" in lo or "rendering" in lo:
                stage = "rendering"
            elif "encoding" in lo:
                stage = "encoding"
            elif "muxing" in lo or "stitching" in lo:
                stage = "muxing"
            elif "done" in lo and "ms" in lo:
                stage = "done"
            m = pct_re.search(line)
            if m:
                try:
                    p = int(m.group(1))
                    if 0 <= p <= 100:
                        pct = p
                except ValueError:
                    pct = None
            with _montage_render_lock:
                st = _montage_render_tasks.get(task_id)
                if st:
                    if stage:
                        st["stage"] = stage
                    if pct is not None:
                        st["progress_pct"] = pct
                    st["message"] = line[:240]

    rc = proc.wait()

    with _montage_render_lock:
        st = _montage_render_tasks.get(task_id)
        if not st:
            return
        cancelled = bool(st.get("cancel_requested"))
        if cancelled:
            st.update(
                state="cancelled",
                stage="cancelled",
                error="cancelled",
                message="Рендер остановлен пользователем.",
                finished_at=time.time(),
            )
        elif rc == 0 and out_path.is_file():
            st.update(
                state="done",
                progress_pct=100,
                stage="done",
                message="Рендер MP4 завершён.",
                finished_at=time.time(),
                output_filename="out.mp4",
                output_url=url_for("job_montage_file", job_id=job_id, filename="out.mp4"),
            )
        else:
            st.update(
                state="error",
                stage=st.get("stage") or "error",
                error=f"exit_code={rc}",
                message=("Ошибка рендера: " + (last_line[:240] if last_line else "")) or "Ошибка рендера",
                finished_at=time.time(),
            )


@app.route("/job/<job_id>/montage/render", methods=["POST"])
def job_montage_render(job_id: str):
    if load_job(job_id) is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    props_path = _job_remotion_dir(job_id) / "props.json"
    if not props_path.is_file():
        return jsonify({"ok": False, "error": "no_props", "message": "Сначала запустите подготовку («Смонтировать видео»)."}), 400

    with _montage_render_lock:
        for tid, st in _montage_render_tasks.items():
            if st.get("job_id") == job_id and st.get("state") in ("queued", "running"):
                return jsonify({"ok": True, **_montage_render_view(st)})
        task_id = uuid.uuid4().hex
        _montage_render_tasks[task_id] = {
            "task_id": task_id,
            "job_id": job_id,
            "state": "queued",
            "progress_pct": 0,
            "stage": "queued",
            "message": "Поставлено в очередь",
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "output_url": None,
            "output_filename": None,
            "pid": None,
        }
        st = _montage_render_tasks[task_id]

    threading.Thread(target=_montage_render_worker, args=(task_id, job_id), daemon=True).start()
    return jsonify({"ok": True, **_montage_render_view(st)})


@app.route("/job/<job_id>/montage/render/status")
def job_montage_render_status(job_id: str):
    task_id = (request.args.get("task_id") or "").strip()
    with _montage_render_lock:
        st = _montage_render_tasks.get(task_id)
        if not st or st.get("job_id") != job_id:
            for tid, candidate in _montage_render_tasks.items():
                if candidate.get("job_id") == job_id and (
                    candidate.get("state") in ("queued", "running") or not task_id
                ):
                    st = candidate
                    break
        if not st:
            # Активной/недавней задачи нет, но MP4 уже мог быть отрендерен ранее
            # (сервис мог быть перезапущен — память _montage_render_tasks теряется).
            out_path = _job_remotion_dir(job_id) / "out.mp4"
            if out_path.is_file():
                return jsonify({
                    "ok": True,
                    "task_id": None,
                    "job_id": job_id,
                    "state": "done",
                    "progress_pct": 100,
                    "stage": "done",
                    "message": "MP4 уже готов.",
                    "started_at": None,
                    "finished_at": out_path.stat().st_mtime,
                    "error": None,
                    "output_url": url_for("job_montage_file", job_id=job_id, filename="out.mp4"),
                    "output_filename": "out.mp4",
                })
            return jsonify({"ok": False, "error": "not_found"}), 404
        view = _montage_render_view(st)
    return jsonify({"ok": True, **view})


@app.route("/job/<job_id>/montage/render/cancel", methods=["POST"])
def job_montage_render_cancel(job_id: str):
    """Останавливает активный рендер Remotion для job_id (kill -TERM по PID)."""
    if load_job(job_id) is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    task_id = (request.args.get("task_id") or "").strip()
    target = None
    with _montage_render_lock:
        if task_id:
            cand = _montage_render_tasks.get(task_id)
            if cand and cand.get("job_id") == job_id:
                target = cand
        if target is None:
            for _tid, cand in _montage_render_tasks.items():
                if cand.get("job_id") == job_id and cand.get("state") in ("queued", "running"):
                    target = cand
                    break
        if target is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        pid = target.get("pid")
        target["cancel_requested"] = True
        target["message"] = "Остановка по запросу пользователя…"

    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError, OSError) as exc:
            with _montage_render_lock:
                target["message"] = f"Не удалось остановить процесс: {exc}"
            return jsonify({"ok": False, "error": str(exc)}), 500

    with _montage_render_lock:
        return jsonify({"ok": True, **_montage_render_view(target)})


@app.route("/job/<job_id>/download-all")
def job_download_all(job_id: str):
    """ZIP: озвучка (если есть) + готовые start/end/video по сценам (имена scene_175_start.ext …). Одним запросом (без прогресса)."""
    job = load_job(job_id)
    if job is None:
        abort(404)
    plan = _archive_plan_steps(job_id, job)
    if not plan:
        return Response(
            "В архиве нечего собрать: нет сохранённой озвучки и нет доступных по ссылкам start/end/video.",
            status=400,
            mimetype="text/plain; charset=utf-8",
        )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        added, _cancelled = _run_archive_into_zipfile(job_id, job, zf, steps=plan, report=None)
    if added == 0:
        return Response(
            "В архиве нечего собрать: не удалось скачать ни один файл по ссылкам.",
            status=400,
            mimetype="text/plain; charset=utf-8",
        )
    buf.seek(0)
    raw = buf.getvalue()
    label = str(job.get("project_name") or "").strip()
    if label:
        try:
            label.encode("ascii")
        except UnicodeEncodeError:
            label = ""
    fname_base = _safe_zip_archive_basename(label, job_id)
    fname = f"{fname_base}.zip" if not fname_base.lower().endswith(".zip") else fname_base
    resp = make_response(raw)
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.route("/job/<job_id>")
def job_page(job_id: str):
    """Страница проекта — генерация изображений и видео."""
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            flash("Проект не найден.", "error")
            return redirect(url_for("video_index"))

        # Совместимость со старыми job без project_name, job_meta
        job.setdefault("project_name", "")
        if "job_meta" not in job:
            job["job_meta"] = {}
        meta = job["job_meta"]
        meta.setdefault("aspect_ratio", job.get("selected_aspect_ratio", "16:9"))
        meta.setdefault("video_duration", job.get("selected_video_duration", 10))
        meta.setdefault("image_model", job.get("selected_image_model", "nano-banana-pro"))
        meta["image_model"] = normalize_image_model(meta.get("image_model"))
        meta["image_model_label"] = image_model_label(meta.get("image_model"))
        meta.setdefault("video_model", job.get("selected_video_model", "veo3_fast"))
        meta["video_model"] = normalize_video_model(meta.get("video_model"))
        meta["video_model_label"] = video_model_label(meta.get("video_model"))
        meta.setdefault("resolution", job.get("selected_resolution", "2K"))
        meta.setdefault("output_format", "jpg")
        meta.setdefault("image_template", job.get("selected_image_template", ""))

        if "tts_outputs" in job:
            job.pop("tts_outputs", None)
            save_job(job_id, job)

    summary = compute_summary(job.get("scenes", []))
    template_display = job_template_display(meta.get("image_template", ""))
    elevenlabs_key_set = bool((os.getenv("ELEVENLABS_API_KEY") or "").strip())
    openai_key_set = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    res_display = meta.get("resolution") or job.get("selected_resolution") or "2K"
    img_label = meta.get("image_model_label") or image_model_label(meta.get("image_model"))
    vid_label = meta.get("video_model_label") or video_model_label(meta.get("video_model"))
    scene_slot_image_header_meta = f"{res_display} · {img_label}"
    scene_slot_video_header_meta = vid_label
    audio_dir = JOB_AUDIO_DIR / job_id
    job_has_audio = audio_dir.is_dir() and any(audio_dir.glob("*.mp3"))
    tts_last_audio_href: str | None = None
    tts_last_audio_name: str | None = None
    tts_last_words_href: str | None = None
    tts_last_words_name: str | None = None
    if job_has_audio and audio_dir.is_dir():
        mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if mp3s:
            tts_last_audio_name = mp3s[0].name
            tts_last_audio_href = url_for("job_audio_file", job_id=job_id, filename=mp3s[0].name)
            # Парный .words.json (если есть) — для авто-подгрузки блока «Тайминги слов».
            words_candidate = mp3s[0].with_suffix("").name + ".words.json"  # base без .mp3 + .words.json
            words_path = audio_dir / words_candidate
            if words_path.is_file():
                tts_last_words_name = words_candidate
                tts_last_words_href = url_for("job_audio_file", job_id=job_id, filename=words_candidate)
    _mont = meta.get("montage") if isinstance(meta.get("montage"), dict) else {}
    montage_zoom_scale = _montage_zoom_scale_from_meta(_mont)
    montage_zoom_mode = _montage_zoom_mode_clamp(_mont.get("zoom_mode"))
    montage_zoom_smooth = _montage_bool_clamp(_mont.get("zoom_smooth"))
    if _mont.get("zoom_ref_seconds") is not None:
        montage_zoom_ref_seconds = _montage_zoom_ref_seconds_clamp(_mont.get("zoom_ref_seconds"))
    else:
        montage_zoom_ref_seconds = _MONTAGE_ZOOM_REF_SEC_DEFAULT
    try:
        montage_fade_in_pct = max(0, min(100, int(round(float(_mont.get("fade_in_pct") or 0)))))
    except (TypeError, ValueError):
        montage_fade_in_pct = 0

    # Состояние «после рефреша»: если props.json/out.mp4 уже есть на диске —
    # сразу восстанавливаем кнопки «props.json», «Открыть в Remotion», «Скачать MP4».
    _rem_dir = _job_remotion_dir(job_id)
    _props_path = _rem_dir / "props.json"
    _mp4_path = _rem_dir / "out.mp4"
    montage_props_ready = _props_path.is_file()
    montage_mp4_ready = _mp4_path.is_file()
    montage_props_url = url_for("job_montage_file", job_id=job_id, filename="props.json") if montage_props_ready else ""
    montage_mp4_url = url_for("job_montage_file", job_id=job_id, filename="out.mp4") if montage_mp4_ready else ""
    montage_remotion_open_url = url_for("job_montage_remotion_open", job_id=job_id) if montage_props_ready else ""
    # Активный рендер: если в памяти процесса есть running/queued — пробросим task_id,
    # чтобы UI сам подцепился к прогрессу через /montage/render/status.
    montage_active_render_task_id = ""
    with _montage_render_lock:
        for _tid, _cand in _montage_render_tasks.items():
            if _cand.get("job_id") == job_id and _cand.get("state") in ("queued", "running"):
                montage_active_render_task_id = _tid
                break
    return render_template(
        "job.html",
        job_id=job_id,
        job=job,
        scenes=job.get("scenes", []),
        scenes_stripped_with_timing=_render_scenes_stripped_with_timing(job.get("scenes") or []),
        tts_words_available=bool(tts_last_words_href),
        job_has_audio=job_has_audio,
        tts_last_text=str(job.get("tts_last_text") or ""),
        tts_last_audio_href=tts_last_audio_href,
        tts_last_audio_name=tts_last_audio_name,
        tts_last_words_href=tts_last_words_href,
        tts_last_words_name=tts_last_words_name,
        summary=summary,
        scene_slot_image_header_meta=scene_slot_image_header_meta,
        scene_slot_video_header_meta=scene_slot_video_header_meta,
        template_display=template_display,
        image_templates=templates_ui_rows(),
        tts_models=TTS_MODELS,
        elevenlabs_key_set=elevenlabs_key_set,
        openai_key_set=openai_key_set,
        tts_defaults=job.get("tts_defaults") or {},
        tts_template_names=list_elevenlabs_template_names(),
        tts_template=(job.get("tts_template") or "Naomi"),
        montage_zoom_scale=montage_zoom_scale,
        montage_zoom_mode=montage_zoom_mode,
        montage_zoom_modes=list(_MONTAGE_ZOOM_MODES),
        montage_zoom_smooth=montage_zoom_smooth,
        montage_zoom_ref_seconds=montage_zoom_ref_seconds,
        montage_zoom_ref_seconds_min=_MONTAGE_ZOOM_REF_SEC_MIN,
        montage_zoom_ref_seconds_max=_MONTAGE_ZOOM_REF_SEC_MAX,
        montage_zoom_ref_seconds_step=_MONTAGE_ZOOM_REF_SEC_STEP,
        montage_fade_in_pct=montage_fade_in_pct,
        montage_props_ready=montage_props_ready,
        montage_mp4_ready=montage_mp4_ready,
        montage_props_url=montage_props_url,
        montage_mp4_url=montage_mp4_url,
        montage_remotion_open_url=montage_remotion_open_url,
        montage_active_render_task_id=montage_active_render_task_id,
    )


try:
    _orphan_count = _tm_mark_orphan_running_as_interrupted(REWRITE_JOBS_DIR)
    if _orphan_count:
        try:
            print(f"[task_manager] marked {_orphan_count} orphan running task(s) as interrupted on startup")
        except Exception:
            pass
except Exception as _e:  # noqa: BLE001
    try:
        print(f"[task_manager] startup recovery error: {_e}")
    except Exception:
        pass


if __name__ == "__main__":
    import argparse

    pr = argparse.ArgumentParser(description="JSON Video dev server (Flask)")
    pr.add_argument(
        "--cookies",
        metavar="FILE",
        help="cookies.txt в формате Netscape (как у yt-dlp --cookies); на время процесса задаёт YT_COOKIES_PATH.",
    )
    pr.add_argument("--host", default="0.0.0.0")
    pr.add_argument("--port", type=int, default=5000)
    pr.add_argument("--no-debug", action="store_true", help="выключить debug у Flask")
    args, _unknown = pr.parse_known_args()
    if args.cookies:
        os.environ["YT_COOKIES_PATH"] = str(Path(args.cookies).expanduser().resolve())
    app.run(host=args.host, port=args.port, debug=not args.no_debug)
