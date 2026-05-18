#!/usr/bin/env python3
"""
JSON Video Generator - First Page
Web interface for parsing scene JSON and preparing for image/video generation.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
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
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

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
    add_reference_files,
    build_image_input_urls,
    collect_reference_and_logo,
    create_template_dir,
    delete_reference_file,
    delete_template_dir,
    list_templates,
    rename_template_dir,
    is_logo_file,
    safe_template_dir,
    save_logo_file,
    save_reference_order,
    template_detail,
    validate_template_name,
)
from elevenlabs_client import (
    SPEED_PCT_DEFAULT,
    TTS_MODELS,
    chars_to_words_ms,
    list_voices as elevenlabs_list_voices,
    max_chars_for_model,
    max_chars_for_tts_with_timestamps,
    normalize_tts_model_id,
    normalize_tts_script_source,
    merge_mp3_files_ffmpeg,
    mp3_duration_seconds_ffprobe,
    split_tts_text_into_chunks,
    text_to_speech_bytes,
    text_to_speech_with_timestamps,
)
from job_scene_audio_align import align_scenes_to_word_timings, merge_audio_timing_into_scenes
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
    REWRITE_CHAT_TEMPERATURE,
    REWRITE_DEFAULT_MODEL,
    REWRITE_MODELS,
    REWRITE_STREAM_USER_TERMINATOR,
    clamp_chat_temperature,
    iter_draft1_blockwise_completion,
    iter_rewrite_completion,
    iter_rewrite_completion_stream,
    list_draft1_wire_chat_payloads_for_export,
    normalize_rewrite_model,
    rewrite_chat_completion_wire_payload,
    scrub_rewrite_end_markers,
)
from rewrite_pipeline import (
    REWRITE_PRESET_DEFAULT,
    REWRITE_PRESET_KEYS,
    REWRITE_PRESET_DESCRIPTIONS,
    REWRITE_PRESET_LABELS,
    REWRITE_PRESET_PREWRITTEN,
    REWRITE_PRESET_SOFT,
    REWRITE_PRESET_STAGE_KEYS,
    REWRITE_STAGE_CARD_NO_INDEX_KEYS,
    REWRITE_STAGE_HELP_HINTS,
    REWRITE_STAGE_KEYS,
    REWRITE_STAGE_SEND_HINTS,
    REWRITE_STAGE_SUBTITLES,
    REWRITE_STAGES,
    _extract_edited_text,
    parse_voiceover_editor_payload,
    any_stage_has_result,
    clamp_target_chars,
    apply_title_strategist_original_title_to_user_json,
    build_elevenlabs_editor_check,
    compose_rewrite_openai_request_body,
    downstream_script_input_text,
    strip_elevenlabs_inserts,
    normalize_rewrite_pipeline_language,
    normalize_rewrite_preset,
    snapshot_rewrite_preset_from_body,
    stages_for_preset,
    merge_stages_from_request,
    new_stages_dict,
    normalize_rewrite_job_data,
    rewrite_placeholder_apply_from_request,
    snapshot_master_prompt_from_body,
    snapshot_original_title_from_body,
    snapshot_pipeline_extras_from_body,
    snapshot_rewrite_pipeline_language_from_body,
    snapshot_stages_from_body,
    stage_run_prerequisites_met,
    _stage_user_prompt_text,
)
from rewrite_templates import (
    REWRITE_TEMPLATES_DIR,
    REWRITE_TEMPLATE_SCOPE_STAGE_KEYS,
    allocate_rewrite_template_name,
    filter_stages_for_template_scope,
    find_logo_file,
    list_rewrite_template_names,
    load_rewrite_template,
    rename_rewrite_template_dir,
    resolve_rewrite_template_name,
    save_rewrite_template_logo,
    save_rewrite_template_to_disk,
)
from locked_prompts import (
    LOCKED_PROMPTS as LOCKED_PROMPTS_REGISTRY,
    get_locked_prompt,
    is_known_prompt as locked_prompt_is_known,
    public_state as locked_prompt_public_state,
    save_locked_prompt,
    verify_pin as verify_locked_prompts_pin,
)
from claude_kie import CLAUDE_MODEL_IDS, strip_markdown_code_fence
from json_llm_repair import STAGE_JSON_OBJECT_KEYS, normalize_llm_json_object
from model_text_sanitize import normalize_model_plain_text
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
REWRITE_JOBS_DIR = BASE_DIR / "data" / "rewrite_jobs"
REWRITE_MEDIA_DIR = BASE_DIR / "data" / "rewrite_media"

_REWRITE_JSON_EDITOR_STAGES = frozenset({
    "retention_editor",
    "hook_editor",
    "persona_editor",
    "voiceover_editor",
})


def _rewrite_stage_editor_changes_cell_key(stage_key: str) -> str:
    return "voiceover_changes" if stage_key == "voiceover_editor" else f"{stage_key}_changes"


def _sanitize_scene_deprecated(scene: dict[str, Any]) -> None:
    """Удаляет поля снятых фич: animation, prompt_master, Live media / Pexels, …"""
    if not isinstance(scene, dict):
        return
    scene.pop("animation", None)
    scene.pop("content_type", None)
    scene.pop("keywords", None)
    scene.pop("excluded_keywords", None)
    scene.pop("pexels_results", None)
    scene.pop("pexels_selected_indices", None)
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


def _strip_deprecated_job_fields(job: dict[str, Any] | None) -> None:
    """Remove keys from older UI versions so they do not linger in saved JSON."""
    if not isinstance(job, dict):
        return
    job.pop("tts_template", None)


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


# Единый формат ID после слияния rewrite + job: разрешаем оба префикса,
# чтобы существующие AJAX-роуты под `/rewrite/<id>/...` принимали как старые
# rewrite_YYYYMMDD_HHMMSS, так и новые job_YYYYMMDD_HHMMSS.
_REWRITE_ID_RE = re.compile(r"^(rewrite|job)_\d{8}_\d{6}$")


def _timings_source_normalize(value: Any) -> str:
    """Нормализует источник пословных таймингов: 'elevenlabs' (default) или 'whisper'."""
    v = str(value or "").strip().lower()
    if v == "whisper":
        return "whisper"
    return "elevenlabs"


def _words_path_suffix_for_source(source: str) -> str:
    """Суффикс файла со словами в зависимости от источника."""
    return ".whisper.words.json" if _timings_source_normalize(source) == "whisper" else ".words.json"


def _latest_tts_words_doc_for_job(
    job_id: str,
    source: str = "elevenlabs",
) -> tuple[dict[str, Any] | None, str | None]:
    """Последний по mtime MP3 в data/job_audio/<job_id>/ и парный JSON со словами.

    `source = "elevenlabs"` → `<stem>.words.json` (TTS от ElevenLabs).
    `source = "whisper"`    → `<stem>.whisper.words.json` (локальный Whisper).
    """
    audio_dir = JOB_AUDIO_DIR / job_id
    if not audio_dir.is_dir():
        return None, None
    mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3s:
        return None, None
    mp3 = mp3s[0]
    words_path = audio_dir / f"{mp3.stem}{_words_path_suffix_for_source(source)}"
    if not words_path.is_file():
        return None, None
    try:
        doc = json.loads(words_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(doc, dict):
        return None, None
    return doc, mp3.name


def _job_has_words_for_source(job_id: str, source: str) -> bool:
    """Быстрая проверка: есть ли для последнего MP3 файл слов выбранного источника."""
    audio_dir = JOB_AUDIO_DIR / job_id
    if not audio_dir.is_dir():
        return False
    mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3s:
        return False
    return (audio_dir / f"{mp3s[0].stem}{_words_path_suffix_for_source(source)}").is_file()


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


def _apply_tts_word_timings_to_scenes(
    job_id: str,
    scenes: list[dict],
    source: str = "elevenlabs",
) -> None:
    """Если у выбранного источника есть words.json — выравнивает сцены и пишет audio_timing.

    `source` ∈ {"elevenlabs", "whisper"} — какой файл слов использовать
    (см. `_latest_tts_words_doc_for_job`). По умолчанию — ElevenLabs, чтобы не ломать
    существующее поведение в worker'ах рендера.
    """
    if not scenes:
        return
    words_doc, audio_fname = _latest_tts_words_doc_for_job(job_id, source=source)
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
    # Разрешаем озвучку (.mp3), парный `.words.json` (ElevenLabs `/with-timestamps`)
    # и `.whisper.words.json` (локальный faster-whisper).
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.(mp3|words\.json|whisper\.words\.json)$", name))


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


app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def _fmt_num_ru_filter(n: object) -> str:
    """Jinja-фильтр-обёртка над `_fmt_num_ru` (он определён ниже по файлу).
    Используется в шаблонах: `{{ value|fmt_num_ru }}` для разделения разрядов
    тонким неразрывным пробелом."""
    return _fmt_num_ru(n)


app.jinja_env.filters["fmt_num_ru"] = _fmt_num_ru_filter
app.jinja_env.filters["rewrite_pipeline_lang"] = normalize_rewrite_pipeline_language
# Large scene batches can exceed Werkzeug's form defaults.
# Allow bigger payloads for `/parse` and similar form submissions.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 64 * 1024 * 1024
# Если nginx не отдаёт /static/ с того же хоста: STATIC_STYLE_HREF=https://…/static/style.css
app.config["STATIC_STYLE_HREF"] = (os.getenv("STATIC_STYLE_HREF") or "").strip()


@app.context_processor
def _inject_static_style_mtime() -> dict[str, str]:
    """Cache-bust для style.css: `?v=<mtime>-<hash>` — меняется при любой правке файла."""
    try:
        p = Path(app.static_folder) / "style.css"
        raw = p.read_bytes()
        v = f"{int(p.stat().st_mtime)}-{hashlib.md5(raw).hexdigest()[:10]}"
    except (OSError, AttributeError, TypeError):
        v = ""
    return {"static_style_mtime": v}
GENERATION_TASKS: dict[str, dict] = {}


def public_base_url_for_kie() -> str:
    """Базовый URL этого приложения, доступный из интернета (Kie скачивает image_input по URL)."""
    b = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if b:
        return b
    if has_request_context():
        return request.url_root.rstrip("/")
    return ""


def _image_template_asset_url(folder_name: str, filename: str) -> str:
    """Публичный URL файла шаблона; ?v=mtime сбрасывает кэш после замены logo.png."""
    base = url_for("template_assets", template_name=folder_name, filename=filename)
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return base
    target = (td / filename).resolve()
    try:
        target.relative_to(td.resolve())
    except ValueError:
        return base
    if not target.is_file():
        return base
    try:
        return f"{base}?v={int(target.stat().st_mtime)}"
    except OSError:
        return base


def templates_ui_rows() -> list[dict]:
    rows = list_templates()
    for r in rows:
        lf = r.get("logo_file")
        if lf:
            r["logo_url"] = _image_template_asset_url(r["folder_name"], lf)
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
    logo_url = _image_template_asset_url(name, logo.name) if logo else None
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
    # Короткий max-age: при смене logo.png URL обновляется через ?v=mtime
    return send_from_directory(d, filename, max_age=300)


def _image_template_detail_json(folder_name: str) -> dict | None:
    detail = template_detail(folder_name)
    if not detail:
        return None
    fn = detail["folder_name"]
    logo_file = detail.get("logo_file")
    detail["logo_url"] = (
        _image_template_asset_url(fn, logo_file) if logo_file else None
    )
    refs_out = []
    for r in detail.get("references") or []:
        fname = r.get("filename") or ""
        if fname:
            refs_out.append(
                {
                    "filename": fname,
                    "url": _image_template_asset_url(fn, fname),
                }
            )
    detail["references"] = refs_out
    return detail


@app.route("/api/image-templates", methods=["GET"])
def api_image_templates_list():
    return jsonify({"ok": True, "templates": templates_ui_rows()})


@app.route("/api/image-templates", methods=["POST"])
def api_image_templates_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    _td, err = create_template_dir(name)
    if err:
        code = 409 if "уже существует" in err else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True, "template": _image_template_detail_json(name)})


@app.route("/api/image-templates/<path:folder_name>", methods=["GET"])
def api_image_templates_get(folder_name: str):
    detail = _image_template_detail_json(folder_name)
    if not detail:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "template": detail})


@app.route("/api/image-templates/<path:folder_name>", methods=["PUT"])
def api_image_templates_update(folder_name: str):
    body = request.get_json(silent=True) or {}
    new_name = str(body.get("name") or "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "Введите название шаблона."}), 400
    old = str(folder_name or "").strip()
    if new_name != old:
        err = rename_template_dir(old, new_name)
        if err:
            code = 409 if "уже существует" in err else 400
            return jsonify({"ok": False, "error": err}), code
        folder_name = new_name
    detail = _image_template_detail_json(folder_name)
    if not detail:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "template": detail})


@app.route("/api/image-templates/<path:folder_name>", methods=["DELETE"])
def api_image_templates_delete(folder_name: str):
    err = delete_template_dir(str(folder_name or "").strip())
    if err:
        code = 404 if "не найден" in err.lower() else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True, "deleted": folder_name})


@app.route("/api/image-templates/<path:folder_name>/logo", methods=["POST"])
def api_image_templates_upload_logo(folder_name: str):
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return jsonify({"ok": False, "error": "not_found"}), 404
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Файл логотипа не передан."}), 400
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "Пустой файл."}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".png"
    try:
        save_logo_file(td, data, ext)
    except OSError:
        return jsonify({"ok": False, "error": "Не удалось сохранить логотип."}), 500
    detail = _image_template_detail_json(td.name)
    return jsonify({"ok": True, "template": detail})


@app.route("/api/image-templates/<path:folder_name>/references", methods=["POST"])
def api_image_templates_upload_references(folder_name: str):
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return jsonify({"ok": False, "error": "not_found"}), 404
    uploads: list[tuple[str, bytes]] = []
    for key in request.files:
        for f in request.files.getlist(key):
            if not f or not f.filename:
                continue
            raw = f.read()
            if raw:
                uploads.append((f.filename, raw))
    if not uploads:
        return jsonify({"ok": False, "error": "Нет файлов для загрузки."}), 400
    saved, err = add_reference_files(td, uploads)
    if err and not saved:
        return jsonify({"ok": False, "error": err}), 400
    detail = _image_template_detail_json(td.name)
    return jsonify({"ok": True, "saved": saved, "template": detail, "warning": err})


@app.route(
    "/api/image-templates/<path:folder_name>/references/order",
    methods=["PUT"],
)
def api_image_templates_reorder_references(folder_name: str):
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    order = body.get("order")
    if not isinstance(order, list):
        return jsonify({"ok": False, "error": "Передайте массив order с именами файлов."}), 400
    err = save_reference_order(td, order)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    detail = _image_template_detail_json(td.name)
    return jsonify({"ok": True, "template": detail})


@app.route(
    "/api/image-templates/<path:folder_name>/references/<path:filename>",
    methods=["DELETE"],
)
def api_image_templates_delete_reference(folder_name: str, filename: str):
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if not delete_reference_file(td, filename):
        return jsonify({"ok": False, "error": "not_found"}), 404
    detail = _image_template_detail_json(td.name)
    return jsonify({"ok": True, "template": detail})


# --- Parsing logic ---

def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_voiceover_plain_text(raw_text: str) -> str:
    text, _changes = parse_voiceover_editor_payload(raw_text)
    return text if text else str(raw_text or "")


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


_STRUCTURE_SPLITTER_CHECK_MAX_DELTA_CHARS = 50


def _rewrite_stage_last_result_text(
    rewrite_id: str, stages_snap: dict[str, Any], stage_key: str
) -> str:
    t = str((stages_snap.get(stage_key) or {}).get("last_result") or "")
    if not t.strip():
        p = _rewrite_stage_result_path(rewrite_id, stage_key)
        if p.exists():
            try:
                t = p.read_text(encoding="utf-8")
            except OSError:
                t = ""
    return t


def _structure_splitter_check_input_text(
    *,
    rewrite_id: str,
    stages_snap: dict[str, Any],
    voiceover_plain: str,
) -> str:
    return voiceover_plain


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
    has_output_text = output_chars > 0
    structure_ok = (
        has_blocks
        and has_output_text
        and abs(delta_chars) <= _STRUCTURE_SPLITTER_CHECK_MAX_DELTA_CHARS
    )
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


def _normalize_stage_json_result(stage_key: str, content: str) -> str:
    """Починка типичных синтаксических ошибок в JSON-ответах Analysis / Architect."""
    sk = str(stage_key or "").strip().lower()
    if sk not in STAGE_JSON_OBJECT_KEYS:
        return content
    fixed, _repaired = normalize_llm_json_object(content)
    return fixed


def _normalize_stage_plain_result(stage_key: str, content: str) -> str:
    """Убирает HTML <br> из plain-text Result (Block Writer, Rewrite, редакторы)."""
    sk = str(stage_key or "").strip().lower()
    if sk in STAGE_JSON_OBJECT_KEYS:
        return content
    return normalize_model_plain_text(content)


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
    tt = obj.get("text")

    def _norm_inline(s: str) -> str:
        t = normalize_model_plain_text(str(s or ""))
        t = re.sub(r"\\+r\\+n", " ", t)
        t = re.sub(r"\\+n", " ", t)
        t = re.sub(r"\\+r", " ", t)
        t = t.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        t = re.sub(r"[ \t]{2,}", " ", t).strip()
        return t

    changed = False
    if isinstance(et, str):
        nt = _norm_inline(et)
        obj["edited_text"] = nt
        changed = True
    if isinstance(tt, str):
        nt2 = _norm_inline(tt)
        obj["text"] = nt2
        changed = True
    if not changed:
        return raw

    return json.dumps(obj, ensure_ascii=False)


def _iter_scene_json_objects(raw_text: str) -> list[tuple[int, Any, str | None]]:
    """Парсит raw_text как поток JSON-объектов (могут занимать несколько строк).

    Возвращает список троек (1-based line number начала объекта, объект, error_message).
    Если объект распарсился — error_message is None. Если нет — obj is None.
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
            j = text.find("\n", i)
            if j == -1:
                j = n
            line_chunk = text[i:j]
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

        # Live media (content_type / keywords / Pexels) больше не поддерживаются.
        if "content_type" in obj:
            errors.append(
                f"У сцены {current_scene.get('scene_id')} поле content_type больше не поддерживается — удалите его."
            )
            continue

        if "keywords" in obj:
            errors.append(
                f"У сцены {current_scene.get('scene_id')} поле keywords больше не поддерживается — удалите его."
            )
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
        _strip_deprecated_job_fields(payload)
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
                if not isinstance(job, dict):
                    return None
                _sanitize_job_scenes(job)
                _strip_deprecated_job_fields(job)
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
        _strip_deprecated_job_fields(job)
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
# Хранится в каталоге `locked_prompt_files/` и редактируется из UI только по
# пин-коду (см. модуль `locked_prompts.py`). Дефолт лежит в реестре.
def _translate_to_ru_system_prompt() -> str:
    return get_locked_prompt("translate_to_ru")


