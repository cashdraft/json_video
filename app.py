#!/usr/bin/env python3
"""
JSON Video Generator - First Page
Web interface for parsing scene JSON and preparing for image/video generation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
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
    send_from_directory,
    stream_with_context,
    url_for,
)

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
from kie_client import (
    create_image_task,
    create_video_task,
    get_task_result,
    get_video_task_result,
    get_video_1080p_result,
)
from rewrite_openai import (
    REWRITE_DEFAULT_MODEL,
    REWRITE_MODELS,
    iter_rewrite_completion,
    normalize_rewrite_model,
)
from rewrite_pipeline import (
    REWRITE_STAGE_KEYS,
    REWRITE_STAGES,
    any_stage_has_result,
    build_stage_user_message,
    combine_system_prompt,
    merge_stages_from_request,
    new_stages_dict,
    normalize_rewrite_job_data,
    snapshot_master_prompt_from_body,
    snapshot_stages_from_body,
    validate_prerequisites,
)

load_dotenv()

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"
JOB_AUDIO_DIR = BASE_DIR / "data" / "job_audio"
REWRITE_JOBS_DIR = BASE_DIR / "data" / "rewrite_jobs"

_REWRITE_ID_RE = re.compile(r"^rewrite_\d{8}_\d{6}$")


def _safe_job_audio_filename(name: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.mp3$", name))

app = Flask(__name__)
app.secret_key = os.urandom(24)
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
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def save_job(job_id: str, job: dict) -> None:
    """Persist job JSON to disk."""
    filepath = JOBS_DIR / f"{job_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)


def update_job_field(job_id: str, field: str, value) -> bool:
    """Обновляет поле в job-файле. Возвращает True при успехе."""
    job = load_job(job_id)
    if job is None:
        return False
    job[field] = value
    filepath = JOBS_DIR / f"{job_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
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


def _rewrite_filepath(rewrite_id: str) -> Path:
    return REWRITE_JOBS_DIR / f"{rewrite_id}.json"


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
        "duration_minutes": 5,
    }


def create_rewrite_job(project_name: str) -> str:
    REWRITE_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    rewrite_id = f"rewrite_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    payload = new_rewrite_payload(rewrite_id, project_name)
    with open(_rewrite_filepath(rewrite_id), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return rewrite_id


def load_rewrite_job(rewrite_id: str) -> dict | None:
    if not rewrite_id_ok(rewrite_id):
        return None
    fp = _rewrite_filepath(rewrite_id)
    if not fp.is_file():
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        normalize_rewrite_job_data(data)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_rewrite_job(rewrite_id: str, data: dict) -> None:
    if not rewrite_id_ok(rewrite_id):
        raise ValueError("bad rewrite_id")
    REWRITE_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["rewrite_id"] = rewrite_id
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(_rewrite_filepath(rewrite_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_rewrite_jobs() -> list[dict]:
    rows = []
    if not REWRITE_JOBS_DIR.is_dir():
        return rows
    for f in sorted(REWRITE_JOBS_DIR.glob("rewrite_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        rid = f.stem
        if not rewrite_id_ok(rid):
            continue
        try:
            data = json.load(open(f, "r", encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
    return "veo3_fast"


def video_model_label(value: str | None) -> str:
    model_id = normalize_video_model(value)
    return "Veo 3.1 Fast" if model_id == "veo3_fast" else "Veo 3.1 Quality"


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
    return render_index()


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
    resp = make_response(
        render_template(
            "rewrite_project.html",
            rw=rw,
            rewrite_stages=REWRITE_STAGES,
            rewrite_models=REWRITE_MODELS,
            openai_key_set=key_set,
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
    fp = _rewrite_filepath(rewrite_id)
    if rewrite_id_ok(rewrite_id) and fp.is_file():
        fp.unlink()
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
    if "source_text" in body:
        if not rw.get("source_locked") or locked_in_body is False:
            rw["source_text"] = str(body.get("source_text") or "")
    if locked_in_body is not None:
        rw["source_locked"] = bool(locked_in_body)
    m_lock_in = body.get("master_prompt_locked") if "master_prompt_locked" in body else None
    if "master_prompt" in body:
        if not rw.get("master_prompt_locked") or m_lock_in is False:
            rw["master_prompt"] = str(body.get("master_prompt") or "")
    if m_lock_in is not None:
        rw["master_prompt_locked"] = bool(m_lock_in)
    if "duration_minutes" in body:
        try:
            dm = int(body.get("duration_minutes"))
            rw["duration_minutes"] = max(1, min(30, dm))
        except (TypeError, ValueError):
            pass
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


@app.route("/rewrite/<rewrite_id>/run", methods=["POST"])
def rewrite_project_run(rewrite_id: str):
    """Стрим NDJSON для одного этапа: system = промпт этапа, user = исходник + предыдущие результаты."""
    if load_rewrite_job(rewrite_id) is None:
        return jsonify({"error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    stage_key = str(body.get("stage") or "").strip().lower()
    source_text, stages_snap = snapshot_stages_from_body(body)
    master_prompt = snapshot_master_prompt_from_body(body)
    api_key = os.getenv("OPENAI_API_KEY") or ""

    def gen():
        if stage_key not in REWRITE_STAGE_KEYS:
            yield json.dumps(
                {"type": "error", "message": "Неизвестный этап. Обновите страницу."},
                ensure_ascii=False,
            ) + "\n"
            return
        if not (source_text or "").strip():
            yield json.dumps(
                {"type": "error", "message": "Введите исходный текст в верхнем поле."},
                ensure_ascii=False,
            ) + "\n"
            return
        pre_err = validate_prerequisites(stage_key, stages_snap)
        if pre_err:
            yield json.dumps({"type": "error", "message": pre_err}, ensure_ascii=False) + "\n"
            return
        cell = stages_snap.get(stage_key) or {}
        model = normalize_rewrite_model(str(cell.get("model") or ""))
        prompt = combine_system_prompt(str(cell.get("prompt") or ""), master_prompt)
        user_text = build_stage_user_message(source_text, stage_key, stages_snap)
        for item in iter_rewrite_completion(api_key, model, prompt, user_text):
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )


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
    raw_text = request.form.get("json_input", "")
    project_name = request.form.get("project_name", "")
    aspect_ratio = request.form.get("aspect_ratio", "16:9")
    resolution = request.form.get("resolution", "2K")
    video_duration = 10
    image_model = request.form.get("image_model", "nano-banana-pro")
    video_model = normalize_video_model(request.form.get("video_model", "veo3_fast"))
    image_template = request.form.get("image_template", "").strip()

    scenes, errors = parse_scene_blocks(raw_text)

    if errors:
        return render_index(
            json_input=raw_text,
            project_name=project_name,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            video_duration=video_duration,
            image_model=image_model,
            video_model=video_model,
            image_template=image_template,
            errors=errors,
        )

    summary = compute_summary(scenes)

    return render_index(
        json_input=raw_text,
        project_name=project_name,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        video_duration=video_duration,
        image_model=image_model,
        video_model=video_model,
        image_template=image_template,
        scenes=scenes,
        summary=summary,
    )


@app.route("/save", methods=["POST"])
def save():
    raw_text = request.form.get("json_input", "")
    project_name = request.form.get("project_name", "")
    aspect_ratio = request.form.get("aspect_ratio", "16:9")
    resolution = request.form.get("resolution", "2K")
    video_duration = 10
    image_model = request.form.get("image_model", "nano-banana-pro")
    video_model = normalize_video_model(request.form.get("video_model", "veo3_fast"))
    image_template = request.form.get("image_template", "").strip()
    if image_template and not safe_template_dir(IMAGE_TEMPLATES_DIR, image_template):
        flash("Выбранный шаблон не найден в data/image_templates/.", "error")
        return redirect(url_for("index"))

    scenes, errors = parse_scene_blocks(raw_text)

    if errors:
        flash("Не удалось сохранить: есть ошибки парсинга.", "error")
        return redirect(url_for("index"))

    payload = build_job_payload(
        raw_input=raw_text,
        parsed_scenes=scenes,
        aspect_ratio=aspect_ratio,
        video_duration=video_duration,
        image_model=image_model,
        video_model=video_model,
        resolution=resolution,
        project_name=project_name,
        image_template=image_template,
    )

    filepath, job_id = save_job_file(payload)
    flash("Проект сохранён. Переход к генерации.", "success")
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
    aspect_ratio = meta.get("aspect_ratio", "16:9")
    resolution = meta.get("resolution", "2K")
    output_format = meta.get("output_format", "jpg")
    video_model = normalize_video_model(meta.get("video_model", "veo3_fast"))

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
            result = get_video_task_result(task_id)
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
    job["tts_defaults"] = {
        "voice_id": voice_id,
        "model_id": model_id,
        "stability_pct": stability_pct,
        "similarity_pct": similarity_pct,
        "style_pct": style_pct,
        "speed_pct": speed_pct,
        "use_speaker_boost": use_speaker_boost,
    }
    save_job(job_id, job)
    return jsonify({"ok": True, **entry})


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


@app.route("/job/<job_id>")
def job_page(job_id: str):
    """Страница проекта — генерация изображений и видео."""
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
    meta.setdefault("video_model", job.get("selected_video_model", "veo3_fast"))
    meta["video_model"] = normalize_video_model(meta.get("video_model"))
    meta["video_model_label"] = video_model_label(meta.get("video_model"))
    meta.setdefault("resolution", job.get("selected_resolution", "2K"))
    meta.setdefault("output_format", "jpg")
    meta.setdefault("image_template", job.get("selected_image_template", ""))

    if "tts_outputs" in job:
        job.pop("tts_outputs", None)
        ad = JOB_AUDIO_DIR / job_id
        if ad.is_dir():
            shutil.rmtree(ad, ignore_errors=True)
        save_job(job_id, job)

    summary = compute_summary(job.get("scenes", []))
    template_display = job_template_display(meta.get("image_template", ""))
    elevenlabs_key_set = bool((os.getenv("ELEVENLABS_API_KEY") or "").strip())
    return render_template(
        "job.html",
        job_id=job_id,
        job=job,
        scenes=job.get("scenes", []),
        summary=summary,
        template_display=template_display,
        tts_models=TTS_MODELS,
        elevenlabs_key_set=elevenlabs_key_set,
        tts_defaults=job.get("tts_defaults") or {},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
