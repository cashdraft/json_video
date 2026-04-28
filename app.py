#!/usr/bin/env python3
"""
JSON Video Generator - First Page
Web interface for parsing scene JSON and preparing for image/video generation.
"""

from __future__ import annotations

import copy
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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
    list_voices as elevenlabs_list_voices,
    max_chars_for_model,
    merge_mp3_files_ffmpeg,
    split_tts_text_into_chunks,
    text_to_speech_bytes,
)
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
    list_draft1_wire_chat_payloads_for_export,
    normalize_rewrite_model,
    rewrite_chat_completion_wire_payload,
)
from rewrite_pipeline import (
    REWRITE_STAGE_HELP_HINTS,
    REWRITE_STAGE_KEYS,
    REWRITE_STAGE_SEND_HINTS,
    REWRITE_STAGES,
    any_stage_has_result,
    clamp_target_chars,
    compose_rewrite_openai_request_body,
    merge_stages_from_request,
    new_stages_dict,
    normalize_rewrite_job_data,
    snapshot_master_prompt_from_body,
    snapshot_pipeline_extras_from_body,
    snapshot_stages_from_body,
    stage_run_prerequisites_met,
)
from rewrite_templates import (
    REWRITE_TEMPLATES_DIR,
    list_rewrite_template_names,
    load_rewrite_template,
    save_rewrite_template_to_disk,
)

# --- Paths ---
JOBS_DIR = BASE_DIR / "data" / "jobs"
JOB_AUDIO_DIR = BASE_DIR / "data" / "job_audio"
REWRITE_JOBS_DIR = BASE_DIR / "data" / "rewrite_jobs"
REWRITE_MEDIA_DIR = BASE_DIR / "data" / "rewrite_media"

_REWRITE_ID_RE = re.compile(r"^rewrite_\d{8}_\d{6}$")


def _safe_job_audio_filename(name: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.mp3$", name))


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
    txt = str(raw_text or "").strip()
    if not txt:
        return []
    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict) and isinstance(parsed.get("blocks"), list):
        return [x for x in parsed.get("blocks") if isinstance(x, dict)]
    return []


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
            "ok": input_chars == output_chars,
            "ok_compact": input_compact_chars == output_compact_chars,
        },
    }


def _build_block_writer_check(completed_blocks: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for i, b in enumerate(completed_blocks or [], start=1):
        idx = int(b.get("block_index") or i)
        target = int(b.get("target_chars_ideal") or 0)
        out_chars = int(b.get("actual_chars") or len(str(b.get("block_text") or "")))
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


def parse_scene_blocks(raw_text: str) -> tuple[list[dict], list[str]]:
    """
    Parse raw text into scene blocks.
    Logic: new scene_id starts a new scene; subsequent blocks belong to current scene.
    Returns: (list of scene dicts, list of error messages)
    """
    scenes = []
    errors = []
    lines = raw_text.strip().split("\n")
    current_scene = None

    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"Ошибка в строке {i}: не удалось распарсить JSON — {e}")
            continue

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
                return json.load(f)
        except json.JSONDecodeError:
            time.sleep(0.02)
            continue
        except OSError:
            return None
    return None


def save_job(job_id: str, job: dict) -> None:
    """Persist job JSON to disk."""
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
        "stages": new_stages_dict(),
        "model": REWRITE_DEFAULT_MODEL,
        "last_prompt": "",
        "last_text": "",
        "last_result": "",
        "source_locked": False,
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