def _fmt_num_ru(n: object) -> str:
    """Целое число с тонким неразрывным пробелом (U+202F) в качестве
    разделителя разрядов: 18034 → "18 034", -15766 → "-15 766", "abc" → "abc".
    Применяем во всех пользовательских строках со счётчиками символов,
    слов, байтов и т. п. — чтобы UI читался единообразно, как в локали ru-RU.
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n)
    sign = "-" if v < 0 else ""
    return sign + format(abs(v), ",d").replace(",", "\u202f")


def _locked_prompt_fingerprint(text: str) -> str:
    """Короткий отпечаток системного промта: «<длина> симв., #<sha1[:8]>».
    Используется в статус-сообщениях задач, чтобы пользователь мог глазами
    убедиться, что после редактирования промта в OpenAI ушёл свежий текст,
    а не закэшированный старый. Это диагностика, не криптография."""
    if not text:
        return "0 симв., #00000000"
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{_fmt_num_ru(len(text))} симв., #{h}"


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


def _rewrite_stage_editor_changes_path(rewrite_id: str, stage_key: str) -> Path:
    return _rewrite_project_dir(rewrite_id) / f"{stage_key}.changes.txt"


def _rewrite_stage_voiceover_changes_path(rewrite_id: str) -> Path:
    return _rewrite_stage_editor_changes_path(rewrite_id, "voiceover_editor")


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
        return json.loads(t)
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
    Копия тела POST для файла: в HTTP messages[].content и (Claude) system — строки;
    здесь JSON разворачивается рекурсивно; многострочный plain text — в _export.text_lines.
    """
    out = copy.deepcopy(body)
    sy = out.get("system")
    if isinstance(sy, str):
        out["system"] = _message_content_for_openai_export(sy)
    elif isinstance(sy, (dict, list)):
        out["system"] = _expand_value_for_openai_export(sy, 0)
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
        "openai_chat_completions_request_dict / rewrite_chat_completion_wire_payload "
        "(нормализация model, sanitize на system/user, temperature=REWRITE_CHAT_TEMPERATURE). "
        "draft1 и scene_writer шлют несколько POST подряд — в requests[] по одному объекту на каждый такой вызов "
        "(для draft1 при отсутствии block_*.json контекст short_summary может отличаться от живого прогона — см. notes). "
        "Файл — читаемый JSON (UTF-8, отступы); реальное тело POST кодируется компактнее (другой вид сериализации JSON). "
        "Здесь messages[].content и (для Claude) поле system могут быть развёрнуты в объекты и в пометки "
        "{\"_export\":\"text_lines\",\"lines\":[...]} — это только в этом файле для просмотра; в HTTP к API такого нет, там всегда строки."
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


def _export_wire_payloads_translate_source_ru(body: dict[str, Any], rw_job: dict[str, Any]) -> str:
    """Скачиваемый JSON: те же POST, что при переводе Source → RU (по батчам)."""
    src = str(body.get("source_text") if "source_text" in body else rw_job.get("source_text") or "")
    model = normalize_rewrite_model(
        str(
            body.get("russian_semantic_model")
            or body.get("model")
            or rw_job.get("russian_semantic_model")
            or ""
        )
    )
    chat_temp = clamp_chat_temperature(rw_job.get("chat_temperature"))
    hdr: list[str] = []
    if not src.strip():
        hdr.append("[Russian] Нет текста для перевода — POST не формируется.")
        return _format_openai_wire_payloads_txt([], header_lines=hdr)
    sys_prompt = rewrite_placeholder_apply_from_request(
        get_locked_prompt("translate_to_ru"), body, rw_job
    )
    batches = _split_text_into_translation_batches(src, 5000)
    nb = len(batches)
    hdr.append(
        f"Перевод: {nb} POST (батчи до {_fmt_num_ru(5000)} симв.), один system + user на батч."
    )
    wire: list[dict[str, Any]] = []
    for chunk in batches:
        wire.append(
            rewrite_chat_completion_wire_payload(
                model, sys_prompt, chunk, chat_temperature=chat_temp
            )
        )
    return _format_openai_wire_payloads_txt(wire, header_lines=hdr or None)


def _export_wire_payloads_translate_voiceover_final_ru(body: dict[str, Any], rw_job: dict[str, Any]) -> str:
    """Скачиваемый JSON: те же POST, что при переводе «Итоговый текст» → RU (по батчам)."""
    src = str(
        body.get("voiceover_final_text")
        if "voiceover_final_text" in body
        else rw_job.get("voiceover_final_text") or ""
    )
    model = normalize_rewrite_model(
        str(
            body.get("russian_semantic_model")
            or body.get("model")
            or rw_job.get("russian_semantic_model")
            or ""
        )
    )
    chat_temp = clamp_chat_temperature(rw_job.get("chat_temperature"))
    hdr: list[str] = []
    if not src.strip():
        hdr.append("[Russian / Итоговый текст] Нет текста для перевода — POST не формируется.")
        return _format_openai_wire_payloads_txt([], header_lines=hdr)
    sys_prompt = rewrite_placeholder_apply_from_request(
        get_locked_prompt("translate_to_ru"), body, rw_job
    )
    batches = _split_text_into_translation_batches(src, 5000)
    nb = len(batches)
    hdr.append(
        f"Перевод (итог озвучки): {nb} POST (батчи до {_fmt_num_ru(5000)} симв.), один system + user на батч."
    )
    wire: list[dict[str, Any]] = []
    for chunk in batches:
        wire.append(
            rewrite_chat_completion_wire_payload(
                model, sys_prompt, chunk, chat_temperature=chat_temp
            )
        )
    return _format_openai_wire_payloads_txt(wire, header_lines=hdr or None)


def _export_wire_payload_semantic_voiceover_final(body: dict[str, Any], rw_job: dict[str, Any]) -> str:
    """Скачиваемый JSON: Semantic по русскому переводу итога озвучки (отдельно от source)."""
    src_ru = str(
        body.get("voiceover_final_text_ru")
        if "voiceover_final_text_ru" in body
        else rw_job.get("voiceover_final_text_ru") or ""
    )
    model = normalize_rewrite_model(
        str(
            body.get("russian_semantic_model")
            or body.get("model")
            or rw_job.get("russian_semantic_model")
            or ""
        )
    )
    chat_temp = clamp_chat_temperature(rw_job.get("chat_temperature"))
    hdr: list[str] = []
    if not src_ru.strip():
        hdr.append("[Semantic итог] Нет русского перевода итога — POST не формируется.")
        return _format_openai_wire_payloads_txt([], header_lines=hdr)
    system_prompt = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_system"), body, rw_job
    )
    user_template = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_user"), body, rw_job
    )
    user_msg = (user_template or "").rstrip() + "\n\n" + src_ru.strip()
    wire = [
        rewrite_chat_completion_wire_payload(
            model, system_prompt, user_msg, chat_temperature=chat_temp
        )
    ]
    return _format_openai_wire_payloads_txt(wire)


def _export_wire_payload_semantic_text_analyzer(body: dict[str, Any], rw_job: dict[str, Any]) -> str:
    """Скачиваемый JSON: один POST Semantic (system + user с русским текстом)."""
    src_ru = str(
        body.get("source_text_ru")
        if "source_text_ru" in body
        else rw_job.get("source_text_ru") or ""
    )
    model = normalize_rewrite_model(
        str(
            body.get("russian_semantic_model")
            or body.get("model")
            or rw_job.get("russian_semantic_model")
            or ""
        )
    )
    chat_temp = clamp_chat_temperature(rw_job.get("chat_temperature"))
    hdr: list[str] = []
    if not src_ru.strip():
        hdr.append("[Semantic] Нет русского текста — POST не формируется.")
        return _format_openai_wire_payloads_txt([], header_lines=hdr)
    system_prompt = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_system"), body, rw_job
    )
    user_template = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_user"), body, rw_job
    )
    user_msg = (user_template or "").rstrip() + "\n\n" + src_ru.strip()
    wire = [
        rewrite_chat_completion_wire_payload(
            model, system_prompt, user_msg, chat_temperature=chat_temp
        )
    ]
    return _format_openai_wire_payloads_txt(wire)


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
    # Параллель фрагментов: см. также YT_DLP_CONCURRENT_FRAGMENTS в _youtube_ytdlp_perf_opts().
    "concurrent_fragment_downloads": 5,
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


def _youtube_proxy_config_path() -> Path:
    return (BASE_DIR / "data" / "secrets" / "yt_dlp_proxy.json").resolve()


def _youtube_proxy_default_config() -> dict[str, Any]:
    return {
        "proxy_url": "",
        "proxy_input": "",
        "updated_at": None,
        "last_test_ok": None,
        "last_test_at": None,
        "last_test_message": "",
    }


def _youtube_proxy_load() -> dict[str, Any]:
    cfg = _youtube_proxy_default_config()
    p = _youtube_proxy_config_path()
    if not p.is_file():
        return cfg
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    for k in cfg:
        if k in data:
            cfg[k] = data[k]
    cfg["proxy_url"] = str(cfg.get("proxy_url") or "").strip()
    cfg["proxy_input"] = str(cfg.get("proxy_input") or "").strip()
    if not cfg["proxy_input"] and cfg["proxy_url"]:
        cfg["proxy_input"] = _youtube_proxy_compact(cfg["proxy_url"])
    return cfg


def _youtube_proxy_save(cfg: dict[str, Any]) -> None:
    p = _youtube_proxy_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _youtube_proxy_normalize(raw: str) -> str:
    """Принимает ``user:pass@host:port`` или полный URL; возвращает URL для yt-dlp/requests."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    parsed = urlparse(s)
    if not parsed.hostname:
        raise ValueError(
            "Некорректный прокси. Пример: user408609:пароль@185.198.233.243:4588 "
            "или http://user:pass@host:port"
        )
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        scheme = "http"
    netloc = parsed.netloc or ""
    if not netloc:
        raise ValueError("В прокси не указан хост (netloc пустой).")
    return urlunparse((scheme, netloc, "", "", "", ""))


def _youtube_proxy_compact(proxy_url: str) -> str:
    """Формат для копирования: ``user:pass@host:port`` (без схемы)."""
    s = (proxy_url or "").strip()
    if not s:
        return ""
    if "://" not in s:
        return s
    try:
        return urlparse(s).netloc or ""
    except Exception:
        return ""


def _youtube_proxy_mask(proxy_url: str) -> str:
    """Маскирует логин/пароль для отображения в UI и API."""
    if not (proxy_url or "").strip():
        return ""
    try:
        p = urlparse(proxy_url.strip())
    except Exception:
        return "***"
    host = p.hostname or ""
    port = f":{p.port}" if p.port else ""
    user = p.username or ""
    if user:
        uvis = user[:3] + "…" if len(user) > 3 else "***"
        tail = f"{uvis}@{host}{port}"
    else:
        tail = f"{host}{port}"
    sch = (p.scheme or "http").lower()
    return f"{sch}://{tail}"


def _youtube_proxy_run_test(proxy_url: str) -> tuple[bool, str]:
    """Проверка: HTTP(S) к YouTube через прокси (как в браузере)."""
    if not proxy_url:
        return False, "Пустой прокси"
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get(
            "https://www.youtube.com/",
            proxies=proxies,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; json-video-yt-proxy-check/1.0; "
                    "+https://github.com/yt-dlp/yt-dlp)"
                ),
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            allow_redirects=True,
        )
        code = int(r.status_code)
        if 200 <= code < 500:
            return True, f"YouTube ответил HTTP {code}"
        return False, f"YouTube HTTP {code}"
    except requests.RequestException as e:
        return False, (str(e) or "Ошибка сети")[:400]


def _youtube_proxy_effective_url() -> str:
    """Активный URL прокси: переменная окружения имеет приоритет над файлом UI."""
    env_p = (os.getenv("YT_DLP_PROXY") or "").strip()
    if env_p:
        try:
            return _youtube_proxy_normalize(env_p)
        except ValueError:
            return env_p
    return str(_youtube_proxy_load().get("proxy_url") or "").strip()


def youtube_proxy_status_dict() -> dict[str, Any]:
    """Сводка для UI: маска, последний тест, приоритет .env над файлом."""
    env_raw = (os.getenv("YT_DLP_PROXY") or "").strip()
    cfg = _youtube_proxy_load()
    file_url = str(cfg.get("proxy_url") or "").strip()
    active = _youtube_proxy_effective_url()
    env_overrides = bool(env_raw)
    try:
        masked_active = _youtube_proxy_mask(active) if active else ""
    except Exception:
        masked_active = "(прокси задан, маска недоступна)"
    masked_file = _youtube_proxy_mask(file_url) if file_url else ""
    last_ok = cfg.get("last_test_ok")
    if last_ok is not None:
        last_ok = bool(last_ok)
    file_input = str(cfg.get("proxy_input") or "").strip()
    file_compact = file_input or _youtube_proxy_compact(file_url)
    active_compact = (
        _youtube_proxy_compact(active)
        if env_overrides
        else file_compact
    )
    return {
        "env_overrides_file": env_overrides,
        "file_configured": bool(file_url),
        "active_configured": bool(active),
        "masked_active": masked_active,
        "masked_file": masked_file,
        "proxy_file_compact": file_compact,
        "proxy_active_compact": active_compact,
        "last_test_ok": last_ok,
        "last_test_at": cfg.get("last_test_at"),
        "last_test_message": str(cfg.get("last_test_message") or "")[:500],
    }


def _youtube_ytdlp_env_bool(name: str, *, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def _youtube_ytdlp_perf_opts() -> dict[str, Any]:
    """Доп. опции YoutubeDL: IPv4, без кеша, параллель фрагментов, внешний загрузчик, прокси.

    Переменные окружения (все опциональны):
      YT_DLP_FORCE_IPV4=1|0 — как ``-4`` / ``--force-ipv4`` (по умолчанию 1).
      YT_DLP_NOCACHE=1|0 — как ``--no-cache-dir`` (по умолчанию 1).
      YT_DLP_CONCURRENT_FRAGMENTS=N — число параллельных фрагментов (1..32, по умолчанию 5).
      YT_DLP_USE_ARIA2C=1|0 — если 1 и ``aria2c`` в PATH, задаёт ``external_downloader`` (по умолчанию 1).
      YT_DLP_EXTERNAL_DOWNLOADER=aria2c|curl|… — явное имя загрузчика (первое слово должно быть в PATH).
      YT_DLP_PROXY=URL — прокси для yt-dlp (перекрывает файл из UI). Либо задайте прокси
      в блоке «Прокси yt-dlp» на странице job — сохранится в ``data/secrets/yt_dlp_proxy.json``.

    Глобальные ``HTTP_PROXY`` / ``HTTPS_PROXY`` для процесса yt-dlp обычно подхватываются сами;
    ``YT_DLP_PROXY`` или файл из UI — если нужен отдельный прокси только для YouTube.
    """
    out: dict[str, Any] = {}
    if _youtube_ytdlp_env_bool("YT_DLP_FORCE_IPV4", default=True):
        out["force_ipv4"] = True
    if _youtube_ytdlp_env_bool("YT_DLP_NOCACHE", default=True):
        out["nocachedir"] = True
    try:
        cfd = int((os.getenv("YT_DLP_CONCURRENT_FRAGMENTS") or "5").strip())
    except (TypeError, ValueError):
        cfd = 5
    out["concurrent_fragment_downloads"] = max(1, min(32, cfd))

    ext_raw = (os.getenv("YT_DLP_EXTERNAL_DOWNLOADER") or "").strip()
    if ext_raw.lower() in ("", "0", "none", "native", "default"):
        ext_bin = ""
    else:
        ext_bin = ext_raw.split()[0]
    if ext_bin:
        if shutil.which(ext_bin):
            out["external_downloader"] = ext_raw
        else:
            try:
                app.logger.warning(
                    "YT_DLP_EXTERNAL_DOWNLOADER=%r: исполняемый файл не найден в PATH — встроенный загрузчик.",
                    ext_raw,
                )
            except Exception:
                pass
    elif _youtube_ytdlp_env_bool("YT_DLP_USE_ARIA2C", default=True) and shutil.which("aria2c"):
        out["external_downloader"] = "aria2c"

    proxy = _youtube_proxy_effective_url()
    if proxy:
        out["proxy"] = proxy
    return out


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
        "filename": p.name,
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


_YOUTUBE_OG_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)\s*=\s*"og:image"[^>]*content\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)


def _youtube_fetch_channel_avatar_url(channel_url: str, *, timeout: float = 4.0) -> str:
    """Качаем HTML канала и вытаскиваем <meta property="og:image"> — это аватар.

    На странице канала YouTube кладёт в og:image именно круглый логотип канала
    (формат s900-c-k-..., домены yt3.googleusercontent.com / i.ytimg.com).
    Возвращаем "" при любых сбоях — UI просто не покажет картинку.
    """
    u = (channel_url or "").strip()
    if not u or not (u.startswith("http://") or u.startswith("https://")):
        return ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(u, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return ""
    if not r.ok:
        return ""
    m = _YOUTUBE_OG_IMAGE_RE.search(r.text or "")
    if not m:
        return ""
    av = m.group(1).strip()
    if not (av.startswith("https://yt3.") or av.startswith("https://i.ytimg.")):
        return ""
    return av


def _youtube_channel_meta_from_info(info: dict | None) -> dict[str, str]:
    """Извлекаем читаемые поля канала из yt-dlp info."""
    d = info if isinstance(info, dict) else {}
    name = str(d.get("channel") or d.get("uploader") or "").strip()
    cid = str(d.get("channel_id") or "").strip()
    url = str(d.get("channel_url") or d.get("uploader_url") or "").strip()
    return {"name": name, "id": cid, "url": url}


def _youtube_enrich_channel_meta(
    rw: dict,
    *,
    info: dict | None = None,
    fetch_avatar: bool = True,
) -> dict[str, str]:
    """Заполняем rw.youtube_channel{,_id,_url,_avatar}; возвращаем актуальные значения для клиента."""
    meta = _youtube_channel_meta_from_info(info)
    rw["youtube_channel"] = meta["name"]
    rw["youtube_channel_id"] = meta["id"]
    rw["youtube_channel_url"] = meta["url"]
    avatar = ""
    if fetch_avatar and meta["url"]:
        avatar = _youtube_fetch_channel_avatar_url(meta["url"])
    rw["youtube_channel_avatar"] = avatar
    return {
        "youtube_channel": meta["name"],
        "youtube_channel_url": meta["url"],
        "youtube_channel_avatar": avatar,
    }


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
    """Сколько секунд ждать **без данных** по сокету (CDN / youtube) на одной попытке, затем «провал» → следующий player_client.

    Эмпирика: googlevideo CDN иногда отдаёт первый байт через 25-35 секунд
    (роутинг до ближайшего edge-узла, медленный старт после n-challenge).
    При 20 секундах android_vr/android регулярно ловят Read timed out
    ещё до начала реальной отдачи аудио — тогда yt-dlp прокидывает ошибку,
    мы прыгаем на следующий player_client, и через 1-2 шага упираемся
    в `Requested format is not available`. Поэтому держим 45 с по умолчанию.
    """
    raw = (os.getenv("YOUTUBE_STALL_READ_SEC") or "45").strip()
    try:
        s = int(raw, 10)
    except ValueError:
        s = 45
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

    В 2026 году многим роликам web/ios/mweb-клиенты выдают «Requested format is not available»
    (формат требует GVS PO Token и yt-dlp его пропускает). android_vr остаётся рабочим
    фоллбеком — даёт полный список audio-only форматов, поэтому держим его в цепочке
    по умолчанию.
    """
    head = (os.getenv("YOUTUBE_PLAYER_CLIENT") or "android_vr").strip()
    head_list = [c.strip().lower() for c in head.split(",") if c.strip()]
    # Порядок: android_vr и android первыми — они возвращают полный набор audio-only
    # форматов (139/140/249/251) и не зависят от GVS PO Token, который ломает
    # web/ios/mweb («Requested format is not available» — у этих клиентов в каталоге
    # остаются только storyboards).
    # tvhtml5 и mediaconnect — две «здоровые» подстраховки: на практике (проверено
    # для подкастов с длинной озвучкой 2026‑05) они тоже отдают m4a 140 без PO Token,
    # когда CDN кладёт первую попытку android/android_vr (read timeout на googlevideo).
    # tv (классический) — лишний раунд плеера, оставили в самом конце.
    # Подогнать под ваш IP: YBENCH_URL=... .venv/bin/python3 scripts/benchmark_youtube_clients.py
    tail = (
        os.getenv("YOUTUBE_PLAYER_CLIENT_FALLBACK")
        or "android,tvhtml5,mediaconnect,web,ios,mweb"
    ).strip()
    tail_list = [c.strip().lower() for c in tail.split(",") if c.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for c in head_list + tail_list:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out or ["android_vr", "android", "tvhtml5", "mediaconnect", "web"]


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
        **_youtube_ytdlp_perf_opts(),
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
        "voiceover_final_semantic_text_analysis": "",
        "voiceover_final_semantic_text_analysis_locked": True,
        "voiceover_final_semantic_text_analysis_at": "",
        "master_prompt": "",
        "master_prompt_locked": False,
        "target_chars": clamp_target_chars(5 * 344),
        "duration_minutes": 5,
        "hero_prompt": "",
        "chars_per_minute": 344,
        "rewrite_template": "",
        "rewrite_pipeline_language": "ru",
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
        "chat_temperature": REWRITE_CHAT_TEMPERATURE,
    }


def create_rewrite_job(project_name: str) -> str:
    """Legacy: создание автономного rewrite-проекта.

    Новые проекты создаются через `create_unified_project`, единая страница
    `/job/<id>` рендерит и rewrite-блок, и job-блок. Функция оставлена для
    обратной совместимости и тестов.
    """
    REWRITE_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    rewrite_id = f"rewrite_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    payload = new_rewrite_payload(rewrite_id, project_name)
    save_rewrite_job(rewrite_id, payload)
    return rewrite_id


def create_unified_project(project_name: str) -> str:
    """Создаёт единый проект (один ID для video-job и rewrite-проекта).

    Создаются одновременно:
      - `data/jobs/<id>.json`         — каркас video-job.
      - `data/rewrite_jobs/<id>/`     — каркас rewrite-проекта с `project.json`.
    Возвращает общий ID в формате `job_YYYYMMDD_HHMMSS`.
    """
    unified_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    name = (project_name or "").strip()

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_payload = new_video_job_payload(name)
    job_path = JOBS_DIR / f"{unified_id}.json"
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(job_payload, f, ensure_ascii=False, indent=2)

    rewrite_payload = new_rewrite_payload(unified_id, name)
    save_rewrite_job(unified_id, rewrite_payload)
    return unified_id


def _ensure_job_file_for_id(unified_id: str, project_name: str = "") -> bool:
    """Если для ID есть rewrite-папка, но нет `data/jobs/<id>.json` — создаёт пустой.

    Возвращает True, если файл был создан, False — если уже существовал
    или ID невалиден.
    """
    if not rewrite_id_ok(unified_id):
        return False
    job_path = JOBS_DIR / f"{unified_id}.json"
    if job_path.is_file():
        return False
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload = new_video_job_payload(project_name)
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return True


def _ensure_rewrite_project_for_unified_id(unified_id: str) -> bool:
    """Если есть `data/jobs/<id>.json`, но нет rewrite `project.json` — создаёт каркас ReWrite.

    Иначе `job.html` скрывает весь блок `{% if rw %}`. Так бывает у старых
    проектов, копий без папки `data/rewrite_jobs/<id>/` или после ручного удаления
    `project.json`. Возвращает True, если rewrite-данные созданы.
    """
    if not rewrite_id_ok(unified_id):
        return False
    if load_rewrite_job(unified_id) is not None:
        return False
    job = load_job(unified_id)
    if job is None or not isinstance(job, dict):
        return False
    pname = str(job.get("project_name") or "").strip() or unified_id
    try:
        save_rewrite_job(unified_id, new_rewrite_payload(unified_id, pname))
    except Exception:
        app.logger.exception("ensure rewrite project failed for %s", unified_id)
        return False
    return True