def image_model_label(value: str | None) -> str:
    """Короткая подпись для UI (Nano Banana Pro и т.д.)."""
    mid = (value or "").strip().lower()
    if mid in {"nano-banana-pro", "nano banana pro"}:
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
    rewrite_stage_run_ok = {
        sk: stage_run_prerequisites_met(sk, st) for sk in REWRITE_STAGE_KEYS
    }
    rewrite_stage_key_order = [k for k, _ in REWRITE_STAGES]
    try:
        resp = make_response(
            render_template(
                "rewrite_project.html",
                rw=rw,
                rewrite_stages=REWRITE_STAGES,
                rewrite_stage_send_hints=REWRITE_STAGE_SEND_HINTS,
                rewrite_stage_help_hints=REWRITE_STAGE_HELP_HINTS,
                rewrite_stage_run_ok=rewrite_stage_run_ok,
                rewrite_stage_key_order=rewrite_stage_key_order,
                rewrite_models=REWRITE_MODELS,
                rewrite_template_names=list_rewrite_template_names(),
                openai_key_set=key_set,
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


@app.route("/rewrite/<rewrite_id>/run", methods=["POST"])
def rewrite_project_run(rewrite_id: str):
    """Стрим NDJSON: отдельная сборка для structure и draft1; остальные — общая."""
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    stage_key = str(body.get("stage") or "").strip().lower()
    source_text, stages_snap = snapshot_stages_from_body(body)
    master_prompt = snapshot_master_prompt_from_body(body)
    hero_prompt, target_chars = snapshot_pipeline_extras_from_body(body)
    api_key = os.getenv("OPENAI_API_KEY") or ""

    def gen():
        if stage_key not in REWRITE_STAGE_KEYS:
            yield json.dumps(
                {"type": "error", "message": "Неизвестный этап. Обновите страницу."},
                ensure_ascii=False,
            ) + "\n"
            return
        if stage_key not in (
            "structure",
            "continuity_editor",
            "retention_editor",
            "hook_editor",
            "flow_editor",
            "persona_editor",
            "voiceover_editor",
            "structure_splitter",
            "scene_writer",
        ) and not (source_text or "").strip():
            yield json.dumps(
                {"type": "error", "message": "Введите исходный текст в верхнем поле."},
                ensure_ascii=False,
            ) + "\n"
            return
        block_writer_full_text = ""
        if stage_key == "continuity_editor":
            full_text_path = _rewrite_block_writer_dir(rewrite_id) / "full_text.txt"
            if full_text_path.exists():
                try:
                    block_writer_full_text = full_text_path.read_text(encoding="utf-8")
                except OSError:
                    block_writer_full_text = ""
        continuity_editor_text = ""
        if stage_key == "retention_editor":
            p = _rewrite_stage_result_path(rewrite_id, "continuity_editor")
            if p.exists():
                try:
                    continuity_editor_text = p.read_text(encoding="utf-8")
                except OSError:
                    continuity_editor_text = ""
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
        if stage_key == "structure_splitter":
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
        payload, compose_err = compose_rewrite_openai_request_body(
            stage_key,
            source_text=source_text,
            stages_snap=stages_snap,
            master_prompt=master_prompt,
            hero_prompt=hero_prompt,
            target_chars=target_chars,
            block_writer_full_text=block_writer_full_text,
            continuity_editor_text=continuity_editor_text,
            retention_editor_text=retention_editor_text,
            hook_editor_text=hook_editor_text,
            flow_editor_text=flow_editor_text,
            persona_editor_text=persona_editor_text,
            voiceover_editor_text=voiceover_editor_text,
            structure_splitter_text=structure_splitter_text,
        )
        if compose_err:
            yield json.dumps({"type": "error", "message": compose_err}, ensure_ascii=False) + "\n"
            return
        msgs = payload["messages"]
        prompt = str(msgs[0].get("content") or "")
        user_text = str(msgs[1].get("content") or "")
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
            blocks: list[dict] = []
            try:
                parsed = json.loads(raw_blocks) if raw_blocks else []
                if isinstance(parsed, list):
                    blocks = [b for b in parsed if isinstance(b, dict)]
                elif isinstance(parsed, dict) and isinstance(parsed.get("blocks"), list):
                    blocks = [b for b in parsed.get("blocks") if isinstance(b, dict)]
            except json.JSONDecodeError:
                blocks = []
            if not blocks:
                yield json.dumps({"type": "error", "message": "Structure Splitter не вернул список блоков."}, ensure_ascii=False) + "\n"
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
        else:
            for item in iter_rewrite_completion(api_key, model, prompt, user_text):
                yield json.dumps(item, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )


@app.route("/rewrite/<rewrite_id>/api-payload", methods=["POST"])
def rewrite_project_api_payload(rewrite_id: str):
    """Скачивание JSON тела запроса к OpenAI для этапа (как при запуске ↻)."""
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    stage_key = str(body.get("stage") or "").strip().lower()
    source_text, stages_snap = snapshot_stages_from_body(body)
    master_prompt = snapshot_master_prompt_from_body(body)
    hero_prompt, target_chars = snapshot_pipeline_extras_from_body(body)
    block_writer_full_text = ""
    if stage_key == "continuity_editor":
        full_text_path = _rewrite_block_writer_dir(rewrite_id) / "full_text.txt"
        if full_text_path.exists():
            try:
                block_writer_full_text = full_text_path.read_text(encoding="utf-8")
            except OSError:
                block_writer_full_text = ""
    continuity_editor_text = ""
    if stage_key == "retention_editor":
        p = _rewrite_stage_result_path(rewrite_id, "continuity_editor")
        if p.exists():
            try:
                continuity_editor_text = p.read_text(encoding="utf-8")
            except OSError:
                continuity_editor_text = ""
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
    if stage_key == "structure_splitter":
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
    payload, err = compose_rewrite_openai_request_body(
        stage_key,
        source_text=source_text,
        stages_snap=stages_snap,
        master_prompt=master_prompt,
        hero_prompt=hero_prompt,
        target_chars=target_chars,
        block_writer_full_text=block_writer_full_text,
        continuity_editor_text=continuity_editor_text,
        retention_editor_text=retention_editor_text,
        hook_editor_text=hook_editor_text,
        flow_editor_text=flow_editor_text,
        persona_editor_text=persona_editor_text,
        voiceover_editor_text=voiceover_editor_text,
        structure_splitter_text=structure_splitter_text,
    )
    if err:
        return jsonify({"ok": False, "message": err}), 400
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
        blocks_sw: list[dict] = []
        try:
            parsed_sw = json.loads(raw_blocks) if raw_blocks else []
            if isinstance(parsed_sw, list):
                blocks_sw = [b for b in parsed_sw if isinstance(b, dict)]
            elif isinstance(parsed_sw, dict) and isinstance(parsed_sw.get("blocks"), list):
                blocks_sw = [b for b in parsed_sw.get("blocks") if isinstance(b, dict)]
        except json.JSONDecodeError:
            blocks_sw = []
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
    job = load_job(job_id)
    if job is None:
        flash("Проект не найден.", "error")
        return redirect(url_for("video_index"))

    raw_text = request.form.get("json_input", "")
    aspect_ratio = normalize_aspect_ratio(request.form.get("aspect_ratio", "16:9"), "16:9")
    resolution = request.form.get("resolution", "2K")
    image_model = request.form.get("image_model", "nano-banana-pro")
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
    meta["aspect_ratio"] = aspect_ratio
    meta["video_duration"] = 10
    meta["image_model"] = image_model
    meta["image_model_label"] = image_model_label(image_model)
    meta["video_model"] = video_model
    meta["video_model_label"] = video_model_label(video_model)
    meta["resolution"] = resolution
    meta["output_format"] = "jpg"
    meta["image_template"] = image_template

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
    save_job(job_id, job)

    flash(f"Сцены обновлены: {len(scenes)}.", "success")
    return redirect(url_for("job_page", job_id=job_id))


@app.route("/job/<job_id>/generate/start", methods=["POST"])
def generate_slot_start(job_id: str):
    """Старт генерации для слота (start/end/video). Возвращает task_id."""
    job = load_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    data = request.get_json() or {}
    scene_idx = data.get("scene_index", 0)
    slot = data.get("slot", "start")  # start | end | video

    scenes = job.get("scenes", [])
    if scene_idx < 0 or scene_idx >= len(scenes):
        return jsonify({"error": "Invalid scene index"}), 400

    scene = scenes[scene_idx]
    meta = job.get("job_meta", {})
    aspect_ratio = normalize_aspect_ratio(meta.get("aspect_ratio", "16:9"), "16:9")
    resolution = meta.get("resolution", "2K")
    output_format = meta.get("output_format", "jpg")
    video_model = normalize_video_model(meta.get("video_model", "veo3_fast"))
    video_duration = int(meta.get("video_duration", 10) or 10)

    prompt = None
    if slot == "start":
        prompt = scene.get("start", {}).get("prompt")
    elif slot == "end":
        prompt = scene.get("end", {}).get("prompt")
    elif slot == "video":
        prompt = scene.get("video", {}).get("prompt")

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

    try:
        if slot == "video":
            if video_model == "grok-imagine/image-to-video":
                task_id = create_grok_image_to_video_task(
                    prompt=prompt,
                    image_urls=video_image_urls or None,
                    aspect_ratio=aspect_ratio,
                    duration_seconds=video_duration,
                    nsfw_checker=False,
                )
            else:
                task_id = create_video_task(
                    prompt=prompt,
                    model=video_model,
                    aspect_ratio=aspect_ratio,
                    image_urls=video_image_urls,
                    generation_type=video_generation_type,
                )
        else:
            image_input_urls: list[str] = []
            tid = (meta.get("image_template") or "").strip()
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
            task_id = create_image_task(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                output_format=output_format,
                image_input=image_input_urls if image_input_urls else None,
            )
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    GENERATION_TASKS[task_id] = {
        "job_id": job_id,
        "scene_idx": scene_idx,
        "slot": slot,
        "started_at": datetime.now().timestamp(),
        "video_model": video_model if slot == "video" else "",
    }

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
        "started_at": datetime.now().timestamp(),
        "canceled": False,
    }
    job["scenes"] = scenes
    save_job(job_id, job)

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
        job = load_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        for idx, scene in enumerate(job.get("scenes", [])):
            for slot_name in ("start", "end", "video"):
                slot_obj = scene.get(slot_name, {})
                gen = slot_obj.get("generation", {}) if isinstance(slot_obj, dict) else {}
                if gen.get("task_id") == task_id and not gen.get("canceled"):
                    task_meta = {
                        "job_id": job_id,
                        "scene_idx": idx,
                        "slot": slot_name,
                        "started_at": gen.get("started_at", datetime.now().timestamp()),
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
            result = get_task_result(task_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    state = result.get("state", "unknown")
    slot_label = "video" if task_meta.get("slot") == "video" else "image"
    state_text = {
        "waiting": "В очереди Kie.ai (задача принята)",
        "queuing": "В очереди на генерацию",
        "generating": f"Генерация {slot_label}",
        "success": "Готово",
        "fail": "Ошибка генерации",
    }.get(state, "Обработка…")

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
                response["state_text"] = "720p waiting 1080p"
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
                            }
                            response["state"] = "success"
                            response["state_text"] = "Generation complete"
                            response["url"] = hd_url
                            response["hd_status_text"] = "1080p - done"
                            response["hd_elapsed_seconds"] = int(datetime.now().timestamp() - hd_started_at)
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
                            }
                        job["scenes"] = scenes
                        save_job(job_id, job)
        else:
            response["url"] = url
            if url:
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
                        }
                        job["scenes"] = scenes
                        save_job(job_id, job)
            GENERATION_TASKS.pop(task_id, None)
    elif state == "fail":
        response["error"] = result.get("error", "Generation failed")
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
                }
                job["scenes"] = scenes
                save_job(job_id, job)
        GENERATION_TASKS.pop(task_id, None)
    else:
        # Persist progress for refresh/recovery.
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
                    "hd_state": scene[slot].get("generation", {}).get("hd_state") if slot == "video" else None,
                    "hd_started_at": scene[slot].get("generation", {}).get("hd_started_at") if slot == "video" else None,
                    "canceled": False,
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
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    body = request.get_json(silent=True) or {}
    slot = str(body.get("slot") or "").strip().lower()
    if slot not in ("start", "end", "video"):
        return jsonify({"ok": False, "error": "Bad slot"}), 400

    try:
        scene_index = int(body.get("scene_index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad scene_index"}), 400

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

    prompt = str(body.get("prompt") or "").strip()
    slot_obj["prompt"] = prompt
    save_job(job_id, job)
    return jsonify({"ok": True, "prompt": prompt})


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
    job = load_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
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
    job = load_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    voice_id = (data.get("voice_id") or "").strip()
    model_id = (data.get("model_id") or "eleven_multilingual_v2").strip()
    voice_name = (data.get("voice_name") or "").strip() or voice_id

    if not text:
        return jsonify({"error": "Введите текст"}), 400
    if not voice_id:
        return jsonify({"error": "Выберите голос"}), 400

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
    job.pop("tts_outputs", None)
    tts_template = str(data.get("tts_template") or "").strip()
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