def list_unified_projects() -> list[dict]:
    """Объединённый список всех проектов (job-JSON и/или rewrite-папка).

    Возвращает по одной строке на уникальный ID, отсортированной по mtime
    (новые сверху). Поля: `id`, `project_name`, `has_job`, `has_rewrite`,
    `scenes_count`, `updated_at`.
    """
    rows: dict[str, dict] = {}

    if JOBS_DIR.exists():
        for f in JOBS_DIR.glob("job_*.json"):
            jid = f.stem
            try:
                data = json.load(open(f, "r", encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            scenes = data.get("scenes") if isinstance(data, dict) else None
            if not isinstance(scenes, list):
                scenes = data.get("parsed_scenes") if isinstance(data, dict) else []
                if not isinstance(scenes, list):
                    scenes = []
            rows[jid] = {
                "id": jid,
                "project_name": (data.get("project_name") if isinstance(data, dict) else "") or "",
                "has_job": True,
                "has_rewrite": _rewrite_project_dir(jid).is_dir(),
                "scenes_count": len(scenes),
                "updated_at": (data.get("created_at") if isinstance(data, dict) else "") or "",
                "mtime": f.stat().st_mtime,
            }

    if REWRITE_JOBS_DIR.is_dir():
        for d in REWRITE_JOBS_DIR.glob("*"):
            if not d.is_dir():
                continue
            rid = d.name
            if not rewrite_id_ok(rid):
                continue
            row = rows.get(rid)
            mtime = d.stat().st_mtime
            project_json = _rewrite_project_json_path(rid)
            rw_name = ""
            if project_json.is_file():
                try:
                    rw = json.load(open(project_json, "r", encoding="utf-8"))
                    if isinstance(rw, dict):
                        rw_name = str(rw.get("project_name") or "")
                except (json.JSONDecodeError, OSError):
                    rw_name = ""
                mtime = max(mtime, project_json.stat().st_mtime)
            if row is None:
                rows[rid] = {
                    "id": rid,
                    "project_name": rw_name,
                    "has_job": False,
                    "has_rewrite": True,
                    "scenes_count": 0,
                    "updated_at": "",
                    "mtime": mtime,
                }
            else:
                row["has_rewrite"] = True
                if not row.get("project_name") and rw_name:
                    row["project_name"] = rw_name
                if mtime > row.get("mtime", 0.0):
                    row["mtime"] = mtime

    items = list(rows.values())
    items.sort(key=lambda r: r.get("mtime", 0.0), reverse=True)
    return items


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
        if not isinstance(data, dict):
            return None
        try:
            normalize_rewrite_job_data(data)
        except Exception:
            app.logger.warning(
                "rewrite project.json failed to normalize for %s", rewrite_id, exc_info=True
            )
            return None
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
            if sk in _REWRITE_JSON_EDITOR_STAGES:
                main_text, editor_changes = parse_voiceover_editor_payload(txt)
                ch_path = _rewrite_stage_editor_changes_path(rewrite_id, sk)
                if ch_path.is_file():
                    try:
                        ch_raw = ch_path.read_text(encoding="utf-8").strip()
                        if ch_raw:
                            try:
                                parsed_ch = json.loads(ch_raw)
                                if isinstance(parsed_ch, list):
                                    editor_changes = parsed_ch
                            except json.JSONDecodeError:
                                pass
                    except OSError:
                        pass
                data["stages"][sk]["last_result"] = main_text if main_text else txt
                ck = _rewrite_stage_editor_changes_cell_key(sk)
                data["stages"][sk][ck] = (
                    json.dumps(editor_changes, ensure_ascii=False, indent=2)
                    if editor_changes
                    else ""
                )
            else:
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
    try:
        normalize_rewrite_job_data(data)
    except Exception:
        app.logger.warning(
            "normalize_rewrite_job_data before rewrite save failed for %s",
            rewrite_id,
            exc_info=True,
        )
    data["rewrite_id"] = rewrite_id
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    stages = data.get("stages")
    if isinstance(stages, dict):
        for sk in REWRITE_STAGE_KEYS:
            cell = stages.get(sk) if isinstance(stages.get(sk), dict) else {}
            res_text = str((cell or {}).get("last_result") or "")
            if sk in _REWRITE_JSON_EDITOR_STAGES:
                main_text, builtin_ch = parse_voiceover_editor_payload(res_text)
                _rewrite_stage_result_path(rewrite_id, sk).write_text(
                    main_text if main_text else res_text,
                    encoding="utf-8",
                )
                ck = _rewrite_stage_editor_changes_cell_key(sk)
                ch_text = str((cell or {}).get(ck) or "").strip()
                if not ch_text and builtin_ch:
                    ch_text = json.dumps(builtin_ch, ensure_ascii=False, indent=2)
                _rewrite_stage_editor_changes_path(rewrite_id, sk).write_text(
                    ch_text,
                    encoding="utf-8",
                )
            else:
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
    if not isinstance(scenes, list):
        scenes = []
    start_count = 0
    end_count = 0
    video_count = 0
    for s in scenes:
        if not isinstance(s, dict):
            continue
        st = s.get("start")
        if isinstance(st, dict) and st.get("prompt"):
            start_count += 1
        en = s.get("end")
        if isinstance(en, dict) and en.get("prompt"):
            end_count += 1
        vd = s.get("video")
        if isinstance(vd, dict) and vd.get("prompt"):
            video_count += 1
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
    if raw in ("wan/2-7-image", "qwen2/image-edit"):
        return "nano-banana-pro"
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
    if raw in ("wan/2-7-image", "qwen2/image-edit"):
        return "Nano Banana Pro"
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


@app.route("/", methods=["GET", "POST"])
def index():
    """Единая главная: список всех проектов + создание нового unified-проекта.

    POST: создаёт сразу и `data/jobs/<id>.json`, и `data/rewrite_jobs/<id>/`
    под общим ID, редиректит на `/job/<id>`.
    """
    if request.method == "POST":
        project_name = request.form.get("project_name", "").strip()
        unified_id = create_unified_project(project_name)
        flash("Проект создан.", "success")
        return redirect(url_for("job_page", job_id=unified_id))

    resp = make_response(
        render_template(
            "home.html",
            projects=list_unified_projects(),
            openai_key_set=bool((os.getenv("OPENAI_API_KEY") or "").strip()),
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/video")
def video_index():
    return redirect(url_for("index"))


@app.route("/video", methods=["POST"])
def video_create():
    """Legacy POST: теперь создаёт единый проект (video + rewrite одним ID)."""
    project_name = request.form.get("project_name", "").strip()
    unified_id = create_unified_project(project_name)
    flash("Проект создан.", "success")
    return redirect(url_for("job_page", job_id=unified_id))


@app.route("/rewrite", methods=["GET", "POST"])
def rewrite_index():
    """Legacy URL: создание/список ReWrite теперь живёт на главной."""
    if request.method == "POST":
        project_name = request.form.get("project_name", "").strip()
        unified_id = create_unified_project(project_name)
        flash("Проект создан.", "success")
        return redirect(url_for("job_page", job_id=unified_id))
    return redirect(url_for("index"))


@app.route("/rewrite/<rewrite_id>")
def rewrite_project_page(rewrite_id: str):
    """Legacy URL — все проекты теперь на единой странице `/job/<id>`."""
    if not rewrite_id_ok(rewrite_id):
        flash("Проект не найден.", "error")
        return redirect(url_for("index"))
    if _rewrite_project_dir(rewrite_id).is_dir():
        _ensure_job_file_for_id(rewrite_id)
    if load_job(rewrite_id) is None:
        flash("Проект не найден.", "error")
        return redirect(url_for("index"))
    return redirect(url_for("job_page", job_id=rewrite_id), code=302)


def _rewrite_template_context(rewrite_id: str) -> dict:
    """Собирает контекст для рендера rewrite-блока внутри job-страницы.

    Возвращает dict ровно с теми же ключами, что раньше передавала старая
    `rewrite_project_page` в `rewrite_project.html`. Если rewrite-данных нет
    (не валидный ID или папка отсутствует) — возвращает `{"rw": None}`,
    `{% if rw %}` в `job.html` сам обрабатывает пустой случай.
    """
    if not rewrite_id_ok(rewrite_id):
        return {"rw": None}
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        _ensure_rewrite_project_for_unified_id(rewrite_id)
        rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return {"rw": None}
    st = rw.get("stages")
    if not isinstance(st, dict):
        st = {}
    if not (rw.get("youtube_channel_avatar") or "").strip() and (rw.get("youtube_url") or "").strip():
        cache_path = _youtube_info_cache_path(rewrite_id)
        cached_info: dict | None = None
        try:
            if cache_path.is_file():
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached_info = json.load(f)
        except (OSError, ValueError):
            cached_info = None
        if isinstance(cached_info, dict):
            try:
                _youtube_enrich_channel_meta(rw, info=cached_info)
                save_rewrite_job(rewrite_id, rw)
            except Exception:
                app.logger.exception("youtube channel meta backfill failed for %s", rewrite_id)
    rewrite_preset_current = normalize_rewrite_preset(rw.get("rewrite_preset"))
    _src_for_preset = str(rw.get("source_text") or rw.get("last_text") or "")
    rewrite_stage_run_ok = {
        sk: stage_run_prerequisites_met(
            sk, st, preset=rewrite_preset_current, source_text=_src_for_preset
        )
        for sk in REWRITE_STAGE_KEYS
    }
    rewrite_stage_key_order = [k for k, _ in REWRITE_STAGES]
    # После normalize_rewrite_job_data совпадает с Result Voiceover Editor.
    voiceover_final_text = str(rw.get("voiceover_final_text") or "")
    # Совпадает с логикой `_rewrite_block.html`: до 11 ключей текущего пресета
    # для массового сворачивания верхних этапов.
    preset_keys_current = REWRITE_PRESET_STAGE_KEYS.get(rewrite_preset_current, [])
    collapsible_pipeline_stages: list[str] = []
    for _k in preset_keys_current:
        if len(collapsible_pipeline_stages) >= 11:
            break
        collapsible_pipeline_stages.append(_k)
    collapsible_pipeline_stage_range_end = sum(
        1 for k in collapsible_pipeline_stages if k not in REWRITE_STAGE_CARD_NO_INDEX_KEYS
    )
    return {
        "rw": rw,
        "rewrite_stages": REWRITE_STAGES,
        "rewrite_stage_send_hints": REWRITE_STAGE_SEND_HINTS,
        "rewrite_stage_help_hints": REWRITE_STAGE_HELP_HINTS,
        "rewrite_stage_subtitles": REWRITE_STAGE_SUBTITLES,
        "rewrite_stage_run_ok": rewrite_stage_run_ok,
        "rewrite_stage_key_order": rewrite_stage_key_order,
        "rewrite_preset_current": rewrite_preset_current,
        "rewrite_preset_labels": REWRITE_PRESET_LABELS,
        "rewrite_preset_descriptions": REWRITE_PRESET_DESCRIPTIONS,
        "rewrite_preset_stage_keys": REWRITE_PRESET_STAGE_KEYS,
        "rewrite_preset_default": REWRITE_PRESET_DEFAULT,
        "rewrite_models": REWRITE_MODELS,
        "claude_rewrite_model_ids": sorted(CLAUDE_MODEL_IDS),
        "default_chat_temperature": REWRITE_CHAT_TEMPERATURE,
        "rewrite_template_names": list_rewrite_template_names(),
        "rewrite_templates_ui": rewrite_templates_ui_rows(),
        "voiceover_final_text": voiceover_final_text,
        "youtube_cookies_status": youtube_cookies_status_dict(),
        "youtube_proxy_status": youtube_proxy_status_dict(),
        "collapsible_pipeline_stages": collapsible_pipeline_stages,
        "collapsible_pipeline_stage_range_end": collapsible_pipeline_stage_range_end,
        "locked_prompts_state": {
            name: locked_prompt_public_state(name)
            for name in LOCKED_PROMPTS_REGISTRY.keys()
        },
    }


def _rewrite_project_page_legacy_unused(rewrite_id: str):
    """Старый рендер `rewrite_project.html` оставлен в репозитории как
    референс для последующих этапов. Не подключён ни к одному маршруту.
    """
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        flash("Проект ReWrite не найден.", "error")
        return redirect(url_for("index"))
    key_set = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    st = rw.get("stages")
    if not isinstance(st, dict):
        st = {}
    if not (rw.get("youtube_channel_avatar") or "").strip() and (rw.get("youtube_url") or "").strip():
        cache_path = _youtube_info_cache_path(rewrite_id)
        cached_info: dict | None = None
        try:
            if cache_path.is_file():
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached_info = json.load(f)
        except (OSError, ValueError):
            cached_info = None
        if isinstance(cached_info, dict):
            try:
                _youtube_enrich_channel_meta(rw, info=cached_info)
                save_rewrite_job(rewrite_id, rw)
            except Exception:
                app.logger.exception("youtube channel meta backfill failed for %s", rewrite_id)
    rewrite_preset_current = normalize_rewrite_preset(rw.get("rewrite_preset"))
    _src_for_preset = str(rw.get("source_text") or rw.get("last_text") or "")
    rewrite_stage_run_ok = {
        sk: stage_run_prerequisites_met(
            sk, st, preset=rewrite_preset_current, source_text=_src_for_preset
        )
        for sk in REWRITE_STAGE_KEYS
    }
    rewrite_stage_key_order = [k for k, _ in REWRITE_STAGES]
    # После normalize_rewrite_job_data совпадает с Result Voiceover Editor.
    voiceover_final_text = str(rw.get("voiceover_final_text") or "")
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
                youtube_proxy_status=youtube_proxy_status_dict(),
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
    rows = rewrite_templates_ui_rows()
    return jsonify({
        "ok": True,
        "templates": [r["name"] for r in rows],
        "templates_ui": rows,
    })


@app.route("/rewrite/api/templates", methods=["POST"])
def rewrite_api_templates_create():
    """Создать новый rewrite-шаблон из текущих данных формы."""
    body = request.get_json(silent=True) or {}
    name = allocate_rewrite_template_name(body.get("name"))
    if any(ch in name for ch in ('/', '\\')) or name.startswith('.'):
        return jsonify({"ok": False, "error": "bad_name", "message": "Недопустимое имя шаблона."}), 400

    d = REWRITE_TEMPLATES_DIR / name
    try:
        d.mkdir(parents=True, exist_ok=False)
    except OSError:
        return jsonify({"ok": False, "error": "mkdir_failed", "message": "Не удалось создать папку шаблона."}), 400

    stages = filter_stages_for_template_scope(body.get("stages"))

    ok, err = save_rewrite_template_to_disk(
        name,
        hero_prompt=str(body.get("hero_prompt") or ""),
        master_prompt=str(body.get("master_prompt") or ""),
        target_chars=None,
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


def _rewrite_template_logo_url(name: str) -> str | None:
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return None
    logo = find_logo_file(d)
    if not logo:
        return None
    base = url_for(
        "rewrite_api_template_logo_asset",
        name=name,
        filename=logo.name,
    )
    try:
        return f"{base}?v={int(logo.stat().st_mtime)}"
    except OSError:
        return base


def rewrite_templates_ui_rows() -> list[dict[str, Any]]:
    """Список rewrite-шаблонов для пикера: имя + logo_url (если есть файл)."""
    rows: list[dict[str, Any]] = []
    for name in list_rewrite_template_names():
        rows.append(
            {
                "name": name,
                "logo_url": _rewrite_template_logo_url(name),
            }
        )
    return rows


@app.route("/rewrite/api/templates/<path:name>/logo/<path:filename>")
def rewrite_api_template_logo_asset(name: str, filename: str):
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return "Not found", 404
    if "/" in filename or "\\" in filename or ".." in filename:
        return "Not found", 404
    path = (d / filename).resolve()
    try:
        path.relative_to(d.resolve())
    except ValueError:
        return "Not found", 404
    if not path.is_file() or not is_logo_file(path):
        return "Not found", 404
    return send_from_directory(d, filename)


@app.route("/rewrite/api/templates/<name>", methods=["GET"])
def rewrite_api_template_get(name: str):
    data = load_rewrite_template(name)
    if data is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = dict(data)
    data["logo_url"] = _rewrite_template_logo_url(name)
    return jsonify({"ok": True, **data})


@app.route("/rewrite/api/templates/<path:name>/edit")
def rewrite_template_edit_page(name: str):
    """Старый URL: редактирование только во всплывающем окне на странице проекта."""
    flash("Откройте шаблон двойным щелчком по кружку на странице проекта.", "info")
    return redirect(request.referrer or url_for("index"))


def rewrite_template_display_label(name: str) -> str:
    n = str(name or "").strip()
    if n.lower() == "base template":
        return "Базовый"
    return n


@app.route("/rewrite/api/elevenlabs/voices", methods=["GET"])
def rewrite_api_elevenlabs_voices():
    if not (os.getenv("ELEVENLABS_API_KEY") or "").strip():
        return jsonify({"error": "ELEVENLABS_API_KEY не задан"}), 503
    try:
        voices = elevenlabs_list_voices()
        return jsonify({"voices": voices})
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/rewrite/api/templates/<path:name>", methods=["PUT"])
def rewrite_api_template_put(name: str):
    """Полное сохранение шаблона: имя, описание, промты, TTS."""
    old_name = str(name or "").strip()
    body = request.get_json(silent=True) or {}
    new_name = allocate_rewrite_template_name(body.get("name") or old_name, exclude=old_name)
    if old_name.lower() == "base template" and new_name.lower() != "base template":
        return jsonify(
            {"ok": False, "error": "protected", "message": "Base Template нельзя переименовать."}
        ), 400
    if new_name != old_name:
        err = rename_rewrite_template_dir(old_name, new_name)
        if err:
            code = 409 if "уже существует" in err else 400
            return jsonify({"ok": False, "error": "rename_failed", "message": err}), code
        name_key = new_name
    else:
        name_key = old_name
    known = set(list_rewrite_template_names())
    if name_key not in known:
        return jsonify({"ok": False, "error": "not_found"}), 404
    existing = load_rewrite_template(name_key) or {}
    stages_in = body.get("stages")
    if not isinstance(stages_in, dict):
        stages_in = {}
    stages: dict[str, Any] = {}
    base_stages = existing.get("stages") if isinstance(existing.get("stages"), dict) else {}
    for sk in REWRITE_TEMPLATE_SCOPE_STAGE_KEYS:
        cell = base_stages.get(sk) if isinstance(base_stages.get(sk), dict) else {}
        merged = {
            "prompt": str(cell.get("prompt") or ""),
            "user_prompt": str(cell.get("user_prompt") or ""),
            "style_prompt": str(cell.get("style_prompt") or ""),
            "past_prompt": str(cell.get("past_prompt") or ""),
        }
        if sk in stages_in and isinstance(stages_in.get(sk), dict):
            inc = stages_in[sk]
            for fld in ("prompt", "user_prompt", "style_prompt", "past_prompt"):
                if fld in inc:
                    merged[fld] = str(inc.get(fld) or "")
        stages[sk] = merged
    tts_defaults = body.get("tts_defaults")
    if not isinstance(tts_defaults, dict):
        tts_defaults = None
    ok, err = save_rewrite_template_to_disk(
        name_key,
        hero_prompt=str(body.get("hero_prompt") or ""),
        master_prompt=str(body.get("master_prompt") or ""),
        target_chars=None,
        stages=stages,
        description=str(body.get("description") or ""),
        tts_defaults=tts_defaults,
    )
    if not ok:
        return jsonify({"ok": False, "error": err or "save_failed"}), 400
    data = load_rewrite_template(name_key)
    if data is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = dict(data)
    data["logo_url"] = _rewrite_template_logo_url(name_key)
    return jsonify({"ok": True, "name": name_key, **data})


@app.route("/rewrite/api/templates/<path:name>/logo", methods=["POST"])
def rewrite_api_template_upload_logo(name: str):
    nn = str(name or "").strip()
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, nn)
    if d is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Файл логотипа не передан."}), 400
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "Пустой файл."}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".png"
    ok, err = save_rewrite_template_logo(nn, data, ext)
    if not ok:
        return jsonify({"ok": False, "error": err or "write_failed"}), 500
    return jsonify(
        {
            "ok": True,
            "logo_url": _rewrite_template_logo_url(nn),
        }
    )


@app.route("/rewrite/api/templates/<name>/save", methods=["POST"])
def rewrite_api_template_save(name: str):
    """Сохранить текущие поля промптов и Config в подпапку rewrite_templates/<name>/."""
    body = request.get_json(silent=True) or {}
    known = set(list_rewrite_template_names())
    if name.strip() not in known:
        return jsonify({"ok": False, "error": "not_found"}), 404
    stages = filter_stages_for_template_scope(body.get("stages"))
    ok, err = save_rewrite_template_to_disk(
        name.strip(),
        hero_prompt=str(body.get("hero_prompt") or ""),
        master_prompt=str(body.get("master_prompt") or ""),
        target_chars=None,
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
    """Legacy route. После слияния делегирует на единое удаление проекта."""
    return delete_job(rewrite_id)


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
    if "rewrite_pipeline_language" in body:
        rw["rewrite_pipeline_language"] = normalize_rewrite_pipeline_language(
            body.get("rewrite_pipeline_language")
        )
    if "russian_semantic_model" in body:
        rsm_raw = str(body.get("russian_semantic_model") or "").strip()
        rw["russian_semantic_model"] = normalize_rewrite_model(rsm_raw) if rsm_raw else ""
    merge_stages_from_request(rw, body.get("stages"))
    st_after = rw.get("stages")
    if isinstance(st_after, dict):
        vo_cell = st_after.get("voiceover_editor")
        if isinstance(vo_cell, dict):
            rw["voiceover_final_text"] = _extract_edited_text(str(vo_cell.get("last_result") or ""))
    if "semantic_text_analysis" in body:
        rw["semantic_text_analysis"] = str(body.get("semantic_text_analysis") or "")
    sa_lock_in = body.get("semantic_text_analysis_locked") if "semantic_text_analysis_locked" in body else None
    if sa_lock_in is not None:
        rw["semantic_text_analysis_locked"] = bool(sa_lock_in)
    if "voiceover_final_semantic_text_analysis" in body:
        rw["voiceover_final_semantic_text_analysis"] = str(
            body.get("voiceover_final_semantic_text_analysis") or ""
        )
    vfsa_lock_in = (
        body.get("voiceover_final_semantic_text_analysis_locked")
        if "voiceover_final_semantic_text_analysis_locked" in body
        else None
    )
    if vfsa_lock_in is not None:
        rw["voiceover_final_semantic_text_analysis_locked"] = bool(vfsa_lock_in)
    if "model" in body:
        rw["model"] = normalize_rewrite_model(str(body.get("model") or ""))
    if "chat_temperature" in body and body.get("chat_temperature") is not None and str(body.get("chat_temperature", "")).strip() != "":
        try:
            rw["chat_temperature"] = clamp_chat_temperature(body.get("chat_temperature"))
        except (TypeError, ValueError):
            pass
    if "last_prompt" in body:
        rw["last_prompt"] = str(body.get("last_prompt") or "")
    if "last_text" in body:
        rw["last_text"] = str(body.get("last_text") or "")
    if "last_result" in body:
        rw["last_result"] = str(body.get("last_result") or "")
    save_rewrite_job(rewrite_id, rw)
    return jsonify({"ok": True})


def _normalize_locked_prompt_route_name(raw: str | None) -> str:
    """Имя промта из path: strip, снять BOM (редкий случай прокси/копипаста)."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff")
    return s


@app.route("/api/locked-prompts", methods=["GET", "POST"])
def api_locked_prompt_query():
    """Тот же locked-promt, но имя только в query `?name=` — стабильнее за nginx/префиксами.

    Путь без динамического сегмента не теряется и не подменяется на плейсхолдеры в прокси.
    """
    name_key = _normalize_locked_prompt_route_name(request.args.get("name"))
    if not name_key:
        return jsonify({"ok": False, "error": "missing_name", "message": "Укажите query-параметр name."}), 400
    if not locked_prompt_is_known(name_key):
        return jsonify({"ok": False, "error": "unknown_prompt", "requested": name_key}), 404
    if request.method == "GET":
        state = locked_prompt_public_state(name_key)
        content = get_locked_prompt(name_key)
        return jsonify({
            "ok": True,
            "name": name_key,
            "label": state.get("label"),
            "present": bool(state.get("present")),
            "content": content,
        })
    body = request.get_json(silent=True) or {}
    pin = body.get("pin")
    if not verify_locked_prompts_pin(pin):
        return jsonify({"ok": False, "error": "bad_pin"}), 401
    content = body.get("content")
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "bad_content"}), 400
    try:
        save_locked_prompt(name_key, content)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write_failed: {e}"}), 500
    state = locked_prompt_public_state(name_key)
    return jsonify({
        "ok": True,
        "name": name_key,
        "label": state.get("label"),
        "present": bool(state.get("present")),
    })


@app.route("/api/locked-prompts/<name>", methods=["GET"])
def api_locked_prompt_get(name: str):
    """Отдать содержимое защищённого промта (без пин-кода).

    Просмотр доступен всем — пин-код требуется только для записи. Это
    осознанный выбор: задача защиты — предотвратить случайную правку,
    а не скрыть содержимое. См. модуль `locked_prompts.py`.
    """
    name_key = _normalize_locked_prompt_route_name(name)
    if not locked_prompt_is_known(name_key):
        return jsonify({"ok": False, "error": "unknown_prompt", "requested": name_key}), 404
    state = locked_prompt_public_state(name_key)
    content = get_locked_prompt(name_key)
    return jsonify({
        "ok": True,
        "name": name_key,
        "label": state.get("label"),
        "present": bool(state.get("present")),
        "content": content,
    })


@app.route("/api/locked-prompts/<name>", methods=["POST"])
def api_locked_prompt_save(name: str):
    """Сохранить защищённый промт. Body: {pin: "1234", content: "…"}.

    pin сверяется с env переменной `LOCKED_PROMPTS_PIN` (дефолт `1234`).
    """
    name_key = _normalize_locked_prompt_route_name(name)
    if not locked_prompt_is_known(name_key):
        return jsonify({"ok": False, "error": "unknown_prompt", "requested": name_key}), 404
    body = request.get_json(silent=True) or {}
    pin = body.get("pin")
    if not verify_locked_prompts_pin(pin):
        return jsonify({"ok": False, "error": "bad_pin"}), 401
    content = body.get("content")
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "bad_content"}), 400
    try:
        save_locked_prompt(name_key, content)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write_failed: {e}"}), 500
    state = locked_prompt_public_state(name_key)
    return jsonify({
        "ok": True,
        "name": name_key,
        "label": state.get("label"),
        "present": bool(state.get("present")),
    })


TRANSLATE_SOURCE_RU_TASK_KIND = "translate_source_ru"
TRANSLATE_SOURCE_RU_TASK_REF_ID = "source"

SEMANTIC_TEXT_ANALYZER_TASK_KIND = "semantic_text_analyzer"
SEMANTIC_TEXT_ANALYZER_TASK_REF_ID = "semantic"

SEMANTIC_TEXT_ANALYZER_VO_FINAL_TASK_KIND = "semantic_text_analyzer_voiceover_final"
SEMANTIC_TEXT_ANALYZER_VO_FINAL_TASK_REF_ID = "voiceover_final_semantic"


def _iter_translate_source_ru_events(
    rewrite_id: str,
    source_text: str,
    model: str,
    *,
    cancel_event: threading.Event | None = None,
    body: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """События перевода (status / error / result) — для NDJSON-стрима и для task_manager."""
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not source_text.strip():
        yield {"type": "error", "message": "Нет текста для перевода."}
        return
    if not api_key_present or not api_key:
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY."}
        return
    batches = _split_text_into_translation_batches(source_text, 5000)
    if not batches:
        yield {"type": "error", "message": "Нет текста для перевода."}
        return
    rw_ph = load_rewrite_job(rewrite_id) or {}
    chat_temp = clamp_chat_temperature(rw_ph.get("chat_temperature"))
    nb = len(batches)
    yield {
        "type": "status",
        "message": f"Разбиение текста на батчи готово: {nb} шт. (≤{_fmt_num_ru(5000)} симв./батч).",
    }
    yield {"type": "status", "message": f"Модель: {model}"}
    parts: list[str] = []
    for bi, chunk in enumerate(batches):
        if cancel_event is not None and cancel_event.is_set():
            yield {"type": "error", "message": "Задача отменена пользователем."}
            return
        tag = f"[Батч {bi + 1}/{nb}, {_fmt_num_ru(len(chunk))} симв.] "
        sys_prompt = rewrite_placeholder_apply_from_request(
            get_locked_prompt("translate_to_ru"), body, rw_ph
        )
        yield {
            "type": "status",
            "message": (
                tag
                + "Старт перевода батча… (System Promt: "
                + _locked_prompt_fingerprint(sys_prompt)
                + ")"
            ),
        }
        user_msg = chunk
        got_result = False
        err_text: str | None = None
        for ev in iter_rewrite_completion(
            api_key,
            model,
            sys_prompt,
            user_msg,
            chat_temperature=chat_temp,
        ):
            etype = str(ev.get("type") or "")
            if etype == "status":
                yield {"type": "status", "message": tag + str(ev.get("message") or "")}
            elif etype == "error":
                err_text = str(ev.get("message") or "Ошибка OpenAI")
                break
            elif etype == "result":
                parts.append(str(ev.get("content") or ""))
                got_result = True
                yield {
                    "type": "status",
                    "message": tag + f"Готово: получено {_fmt_num_ru(len(parts[-1]))} симв.",
                }
        if err_text is not None:
            yield {"type": "error", "message": tag + err_text}
            return
        if not got_result:
            yield {"type": "error", "message": tag + "Пустой ответ модели."}
            return
    combined = "".join(parts).strip()
    rw_save = load_rewrite_job(rewrite_id)
    if rw_save is None:
        yield {"type": "error", "message": "Проект не найден при сохранении перевода."}
        return
    try:
        rw_save["source_text_ru"] = combined
        save_rewrite_job(rewrite_id, rw_save)
        yield {"type": "status", "message": "Сохранено в project.json (поле source_text_ru)."}
    except Exception as e:
        yield {"type": "status", "message": f"Не удалось сохранить project.json: {e}"}
    yield {"type": "result", "content": combined, "batches": nb, "chars": len(combined)}


def _translate_source_ru_task_target(
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
    request_payload: dict[str, Any],
) -> None:
    """Фоновый target для task_manager: перевод source → RU."""
    rewrite_id = str(request_payload.get("rewrite_id") or "").strip()
    source_text = str(request_payload.get("source_text") or "")
    model = str(request_payload.get("model") or "")
    ph = request_payload.get("placeholder_request_body")
    ph_body = ph if isinstance(ph, dict) else {}
    for ev in _iter_translate_source_ru_events(
        rewrite_id, source_text, model, cancel_event=cancel_event, body=ph_body
    ):
        emit(ev)


@app.route("/rewrite/<rewrite_id>/translate-source-ru/start", methods=["POST"])
def rewrite_translate_source_ru_start(rewrite_id: str):
    """Браузер-независимый запуск перевода source → RU (фон + task_manager)."""
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    source_text = str(body.get("source_text") if "source_text" in body else rw.get("source_text") or "")
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    stages = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    ana = stages.get("analysis") if isinstance(stages.get("analysis"), dict) else {}
    model = normalize_rewrite_model(
        str(body.get("model") or rw.get("russian_semantic_model") or "")
    )
    if not source_text.strip():
        return jsonify({"ok": False, "error": "no_source_text", "message": "Нет текста для перевода."}), 400
    if not api_key_present or not (os.getenv("OPENAI_API_KEY") or "").strip():
        return jsonify({"ok": False, "error": "no_api_key", "message": "Не задан OPENAI_API_KEY."}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    meta = _tm_start_task(
        proj_dir,
        kind=TRANSLATE_SOURCE_RU_TASK_KIND,
        ref_id=TRANSLATE_SOURCE_RU_TASK_REF_ID,
        target=_translate_source_ru_task_target,
        request_payload={
            "rewrite_id": rewrite_id,
            "source_text": source_text,
            "model": model,
            "placeholder_request_body": body,
        },
        reuse_active=True,
    )
    return jsonify({"ok": True, "task": meta})


@app.route("/rewrite/<rewrite_id>/translate-source-ru", methods=["POST"])
def rewrite_translate_source_ru(rewrite_id: str):
    """Перевод исходного текста на русский (батчи ~5000 симв., OpenAI). Ответ — NDJSON стрим.

    Оставлен для обратной совместимости; UI переведён на `/translate-source-ru/start` + `/tasks/.../events`.
    """
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    source_text = str(body.get("source_text") if "source_text" in body else rw.get("source_text") or "")
    stages = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    ana = stages.get("analysis") if isinstance(stages.get("analysis"), dict) else {}
    model = normalize_rewrite_model(str(body.get("model") or rw.get("model") or ana.get("model") or ""))

    def gen():
        for ev in _iter_translate_source_ru_events(
            rewrite_id, source_text, model, cancel_event=None, body=body
        ):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return Response(stream_with_context(gen()), mimetype="application/x-ndjson")


def _iter_semantic_text_analyzer_events(
    rewrite_id: str,
    src_ru: str,
    model: str,
    *,
    cancel_event: threading.Event | None = None,
    body: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Semantic Text Analyzer: события для NDJSON и task_manager."""
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not src_ru.strip():
        yield {
            "type": "error",
            "message": "Нет русского текста (source_text_ru). Сначала запустите «Перевести на русский».",
        }
        return
    if not api_key_present or not api_key:
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY."}
        return
    if cancel_event is not None and cancel_event.is_set():
        yield {"type": "error", "message": "Задача отменена пользователем."}
        return
    rw_ph = load_rewrite_job(rewrite_id) or {}
    chat_temp = clamp_chat_temperature(rw_ph.get("chat_temperature"))
    system_prompt = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_system"), body, rw_ph
    )
    user_template = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_user"), body, rw_ph
    )
    user_msg = (user_template or "").rstrip() + "\n\n" + src_ru.strip()
    yield {"type": "status", "message": f"Модель: {model}; вход: {_fmt_num_ru(len(src_ru))} симв."}
    yield {
        "type": "status",
        "message": (
            "Старт запроса… (System Promt: "
            + _locked_prompt_fingerprint(system_prompt)
            + "; User Promt: "
            + _locked_prompt_fingerprint(user_template)
            + ")"
        ),
    }
    got_result = False
    err_text: str | None = None
    result_text = ""
    for ev in iter_rewrite_completion(api_key, model, system_prompt, user_msg, chat_temperature=chat_temp):
        if cancel_event is not None and cancel_event.is_set():
            yield {"type": "error", "message": "Задача отменена пользователем."}
            return
        etype = str(ev.get("type") or "")
        if etype == "status":
            yield {"type": "status", "message": str(ev.get("message") or "")}
        elif etype == "error":
            err_text = str(ev.get("message") or "Ошибка OpenAI")
            break
        elif etype == "result":
            result_text = str(ev.get("content") or "").strip()
            got_result = True
    if err_text is not None:
        yield {"type": "error", "message": err_text}
        return
    if not got_result or not result_text:
        yield {"type": "error", "message": "Пустой ответ модели."}
        return
    rw_save = load_rewrite_job(rewrite_id)
    if rw_save is None:
        yield {"type": "error", "message": "Проект не найден при сохранении анализа."}
        return
    try:
        rw_save["semantic_text_analysis"] = result_text
        rw_save["semantic_text_analysis_at"] = datetime.now(timezone.utc).isoformat()
        save_rewrite_job(rewrite_id, rw_save)
        yield {"type": "status", "message": "Сохранено в project.json (поле semantic_text_analysis)."}
    except Exception as e:
        yield {"type": "status", "message": f"Не удалось сохранить project.json: {e}"}
    yield {"type": "result", "content": result_text, "chars": len(result_text)}


def _iter_semantic_voiceover_final_events(
    rewrite_id: str,
    vf_text_ru: str,
    model: str,
    *,
    cancel_event: threading.Event | None = None,
    body: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Semantic по русскому переводу итога озвучки (voiceover_final_text_ru)."""
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not vf_text_ru.strip():
        yield {
            "type": "error",
            "message": (
                "Нет русского перевода итога озвучки. Сначала нажмите ↻ у Russian "
                "в блоке «Итоговый текст»."
            ),
        }
        return
    if not api_key_present or not api_key:
        yield {"type": "error", "message": "Не задан OPENAI_API_KEY."}
        return
    if cancel_event is not None and cancel_event.is_set():
        yield {"type": "error", "message": "Задача отменена пользователем."}
        return
    rw_ph = load_rewrite_job(rewrite_id) or {}
    chat_temp = clamp_chat_temperature(rw_ph.get("chat_temperature"))
    system_prompt = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_system"), body, rw_ph
    )
    user_template = rewrite_placeholder_apply_from_request(
        get_locked_prompt("semantic_text_analyzer_user"), body, rw_ph
    )
    user_msg = (user_template or "").rstrip() + "\n\n" + vf_text_ru.strip()
    yield {"type": "status", "message": f"Модель: {model}; вход (итог RU): {_fmt_num_ru(len(vf_text_ru))} симв."}
    yield {
        "type": "status",
        "message": (
            "Старт Semantic по итогу… (System Promt: "
            + _locked_prompt_fingerprint(system_prompt)
            + "; User Promt: "
            + _locked_prompt_fingerprint(user_template)
            + ")"
        ),
    }
    got_result = False
    err_text: str | None = None
    result_text = ""
    for ev in iter_rewrite_completion(api_key, model, system_prompt, user_msg, chat_temperature=chat_temp):
        if cancel_event is not None and cancel_event.is_set():
            yield {"type": "error", "message": "Задача отменена пользователем."}
            return
        etype = str(ev.get("type") or "")
        if etype == "status":
            yield {"type": "status", "message": str(ev.get("message") or "")}
        elif etype == "error":
            err_text = str(ev.get("message") or "Ошибка OpenAI")
            break
        elif etype == "result":
            result_text = str(ev.get("content") or "").strip()
            got_result = True
    if err_text is not None:
        yield {"type": "error", "message": err_text}
        return
    if not got_result or not result_text:
        yield {"type": "error", "message": "Пустой ответ модели."}
        return
    rw_save = load_rewrite_job(rewrite_id)
    if rw_save is None:
        yield {"type": "error", "message": "Проект не найден при сохранении анализа."}
        return
    try:
        rw_save["voiceover_final_semantic_text_analysis"] = result_text
        rw_save["voiceover_final_semantic_text_analysis_at"] = datetime.now(timezone.utc).isoformat()
        save_rewrite_job(rewrite_id, rw_save)
        yield {
            "type": "status",
            "message": "Сохранено в project.json (поле voiceover_final_semantic_text_analysis).",
        }
    except Exception as e:
        yield {"type": "status", "message": f"Не удалось сохранить project.json: {e}"}
    yield {"type": "result", "content": result_text, "chars": len(result_text)}


def _semantic_text_analyzer_task_target(
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
    request_payload: dict[str, Any],
) -> None:
    rewrite_id = str(request_payload.get("rewrite_id") or "").strip()
    src_ru = str(request_payload.get("source_text_ru") or "")
    model = str(request_payload.get("model") or "")
    ph = request_payload.get("placeholder_request_body")
    ph_body = ph if isinstance(ph, dict) else {}
    for ev in _iter_semantic_text_analyzer_events(
        rewrite_id, src_ru, model, cancel_event=cancel_event, body=ph_body
    ):
        emit(ev)


def _semantic_voiceover_final_task_target(
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
    request_payload: dict[str, Any],
) -> None:
    rewrite_id = str(request_payload.get("rewrite_id") or "").strip()
    vf_ru = str(request_payload.get("voiceover_final_text_ru") or "")
    model = str(request_payload.get("model") or "")
    ph = request_payload.get("placeholder_request_body")
    ph_body = ph if isinstance(ph, dict) else {}
    for ev in _iter_semantic_voiceover_final_events(
        rewrite_id, vf_ru, model, cancel_event=cancel_event, body=ph_body
    ):
        emit(ev)


@app.route("/rewrite/<rewrite_id>/semantic-text-analyzer/start", methods=["POST"])
def rewrite_semantic_text_analyzer_start(rewrite_id: str):
    """Фоновый запуск Semantic Text Analyzer (task_manager)."""
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    src_ru = str(body.get("source_text_ru") if "source_text_ru" in body else rw.get("source_text_ru") or "")
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    stages = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    ana = stages.get("analysis") if isinstance(stages.get("analysis"), dict) else {}
    model = normalize_rewrite_model(
        str(body.get("model") or rw.get("russian_semantic_model") or "")
    )
    if not src_ru.strip():
        return jsonify(
            {
                "ok": False,
                "error": "no_source_text_ru",
                "message": "Нет русского текста (source_text_ru). Сначала запустите «Перевести на русский».",
            }
        ), 400
    if not api_key_present or not (os.getenv("OPENAI_API_KEY") or "").strip():
        return jsonify({"ok": False, "error": "no_api_key", "message": "Не задан OPENAI_API_KEY."}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    meta = _tm_start_task(
        proj_dir,
        kind=SEMANTIC_TEXT_ANALYZER_TASK_KIND,
        ref_id=SEMANTIC_TEXT_ANALYZER_TASK_REF_ID,
        target=_semantic_text_analyzer_task_target,
        request_payload={
            "rewrite_id": rewrite_id,
            "source_text_ru": src_ru,
            "model": model,
            "placeholder_request_body": body,
        },
        reuse_active=True,
    )
    return jsonify({"ok": True, "task": meta})


@app.route("/rewrite/<rewrite_id>/semantic-text-analyzer", methods=["POST"])
def rewrite_semantic_text_analyzer(rewrite_id: str):
    """Semantic Text Analyzer: NDJSON-стрим (легаси). UI использует `/semantic-text-analyzer/start`."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    src_ru = str(body.get("source_text_ru") if "source_text_ru" in body else rw.get("source_text_ru") or "")
    stages = rw.get("stages") if isinstance(rw.get("stages"), dict) else {}
    ana = stages.get("analysis") if isinstance(stages.get("analysis"), dict) else {}
    model = normalize_rewrite_model(str(body.get("model") or rw.get("model") or ana.get("model") or ""))

    def gen():
        for ev in _iter_semantic_text_analyzer_events(
            rewrite_id, src_ru, model, cancel_event=None, body=body
        ):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return Response(stream_with_context(gen()), mimetype="application/x-ndjson")


@app.route("/rewrite/<rewrite_id>/semantic-voiceover-final/start", methods=["POST"])
def rewrite_semantic_voiceover_final_start(rewrite_id: str):
    """Фоновый Semantic по русскому переводу итога озвучки (отдельно от source)."""
    if not rewrite_id_ok(rewrite_id):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    vf_ru = str(
        body.get("voiceover_final_text_ru")
        if "voiceover_final_text_ru" in body
        else rw.get("voiceover_final_text_ru") or ""
    )
    api_key_present = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    model = normalize_rewrite_model(
        str(body.get("model") or rw.get("russian_semantic_model") or "")
    )
    if not vf_ru.strip():
        return jsonify(
            {
                "ok": False,
                "error": "no_voiceover_final_text_ru",
                "message": (
                    "Нет русского перевода итога озвучки. Сначала нажмите ↻ у Russian "
                    "в блоке «Итоговый текст»."
                ),
            }
        ), 400
    if not api_key_present or not (os.getenv("OPENAI_API_KEY") or "").strip():
        return jsonify({"ok": False, "error": "no_api_key", "message": "Не задан OPENAI_API_KEY."}), 400
    proj_dir = _rewrite_project_dir(rewrite_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    meta = _tm_start_task(
        proj_dir,
        kind=SEMANTIC_TEXT_ANALYZER_VO_FINAL_TASK_KIND,
        ref_id=SEMANTIC_TEXT_ANALYZER_VO_FINAL_TASK_REF_ID,
        target=_semantic_voiceover_final_task_target,
        request_payload={
            "rewrite_id": rewrite_id,
            "voiceover_final_text_ru": vf_ru,
            "model": model,
            "placeholder_request_body": body,
        },
        reuse_active=True,
    )
    return jsonify({"ok": True, "task": meta})


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
        chat_temp = clamp_chat_temperature(rw.get("chat_temperature"))
        nb = len(batches)
        yield json.dumps(
            {
                "type": "status",
                "message": f"Разбиение текста на батчи готово: {nb} шт. (≤{_fmt_num_ru(5000)} симв./батч).",
            },
            ensure_ascii=False,
        ) + "\n"
        yield json.dumps({"type": "status", "message": f"Модель: {model}"}, ensure_ascii=False) + "\n"
        parts: list[str] = []
        for bi, chunk in enumerate(batches):
            tag = f"[Батч {bi + 1}/{nb}, {_fmt_num_ru(len(chunk))} симв.] "
            sys_prompt = rewrite_placeholder_apply_from_request(
                get_locked_prompt("translate_to_ru"), body, rw
            )
            yield json.dumps(
                {
                    "type": "status",
                    "message": (
                        tag
                        + "Старт перевода батча… (System Promt: "
                        + _locked_prompt_fingerprint(sys_prompt)
                        + ")"
                    ),
                },
                ensure_ascii=False,
            ) + "\n"
            user_msg = chunk
            got_result = False
            err_text: str | None = None
            for ev in iter_rewrite_completion(
                api_key,
                model,
                sys_prompt,
                user_msg,
                chat_temperature=chat_temp,
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
                            "message": tag + f"Готово: получено {_fmt_num_ru(len(parts[-1]))} симв.",
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
                        **_youtube_ytdlp_perf_opts(),
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
    channel_meta = _youtube_enrich_channel_meta(rw, info=info)
    if prev_url != url:
        rw["youtube_audio_file"] = ""
        rw["youtube_transcript_text"] = ""
        rw["youtube_transcript_url"] = ""
        rw["youtube_processing"] = False
        rw["youtube_phase"] = ""
        rw["youtube_status"] = ""
    save_rewrite_job(rewrite_id, rw)
    return jsonify({"ok": True, "youtube_title": title, **channel_meta})


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


@app.route("/rewrite/<rewrite_id>/youtube/proxy/status", methods=["GET"])
def rewrite_youtube_proxy_status(rewrite_id: str):
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, **youtube_proxy_status_dict()})


@app.route("/rewrite/<rewrite_id>/youtube/proxy", methods=["POST"])
def rewrite_youtube_proxy_save(rewrite_id: str):
    """Сохраняет прокси для yt-dlp в ``data/secrets/yt_dlp_proxy.json`` и проверяет запросом к YouTube."""
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    raw = body.get("proxy")
    if raw is None:
        raw = ""
    raw = str(raw).strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    if not raw:
        cfg = _youtube_proxy_default_config()
        cfg["updated_at"] = now_iso
        cfg["last_test_ok"] = None
        cfg["last_test_at"] = None
        cfg["last_test_message"] = "Файл прокси очищен (используется только .env, если задан)."
        _youtube_proxy_save(cfg)
        return jsonify({"ok": True, "cleared": True, "test_ok": None, **youtube_proxy_status_dict()})
    try:
        norm = _youtube_proxy_normalize(raw)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    ok, msg = _youtube_proxy_run_test(norm)
    cfg = _youtube_proxy_load()
    cfg["proxy_url"] = norm
    cfg["proxy_input"] = raw
    cfg["updated_at"] = now_iso
    cfg["last_test_ok"] = ok
    cfg["last_test_at"] = now_iso
    cfg["last_test_message"] = msg
    _youtube_proxy_save(cfg)
    return jsonify({"ok": True, "cleared": False, "test_ok": ok, "test_message": msg, **youtube_proxy_status_dict()})


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
                    f"YouTube {ci + 1}/{n_cli}: «{cname}», вариант аудио {fi + 1}/{n_fmt}…"
                )
            same_re = _youtube_same_client_retries()
            ydl_opts: dict = {
                **_YOUTUBE_YDL_BASE,
                **_youtube_ytdlp_perf_opts(),
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
                                f"Формат недоступен («{cname}»). Подобран id={dynamic_format_id}, повтор…"
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
                _rewrite_youtube_clear_partial_downloads(media_dir)
                if has_next_format:
                    if status_callback is not None:
                        status_callback(f"«{cname}»: пробуем другой формат…")
                    continue
                if has_next_client:
                    if status_callback is not None:
                        status_callback(f"«{cname}» не подошёл, следующий клиент…")
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
        status="Скачивание аудио с YouTube…",
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
                persist_runtime_status(f"Скачивание аудио: {_fmt_num_ru(int(got or 0))} байт…")
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
                    "message": "Скачивание аудио с YouTube…",
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
                        "Готово - "
                        f"{_fmt_num_ru(len(result_holder['text']))} символов · "
                        f"{_fmt_num_ru(len(result_holder['text'].split()))} слов"
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


@app.route("/rewrite/<rewrite_id>/youtube/state", methods=["POST"])
def rewrite_youtube_state_save(rewrite_id: str):
    """Сохранить серверно-видимый статус YouTube-блока. Используется клиентом,
    когда «Остановлено» (или другое финальное сообщение) формируется на стороне
    браузера после AbortController.abort() — чтобы после F5 пользователь увидел
    то же самое сообщение, а не последнюю запись из фонового потока.

    ``clear_preview``: сбросить превью канала/названия (новый запуск «Расшифровать»)."""
    rw = load_rewrite_job(rewrite_id)
    if rw is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    if body.get("clear_preview"):
        rw["youtube_channel"] = ""
        rw["youtube_channel_id"] = ""
        rw["youtube_channel_url"] = ""
        rw["youtube_channel_avatar"] = ""
        rw["youtube_title"] = ""
    if "youtube_processing" in body:
        rw["youtube_processing"] = bool(body.get("youtube_processing"))
    elif "youtube_status" in body or "youtube_phase" in body:
        rw["youtube_processing"] = False
    if "youtube_status" in body:
        rw["youtube_status"] = str(body.get("youtube_status") or "").strip()
    if "youtube_phase" in body:
        rw["youtube_phase"] = str(body.get("youtube_phase") or "").strip()
    save_rewrite_job(rewrite_id, rw)
    return jsonify({"ok": True})


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
    hero_prompt, target_chars, duration_minutes, chars_per_minute, chat_temp = snapshot_pipeline_extras_from_body(body)
    preset = snapshot_rewrite_preset_from_body(body, rw_job)
    api_key = os.getenv("OPENAI_API_KEY") or ""

    # Пресет «Я уже ЗАrewriteИЛ»: подтягиваем inbox.last_result из JSON проекта.
    if (
        preset == REWRITE_PRESET_PREWRITTEN
        and stage_key in ("voiceover_editor", "elevenlabs_editor", "title_strategist", "structure_splitter")
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
    # «Мягкий Rewrite»: те же три агента, но без Inbox — подтягиваем только Rewrite.Result с диска.
    if (
        preset == REWRITE_PRESET_SOFT
        and stage_key in ("voiceover_editor", "elevenlabs_editor", "title_strategist", "structure_splitter")
        and isinstance(stages_snap, dict)
    ):
        _snap_s = dict(stages_snap)
        _rw_cell_s = dict(_snap_s.get("rewrite") or {}) if isinstance(_snap_s.get("rewrite"), dict) else {}
        _rw_res_s = str(_rw_cell_s.get("last_result") or "").strip()
        if not _rw_res_s:
            _rw_res_s = str(((rw_job.get("stages") or {}).get("rewrite") or {}).get("last_result") or "").strip()
        if _rw_res_s:
            _rw_cell_s["last_result"] = _rw_res_s
            _snap_s["rewrite"] = _rw_cell_s
        stages_snap = _snap_s
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
            "persona_editor",
            "voiceover_editor",
            "elevenlabs_editor",
            "title_strategist",
            "structure_splitter",
            "scene_writer",
            "youtube_packaging",
            "rewrite",
        ) and not (source_text or "").strip():
            yield json.dumps(
                {"type": "error", "message": "Введите исходный текст в верхнем поле."},
                ensure_ascii=False,
            ) + "\n"
            return
        block_writer_full_text = ""
        if stage_key == "retention_editor":
            full_text_path = _rewrite_block_writer_dir(rewrite_id) / "full_text.txt"
            if full_text_path.exists():
                try:
                    block_writer_full_text = normalize_model_plain_text(
                        full_text_path.read_text(encoding="utf-8")
                    )
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
        if stage_key == "persona_editor":
            p = _rewrite_stage_result_path(rewrite_id, "hook_editor")
            if p.exists():
                try:
                    hook_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    hook_editor_text = ""
        persona_editor_text = ""
        if stage_key == "voiceover_editor":
            p = _rewrite_stage_result_path(rewrite_id, "persona_editor")
            if p.exists():
                try:
                    persona_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    persona_editor_text = ""
        voiceover_editor_text = ""
        elevenlabs_editor_text = ""
        if stage_key in ("elevenlabs_editor", "title_strategist", "structure_splitter"):
            voiceover_editor_text = _extract_voiceover_plain_text(
                _rewrite_stage_last_result_text(rewrite_id, stages_snap, "voiceover_editor")
            )
            if not voiceover_editor_text.strip():
                voiceover_editor_text = downstream_script_input_text(
                    preset,
                    stages_snap,
                    source_text=source_text,
                )
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
            persona_editor_text=persona_editor_text,
            voiceover_editor_text=voiceover_editor_text,
            elevenlabs_editor_text=elevenlabs_editor_text,
            structure_splitter_text=structure_splitter_text,
            title_strategist_result_text=title_strategist_result_text,
            original_title=original_title,
            preset=preset,
            pipeline_language=snapshot_rewrite_pipeline_language_from_body(body, rw_job),
            chat_temperature=chat_temp,
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
            d1_cell = stages_snap.get("draft1") or {}
            if not isinstance(d1_cell, dict):
                d1_cell = {}
            block_writer_user_prompt = rewrite_placeholder_apply_from_request(
                _stage_user_prompt_text("draft1", d1_cell),
                body,
                rw_job,
            )
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
                block_writer_user_prompt=block_writer_user_prompt,
                on_block_completed=on_block_completed,
                on_all_completed=on_all_completed,
                chat_temperature=chat_temp,
            ):
                yield json.dumps(item, ensure_ascii=False) + "\n"
        elif stage_key == "elevenlabs_editor":
            el_result = ""
            for item in iter_rewrite_completion(api_key, model, prompt, user_text, chat_temperature=chat_temp):
                t = str(item.get("type") or "")
                if t == "result":
                    el_result = str(item.get("content") or "")
                elif t == "error":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    return
                else:
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            yield json.dumps(
                build_elevenlabs_editor_check(voiceover_editor_text, el_result),
                ensure_ascii=False,
            ) + "\n"
            yield json.dumps({"type": "result", "content": el_result}, ensure_ascii=False) + "\n"
        elif stage_key == "structure_splitter":
            split_result = ""
            ss_check_in = _structure_splitter_check_input_text(
                rewrite_id=rewrite_id,
                stages_snap=stages_snap,
                voiceover_plain=voiceover_editor_text,
            )
            for item in iter_rewrite_completion(api_key, model, prompt, user_text, chat_temperature=chat_temp):
                t = str(item.get("type") or "")
                if t == "result":
                    split_result = str(item.get("content") or "")
                elif t == "error":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    return
                else:
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            yield json.dumps(
                _build_structure_splitter_check(ss_check_in, split_result),
                ensure_ascii=False,
            ) + "\n"
            yield json.dumps({"type": "result", "content": split_result}, ensure_ascii=False) + "\n"
        elif stage_key == "scene_writer":
            raw_blocks = str(structure_splitter_text or "").strip()
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
                for item in iter_rewrite_completion(api_key, model, prompt, joined_user, chat_temperature=chat_temp):
                    t = str(item.get("type") or "")
                    if t == "result":
                        part = str(item.get("content") or "").strip()
                    elif t == "error":
                        err = str(item.get("message") or "Ошибка Scene Writer.")
                        yield json.dumps({"type": "error", "message": f"Блок {i}/{total}: {err}"}, ensure_ascii=False) + "\n"
                        return
                    elif t == "status":
                        yield json.dumps({"type": "status", "message": f"[{i}/{total}] {str(item.get('message') or '')}"}, ensure_ascii=False) + "\n"
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
        elif stage_key == "rewrite":
            for item in iter_rewrite_completion_stream(
                api_key,
                model,
                prompt,
                user_text,
                chat_temperature=chat_temp,
                content_stream_terminator=REWRITE_STREAM_USER_TERMINATOR,
            ):
                t_item = str(item.get("type") or "")
                if t_item == "error":
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    return
                if t_item == "result" and isinstance(item.get("content"), str):
                    item = dict(item)
                    raw_c = str(item.get("content") or "")
                    item["content"] = scrub_rewrite_end_markers(strip_markdown_code_fence(raw_c))
                yield json.dumps(item, ensure_ascii=False) + "\n"
        else:
            for item in iter_rewrite_completion(api_key, model, prompt, user_text, chat_temperature=chat_temp):
                t_item = str(item.get("type") or "")
                if t_item == "result" and isinstance(item.get("content"), str):
                    item = dict(item)
                    item["content"] = strip_markdown_code_fence(str(item.get("content") or ""))
                    item["content"] = _normalize_stage_json_result(
                        stage_key, str(item.get("content") or "")
                    )
                    item["content"] = _normalize_stage_plain_result(
                        stage_key, str(item.get("content") or "")
                    )
                    if stage_key in (
                        "retention_editor",
                        "hook_editor",
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
            _content = scrub_rewrite_end_markers(_stripped) if stage_key == "rewrite" else _stripped
            _content = _normalize_stage_json_result(stage_key, _content)
            _content = _normalize_stage_plain_result(stage_key, _content)
            if _content != _orig:
                _ev = dict(_ev)
                _ev["content"] = _content
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
    if stage_key == "translate_source_ru":
        txt = _export_wire_payloads_translate_source_ru(body, rw_job)
        fname = f"{rewrite_id}_translate_source_ru_openai_request.json"
        resp = make_response(txt)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    if stage_key == "translate_voiceover_final_ru":
        txt = _export_wire_payloads_translate_voiceover_final_ru(body, rw_job)
        fname = f"{rewrite_id}_translate_voiceover_final_ru_openai_request.json"
        resp = make_response(txt)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    if stage_key == "semantic_text_analyzer":
        txt = _export_wire_payload_semantic_text_analyzer(body, rw_job)
        fname = f"{rewrite_id}_semantic_text_analyzer_openai_request.json"
        resp = make_response(txt)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    if stage_key == "semantic_text_analyzer_voiceover_final":
        txt = _export_wire_payload_semantic_voiceover_final(body, rw_job)
        fname = f"{rewrite_id}_semantic_text_analyzer_voiceover_final_openai_request.json"
        resp = make_response(txt)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    source_text, stages_snap = snapshot_stages_from_body(body)
    master_prompt = snapshot_master_prompt_from_body(body)
    hero_prompt, target_chars, duration_minutes, chars_per_minute, chat_temp = snapshot_pipeline_extras_from_body(body)
    block_writer_full_text = ""
    if stage_key == "retention_editor":
        full_text_path = _rewrite_block_writer_dir(rewrite_id) / "full_text.txt"
        if full_text_path.exists():
            try:
                block_writer_full_text = normalize_model_plain_text(
                    full_text_path.read_text(encoding="utf-8")
                )
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
    if stage_key == "persona_editor":
        p = _rewrite_stage_result_path(rewrite_id, "hook_editor")
        if p.exists():
            try:
                hook_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                hook_editor_text = ""
    persona_editor_text = ""
    if stage_key == "voiceover_editor":
        p = _rewrite_stage_result_path(rewrite_id, "persona_editor")
        if p.exists():
            try:
                persona_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                persona_editor_text = ""
    voiceover_editor_text = ""
    elevenlabs_editor_text = ""
    if stage_key in ("elevenlabs_editor", "title_strategist", "structure_splitter"):
        voiceover_editor_text = _extract_voiceover_plain_text(
            _rewrite_stage_last_result_text(rewrite_id, stages_snap, "voiceover_editor")
        )
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
    preset_ap = snapshot_rewrite_preset_from_body(body, rw_job)
    # api-payload в пресете «Я уже ЗАrewriteИЛ»: подтягиваем inbox.last_result из JSON.
    if (
        preset_ap == REWRITE_PRESET_PREWRITTEN
        and stage_key in ("voiceover_editor", "elevenlabs_editor", "title_strategist", "structure_splitter")
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
    if (
        preset_ap == REWRITE_PRESET_SOFT
        and stage_key in ("voiceover_editor", "elevenlabs_editor", "title_strategist", "structure_splitter")
        and isinstance(stages_snap, dict)
    ):
        _snap_sap = dict(stages_snap)
        _rw_cell_sap = dict(_snap_sap.get("rewrite") or {}) if isinstance(_snap_sap.get("rewrite"), dict) else {}
        _rw_res_sap = str(_rw_cell_sap.get("last_result") or "").strip()
        if not _rw_res_sap:
            _rw_res_sap = str(((rw_job.get("stages") or {}).get("rewrite") or {}).get("last_result") or "").strip()
        if _rw_res_sap:
            _rw_cell_sap["last_result"] = _rw_res_sap
            _snap_sap["rewrite"] = _rw_cell_sap
        stages_snap = _snap_sap
    if stage_key in ("elevenlabs_editor", "title_strategist", "structure_splitter"):
        if not voiceover_editor_text.strip():
            voiceover_editor_text = downstream_script_input_text(
                preset_ap,
                stages_snap,
                source_text=source_text,
            )
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
        persona_editor_text=persona_editor_text,
        voiceover_editor_text=voiceover_editor_text,
        elevenlabs_editor_text=elevenlabs_editor_text,
        structure_splitter_text=structure_splitter_text,
        title_strategist_result_text=title_strategist_result_text,
        original_title=original_title,
        preset=preset_ap,
        pipeline_language=snapshot_rewrite_pipeline_language_from_body(body, rw_job),
        chat_temperature=chat_temp,
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
    chat_temp_export = clamp_chat_temperature(rw_job.get("chat_temperature"))

    if stage_key == "draft1":
        structure_raw = str((stages_snap.get("structure") or {}).get("last_result") or "").strip()
        d1_cell = stages_snap.get("draft1") or {}
        if not isinstance(d1_cell, dict):
            d1_cell = {}
        block_writer_user_prompt = rewrite_placeholder_apply_from_request(
            _stage_user_prompt_text("draft1", d1_cell),
            body,
            rw_job,
        )
        saved = _load_block_writer_saved_short_summaries(rewrite_id)
        wire_bodies, ctx_exact = list_draft1_wire_chat_payloads_for_export(
            model_m,
            sys_c,
            structure_raw,
            block_writer_user_prompt,
            saved,
            chat_temperature=chat_temp_export,
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
            wire_bodies_sw.append(rewrite_chat_completion_wire_payload(model_m, sys_c, joined_user, chat_temperature=chat_temp_export))
        if not wire_bodies_sw:
            txt = _format_openai_wire_payloads_txt(
                [],
                header_lines=["[Scene Writer] Нет блоков из Structure Splitter — POST не формируется."],
            )
        else:
            txt = _format_openai_wire_payloads_txt(wire_bodies_sw)
    else:
        txt = _format_openai_wire_payloads_txt([rewrite_chat_completion_wire_payload(model_m, sys_c, usr_c, chat_temperature=chat_temp_export)])
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
        flash("Выбранный шаблон не найден в image_templates/.", "error")
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
        _src_parse = _timings_source_normalize(job.get("apply_timings_source"))
        if _src_parse == "whisper" and not _job_has_words_for_source(job_id, "whisper"):
            _src_parse = "elevenlabs"
        _apply_tts_word_timings_to_scenes(job_id, scenes, source=_src_parse)
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


def normalize_json_script_source(value: str | None) -> str:
    """Источник JSON-кода сцен: scene_writer или manual (ручной ввод)."""
    raw = str(value or "").strip().lower()
    if raw in ("manual", "none", "off", ""):
        return "manual"
    return "scene_writer"


@app.route("/job/<job_id>/timings-source", methods=["POST"])
def job_timings_source_save(job_id: str):
    """Сохранить выбор источника пословных таймингов для JSON сцен (Eleven / Whisper)."""
    body = request.get_json(silent=True) or {}
    source = _timings_source_normalize(body.get("source") if isinstance(body, dict) else None)
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        job["apply_timings_source"] = source
        save_job(job_id, job)
    return jsonify({"ok": True, "source": source})


@app.route("/job/<job_id>/video-json/source", methods=["POST"])
def job_video_json_source_save(job_id: str):
    """Сохранить выбор источника данных для блока «JSON-код сцен»."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if "json_script_source" in body:
            job["json_script_source"] = normalize_json_script_source(body.get("json_script_source"))
        save_job(job_id, job)
    return jsonify({"ok": True, "json_script_source": normalize_json_script_source(job.get("json_script_source"))})


@app.route("/job/<job_id>/scenes/apply-tts-timings", methods=["POST"])
def job_scenes_apply_tts_timings(job_id: str):
    """Пересчитывает audio_timing у сцен по выбранному источнику пословных таймингов.

    Используется кнопкой «Сгенерировать JSON-код сцен с таймингами»: если у
    проекта уже есть сцены и есть пословные тайминги озвучки, повторно
    добавлять сцены не нужно — этот эндпоинт берёт `.words.json`/`.whisper.words.json`,
    выравнивает сцены и записывает audio_timing на месте. Выбранный источник
    сохраняется в `job["apply_timings_source"]`, чтобы пережить рефреш страницы.
    """
    body = request.get_json(silent=True) or {}
    source = _timings_source_normalize(body.get("source") if isinstance(body, dict) else None)
    words_suffix = _words_path_suffix_for_source(source)

    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        scenes = job.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            return jsonify({"ok": False, "error": "В проекте нет сцен."}), 400

        words_doc, audio_fname = _latest_tts_words_doc_for_job(job_id, source=source)
        if not words_doc or not audio_fname:
            src_label = "Whisper (.whisper.words.json)" if source == "whisper" else "ElevenLabs (.words.json)"
            return jsonify(
                {
                    "ok": False,
                    "source": source,
                    "error": (
                        f"У проекта нет пословных таймингов источника {src_label}. "
                        f"Сгенерируйте их соответствующей кнопкой выше."
                    ),
                }
            ), 400

        _apply_tts_word_timings_to_scenes(job_id, scenes, source=source)
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
                    "source": source,
                    "error": (
                        "Не удалось сопоставить тайминги: проверьте, что текст сцен "
                        "совпадает с озвучкой."
                    ),
                    "words_filename": audio_fname.replace(".mp3", words_suffix),
                }
            ), 400

        job["apply_timings_source"] = source
        save_job(job_id, job)
        rendered = _render_scenes_stripped_with_timing(scenes)

    return jsonify(
        {
            "ok": True,
            "source": source,
            "scenes_count": len(scenes),
            "timings_applied": timings_applied,
            "scenes_stripped_text": rendered,
            "audio_filename": audio_fname,
            "words_filename": audio_fname.replace(".mp3", words_suffix),
            "message": (
                f"Тайминги ({'Whisper' if source == 'whisper' else 'ElevenLabs'}) "
                f"сопоставлены: {timings_applied}/{len(scenes)} сцен (озвучка: {audio_fname})."
            ),
        }
    )


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
    """Удаляет одну сцену из job по индексу."""
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

    return jsonify({"ok": True, "scene_id": actual_id})


@app.route("/job/<job_id>/delete", methods=["POST"])
def delete_job(job_id: str):
    """Удаляет проект целиком: и job-данные, и rewrite-данные под тем же ID.

    Удаляются: `data/jobs/<id>.json`, `data/job_audio/<id>`,
    `data/job_remotion/<id>`, `data/rewrite_jobs/<id>`, `data/rewrite_media/<id>`
    и симлинк `remotion/public/jobs/<id>`.
    """
    deleted_any = False
    filepath = JOBS_DIR / f"{job_id}.json"
    if filepath.exists():
        filepath.unlink()
        deleted_any = True
    lock_path = JOBS_DIR / f"{job_id}.json.lock"
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass
    for d in (
        JOB_AUDIO_DIR / job_id,
        BASE_DIR / "data" / "job_remotion" / job_id,
        BASE_DIR / "data" / "rewrite_media" / job_id,
    ):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            deleted_any = True
    if rewrite_id_ok(job_id):
        rw_dir = _rewrite_project_dir(job_id)
        if rw_dir.is_dir():
            shutil.rmtree(rw_dir, ignore_errors=True)
            deleted_any = True
        legacy_fp = _rewrite_legacy_filepath(job_id)
        if legacy_fp.is_file():
            legacy_fp.unlink(missing_ok=True)
            deleted_any = True
    remotion_public = BASE_DIR / "remotion" / "public" / "jobs" / job_id
    try:
        if remotion_public.is_symlink() or remotion_public.exists():
            remotion_public.unlink()
            deleted_any = True
    except OSError:
        pass

    if deleted_any:
        flash("Проект удалён.", "success")
    else:
        flash("Проект не найден.", "error")
    return redirect(url_for("index"))


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


@app.route("/job/<job_id>/elevenlabs/defaults", methods=["POST"])
def job_elevenlabs_defaults_save(job_id: str):
    body = request.get_json(silent=True) or {}

    def _pct(key: str, default: int) -> int:
        try:
            return max(0, min(100, int(body.get(key, default))))
        except (TypeError, ValueError):
            return default

    voice_id = str(body.get("voice_id") or "").strip()
    model_id = normalize_tts_model_id(body.get("model_id"))
    voice_name = str(body.get("voice_name") or "").strip()
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
            "speed_pct": _pct("speed_pct", 20),
            "use_speaker_boost": use_speaker_boost,
        }
        if "tts_script_source" in body:
            job["tts_script_source"] = normalize_tts_script_source(body.get("tts_script_source"))
        save_job(job_id, job)
    return jsonify({"ok": True})


@app.route("/job/<job_id>/elevenlabs/tts/clear", methods=["POST"])
def job_elevenlabs_tts_clear(job_id: str):
    """Удалить MP3 озвучки и парные words.json с диска для проекта."""
    with _job_file_lock(job_id):
        if load_job(job_id) is None:
            return jsonify({"ok": False, "error": "Job not found"}), 404

    audio_dir = JOB_AUDIO_DIR / job_id
    removed: list[str] = []
    if audio_dir.is_dir():
        for mp3 in list(audio_dir.glob("*.mp3")):
            stem = mp3.stem
            candidates = [
                mp3,
                audio_dir / f"{stem}.words.json",
                audio_dir / f"{stem}.whisper.words.json",
            ]
            for path in candidates:
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError:
                    pass

    return jsonify({"ok": True, "removed": removed})


@app.route("/job/<job_id>/elevenlabs/tts", methods=["POST"])
def job_elevenlabs_tts(job_id: str):
    """Генерация озвучки ElevenLabs, файл в data/job_audio/<job_id>/."""
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    voice_id = (data.get("voice_id") or "").strip()
    model_id = normalize_tts_model_id(data.get("model_id"))
    voice_name = (data.get("voice_name") or "").strip() or voice_id

    if not text:
        return jsonify({"error": "Введите текст"}), 400
    if not voice_id:
        return jsonify({"error": "Выберите голос"}), 400

    with _job_file_lock(job_id):
        if load_job(job_id) is None:
            return jsonify({"error": "Job not found"}), 404

    max_c = max_chars_for_model()
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
    speed_pct = _pct("speed_pct", SPEED_PCT_DEFAULT)
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
    with _job_file_lock(job_id):
        job = load_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        job.pop("tts_outputs", None)
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
        model_id = normalize_tts_model_id(data.get("model_id"))
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

        model_max_c = max_chars_for_model(model_id)
        max_c = max_chars_for_tts_with_timestamps(text, model_id)
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
        speed_pct = _pct("speed_pct", SPEED_PCT_DEFAULT)
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
                "message": (
                    f"Подготовлено: {total_chunks} кусков, {_fmt_num_ru(total_chars)} символов "
                    f"(≤ {_fmt_num_ru(max_c)} на запрос"
                    + (
                        f", лимит модели {_fmt_num_ru(model_max_c)}"
                        if max_c < model_max_c
                        else ""
                    )
                    + ")."
                ),
                "total_chunks": total_chunks,
                "total_chars": total_chars,
                "sum_chunk_chars": sum_chunk_chars,
                "chunk_limit": max_c,
                "model_chunk_limit": model_max_c,
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
                                f"{_fmt_num_ru(len(ch))} символов. Ожидание ответа…"
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
                                f"[{i}/{total_chunks}] Ответ получен: {_fmt_num_ru(len(part_bytes))} байт, "
                                f"{_fmt_num_ru(len(chunk_words))} слов, длительность {chunk_duration_sec:.2f}с."
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
            "model_chunk_limit": model_max_c,
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
            "tts_model_chunk_limit": model_max_c,
            "text_preview": text[:120] + ("…" if len(text) > 120 else ""),
            "settings": {
                "stability_pct": stability_pct,
                "similarity_pct": similarity_pct,
                "style_pct": style_pct,
                "speed_pct": speed_pct,
                "use_speaker_boost": use_speaker_boost,
            },
        }

        with _job_file_lock(job_id):
            job = load_job(job_id)
            if job is None:
                yield _ev({"type": "error", "error": "Job not found", "elapsed_seconds": elapsed()})
                return
            job.pop("tts_outputs", None)
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
                "message": f"Готово: {len(chunks)} кусков, {_fmt_num_ru(len(text))} символов, ожидание {elapsed()}с.",
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
    return steps


def _archive_step_label(step: dict[str, Any]) -> str:
    if step["type"] == "audio":
        return "Озвучка (MP3)"
    if step["type"] == "scenes_json":
        return f"JSON-код сцен ({step.get('name') or 'scenes.json'})"
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
        if s.get("type") == "media" and s.get("slot") != "video"
    )
    planned_videos = sum(
        1
        for s in plan
        if s.get("type") == "media" and s.get("slot") == "video"
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


@app.route("/job/<job_id>/whisper/words", methods=["POST"])
def job_whisper_words(job_id: str):
    """Прогоняет последний MP3 джоба через локальный faster-whisper и сохраняет
    рядом ``<stem>.whisper.words.json``. Стримит прогресс в формате NDJSON
    (event-per-line), финальное событие — ``{"type":"final", ...}``.
    """
    if load_job(job_id) is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    mp3 = _latest_audio_path_for_job(job_id)
    if mp3 is None:
        return (
            jsonify({"ok": False, "error": "Нет MP3-озвучки в data/job_audio/<job_id>/"}),
            400,
        )
    audio_dir = mp3.parent
    out_filename = f"{mp3.stem}.whisper.words.json"
    out_path = audio_dir / out_filename

    body = request.get_json(silent=True) or {}
    raw_lang = (body.get("language") or "").strip() if isinstance(body, dict) else ""
    language = raw_lang or None

    def _ev(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @stream_with_context
    def gen():
        started = time.monotonic()

        def elapsed() -> float:
            return round(max(0.0, time.monotonic() - started), 1)

        try:
            from whisper_words import iter_progress_events  # late import
        except Exception as e:  # noqa: BLE001
            yield _ev(
                {
                    "type": "error",
                    "error": f"faster-whisper недоступен: {e}",
                    "elapsed_seconds": elapsed(),
                }
            )
            return

        yield _ev(
            {
                "type": "status",
                "phase": "prepare",
                "audio_filename": mp3.name,
                "language": language,
                "message": f"Транскрипция {mp3.name} через локальный faster-whisper…",
                "elapsed_seconds": elapsed(),
            }
        )

        final_doc: dict[str, Any] | None = None
        err: str | None = None
        try:
            for ev in iter_progress_events(mp3, language=language):
                stage = str(ev.get("stage") or "")
                if stage == "error":
                    err = str(ev.get("error") or "whisper failed")
                    break
                if stage == "final":
                    final_doc = ev.get("doc") if isinstance(ev.get("doc"), dict) else None
                    continue
                payload: dict[str, Any] = {"type": "status", "phase": stage, "elapsed_seconds": elapsed()}
                payload.update({k: v for k, v in ev.items() if k != "stage"})
                if stage == "model_load":
                    payload["message"] = f"Загружаю модель Whisper «{ev.get('model')}»…"
                elif stage == "model_ready":
                    payload["message"] = (
                        f"Модель «{ev.get('model')}» готова "
                        f"(device={ev.get('device')}, compute={ev.get('compute_type')})."
                    )
                elif stage == "segment":
                    total_ms = int(ev.get("total_ms") or 0)
                    cur_ms = int(ev.get("current_ms") or 0)
                    pct = (cur_ms / total_ms * 100.0) if total_ms > 0 else 0.0
                    payload["progress_pct"] = round(max(0.0, min(100.0, pct)), 1)
                    payload["message"] = (
                        f"Сегмент {ev.get('segment_index')}: {cur_ms / 1000:.1f}s "
                        f"/ {(total_ms / 1000):.1f}s · слов накоплено {_fmt_num_ru(ev.get('words_so_far'))}."
                    )
                yield _ev(payload)
        except Exception as e:  # noqa: BLE001
            err = str(e)

        if err or final_doc is None:
            yield _ev(
                {
                    "type": "error",
                    "error": err or "Whisper не вернул результат",
                    "elapsed_seconds": elapsed(),
                }
            )
            return

        try:
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(final_doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, out_path)
        except OSError as e:
            yield _ev(
                {
                    "type": "error",
                    "error": f"Не удалось сохранить {out_filename}: {e}",
                    "elapsed_seconds": elapsed(),
                }
            )
            return

        words_list = final_doc.get("words") or []
        first_w = words_list[0] if words_list else None
        last_w = words_list[-1] if words_list else None
        yield _ev(
            {
                "type": "final",
                "phase": "done",
                "elapsed_seconds": elapsed(),
                "audio_filename": mp3.name,
                "words_filename": out_filename,
                "words_url": url_for(
                    "job_audio_file", job_id=job_id, filename=out_filename
                ),
                "total_words": int(final_doc.get("total_words") or len(words_list)),
                "total_duration_ms": int(final_doc.get("total_duration_ms") or 0),
                "language": final_doc.get("language"),
                "language_probability": final_doc.get("language_probability"),
                "model": final_doc.get("model"),
                "device": final_doc.get("device"),
                "compute_type": final_doc.get("compute_type"),
                "first_word": first_w,
                "last_word": last_w,
                "message": (
                    f"Готово: {_fmt_num_ru(len(words_list))} слов, "
                    f"язык={final_doc.get('language')}, "
                    f"модель={final_doc.get('model')} ({final_doc.get('device')})."
                ),
            }
        )

    return Response(stream_with_context(gen()), mimetype="application/x-ndjson")


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
    prefer_video = _montage_bool_clamp(data.get("prefer_video"))

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
            "prefer_video": prefer_video,
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
                job_work = load_job(job_id)
                if job_work is None:
                    worker_result["error"] = "Job not found"
                    return
                scenes_w = job_work.get("scenes")
                if isinstance(scenes_w, list) and scenes_w:
                    # Пересчитать audio_timing из words.json (алгоритм align мог обновиться).
                    # Используем источник, выбранный пользователем (если сохранён);
                    # иначе — ElevenLabs по умолчанию, как и раньше.
                    _src = _timings_source_normalize(job_work.get("apply_timings_source"))
                    if _src == "whisper" and not _job_has_words_for_source(job_id, "whisper"):
                        _src = "elevenlabs"
                    _apply_tts_word_timings_to_scenes(job_id, scenes_w, source=_src)
                    try:
                        save_job(job_id, job_work)
                    except Exception:
                        try:
                            app.logger.warning(  # type: ignore[attr-defined]
                                "montage assemble: save after word re-align failed job=%s",
                                job_id,
                            )
                        except Exception:
                            pass
                props = prepare_montage(
                    job_id=job_id,
                    job=job_work,
                    base_dir=out_dir,
                    audio_src=audio_src,
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


def _montage_file_cors(resp: Response) -> Response:
    """Studio Remotion (:3000) грузит props.json с Flask (:5000) — нужен CORS."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


@app.route("/job/<job_id>/montage/file/<path:filename>", methods=["GET", "HEAD", "OPTIONS"])
def job_montage_file(job_id: str, filename: str):
    if request.method == "OPTIONS":
        return _montage_file_cors(make_response("", 204))
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
        return _montage_file_cors(
            send_from_directory(d, filename, mimetype="application/json", max_age=0)
        )
    # медиа сцен/озвучка/готовый mp4 — содержимое не меняется (cachebusting через имя файла сцены и пересборку каталога),
    # отдаём с долгоживущим immutable, чтобы браузер не перепрашивал каждый сик в превью
    return _montage_file_cors(send_from_directory(d, filename, max_age=60 * 60 * 24 * 30))


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
            # Проект мог быть создан как rewrite-only (legacy) или прийти
            # сюда после редиректа со старого `/rewrite/<id>` — в обоих случаях
            # имеет смысл подхватить, если есть только rewrite-папка.
            if rewrite_id_ok(job_id) and _rewrite_project_dir(job_id).is_dir():
                _ensure_job_file_for_id(job_id)
                job = load_job(job_id)
        if job is None:
            flash("Проект не найден.", "error")
            return redirect(url_for("index"))

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

    # `job.get("scenes", [])` is [] only if the key is missing; explicit null in JSON → None.
    _scenes_val = job.get("scenes", [])
    scenes_for_template = _scenes_val if isinstance(_scenes_val, list) else []

    summary = compute_summary(scenes_for_template)
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
    whisper_last_words_href: str | None = None
    whisper_last_words_name: str | None = None
    whisper_initial_final_ev: dict[str, Any] | None = None
    whisper_words_body: str | None = None
    if job_has_audio and audio_dir.is_dir():
        mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if mp3s:
            tts_last_audio_name = mp3s[0].name
            tts_last_audio_href = url_for("job_audio_file", job_id=job_id, filename=mp3s[0].name)
            # Парный .words.json (если есть) — для авто-подгрузки блока «Тайминги слов».
            base_stem = mp3s[0].with_suffix("").name
            words_candidate = base_stem + ".words.json"
            words_path = audio_dir / words_candidate
            if words_path.is_file():
                tts_last_words_name = words_candidate
                tts_last_words_href = url_for("job_audio_file", job_id=job_id, filename=words_candidate)
            # Whisper-результат (если есть) — авто-восстановление блока после рефреша.
            whisper_candidate = base_stem + ".whisper.words.json"
            whisper_path = audio_dir / whisper_candidate
            if whisper_path.is_file():
                whisper_last_words_name = whisper_candidate
                whisper_last_words_href = url_for(
                    "job_audio_file", job_id=job_id, filename=whisper_candidate
                )
                # Snapshot для JS-функции renderWhisperWordsFromFinal(): только
                # короткая meta, сам массив слов JS дотянет фоном через words_url.
                try:
                    _doc = json.loads(whisper_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    _doc = None
                if isinstance(_doc, dict):
                    _words_list = _doc.get("words") if isinstance(_doc.get("words"), list) else []
                    _first_w = _words_list[0] if _words_list else None
                    _last_w = _words_list[-1] if _words_list else None
                    try:
                        whisper_words_body = json.dumps(_doc, ensure_ascii=False, indent=2)
                    except (TypeError, ValueError):
                        whisper_words_body = None
                    whisper_initial_final_ev = {
                        "type": "final",
                        "phase": "done",
                        "audio_filename": mp3s[0].name,
                        "words_filename": whisper_candidate,
                        "words_url": whisper_last_words_href,
                        "total_words": int(_doc.get("total_words") or len(_words_list)),
                        "total_duration_ms": int(_doc.get("total_duration_ms") or 0),
                        "language": _doc.get("language"),
                        "language_probability": _doc.get("language_probability"),
                        "model": _doc.get("model"),
                        "device": _doc.get("device"),
                        "compute_type": _doc.get("compute_type"),
                        "first_word": _first_w,
                        "last_word": _last_w,
                        "message": (
                            f"Сохранённый прогон: {_fmt_num_ru(len(_words_list))} слов, "
                            f"язык={_doc.get('language')}, модель={_doc.get('model')}."
                        ),
                    }
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
    montage_prefer_video = _montage_bool_clamp(_mont.get("prefer_video"))

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
    rewrite_ctx = _rewrite_template_context(job_id)
    html = render_template(
        "job.html",
        job_id=job_id,
        job=job,
        **rewrite_ctx,
        rewrite_id_allowed=rewrite_id_ok(job_id),
        scenes=scenes_for_template,
        scenes_stripped_with_timing=_render_scenes_stripped_with_timing(scenes_for_template),
        tts_words_available=bool(tts_last_words_href),
        whisper_words_available=bool(whisper_last_words_href),
        whisper_last_words_href=whisper_last_words_href,
        whisper_last_words_name=whisper_last_words_name,
        whisper_initial_final_ev=whisper_initial_final_ev,
        whisper_words_body=whisper_words_body,
        apply_timings_source=(
            # Источник, выбранный пользователем ранее (если до сих пор валиден),
            # иначе — лучший доступный (Whisper при наличии, иначе ElevenLabs).
            _timings_source_normalize(job.get("apply_timings_source"))
            if (
                (_timings_source_normalize(job.get("apply_timings_source")) == "elevenlabs" and bool(tts_last_words_href))
                or (_timings_source_normalize(job.get("apply_timings_source")) == "whisper" and bool(whisper_last_words_href))
            )
            else ("whisper" if whisper_last_words_href else "elevenlabs")
        ),
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
        tts_script_source=normalize_tts_script_source(job.get("tts_script_source")),
        json_script_source=normalize_json_script_source(job.get("json_script_source")),
        rewrite_block_present=bool(rewrite_ctx.get("rw")),
        montage_zoom_scale=montage_zoom_scale,
        montage_zoom_mode=montage_zoom_mode,
        montage_zoom_modes=list(_MONTAGE_ZOOM_MODES),
        montage_zoom_smooth=montage_zoom_smooth,
        montage_zoom_ref_seconds=montage_zoom_ref_seconds,
        montage_zoom_ref_seconds_min=_MONTAGE_ZOOM_REF_SEC_MIN,
        montage_zoom_ref_seconds_max=_MONTAGE_ZOOM_REF_SEC_MAX,
        montage_zoom_ref_seconds_step=_MONTAGE_ZOOM_REF_SEC_STEP,
        montage_fade_in_pct=montage_fade_in_pct,
        montage_prefer_video=montage_prefer_video,
        montage_props_ready=montage_props_ready,
        montage_mp4_ready=montage_mp4_ready,
        montage_props_url=montage_props_url,
        montage_mp4_url=montage_mp4_url,
        montage_remotion_open_url=montage_remotion_open_url,
        montage_active_render_task_id=montage_active_render_task_id,
    )
    # Запрещаем браузерный кеш страницы /job/<id>: HTML+inline-JS меняются часто
    # (rewrite-блок, тайминги слов и т.д.), при кешировании старая копия страницы
    # приводит к «не активным» полям — состояние UI принимает решение по
    # серверным флагам, инжектированным прямо в HTML/JS.
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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