# --- ZIP «скачать всё»: фон + статус (прогресс) и прежний одношаговый GET ---
_download_all_lock = threading.Lock()
_download_all_tasks: dict[str, dict[str, Any]] = {}
_DOWNLOAD_ALL_MAX_TASKS = 48


def _archive_plan_steps(job_id: str, job: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    audio_dir = JOB_AUDIO_DIR / job_id
    if audio_dir.is_dir():
        mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if mp3s:
            steps.append({"type": "audio", "path": mp3s[0]})
    scenes = job.get("scenes") if isinstance(job.get("scenes"), list) else []
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
    planned_images = sum(1 for s in plan if s.get("type") == "media" and s.get("slot") != "video")
    planned_videos = sum(1 for s in plan if s.get("type") == "media" and s.get("slot") == "video")
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
    return send_from_directory(d, filename, mimetype="audio/mpeg", max_age=0)


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
    res_display = meta.get("resolution") or job.get("selected_resolution") or "2K"
    img_label = meta.get("image_model_label") or image_model_label(meta.get("image_model"))
    vid_label = meta.get("video_model_label") or video_model_label(meta.get("video_model"))
    scene_slot_image_header_meta = f"{res_display} · {img_label}"
    scene_slot_video_header_meta = vid_label
    audio_dir = JOB_AUDIO_DIR / job_id
    job_has_audio = audio_dir.is_dir() and any(audio_dir.glob("*.mp3"))
    tts_last_audio_href: str | None = None
    tts_last_audio_name: str | None = None
    if job_has_audio and audio_dir.is_dir():
        mp3s = sorted(audio_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if mp3s:
            tts_last_audio_name = mp3s[0].name
            tts_last_audio_href = url_for("job_audio_file", job_id=job_id, filename=mp3s[0].name)
    return render_template(
        "job.html",
        job_id=job_id,
        job=job,
        scenes=job.get("scenes", []),
        job_has_audio=job_has_audio,
        tts_last_text=str(job.get("tts_last_text") or ""),
        tts_last_audio_href=tts_last_audio_href,
        tts_last_audio_name=tts_last_audio_name,
        summary=summary,
        scene_slot_image_header_meta=scene_slot_image_header_meta,
        scene_slot_video_header_meta=scene_slot_video_header_meta,
        template_display=template_display,
        image_templates=templates_ui_rows(),
        tts_models=TTS_MODELS,
        elevenlabs_key_set=elevenlabs_key_set,
        tts_defaults=job.get("tts_defaults") or {},
        tts_template_names=list_elevenlabs_template_names(),
        tts_template=(job.get("tts_template") or "Naomi"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
