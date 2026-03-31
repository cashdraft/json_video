#!/usr/bin/env python3
"""
JSON Video Generator - First Page
Web interface for parsing scene JSON and preparing for image/video generation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash

from kie_client import create_image_task, create_video_task, get_task_result, get_video_task_result

load_dotenv()

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "data" / "jobs"

app = Flask(__name__)
app.secret_key = os.urandom(24)
GENERATION_TASKS: dict[str, dict] = {}


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
) -> dict:
    """Собирает payload для сохранения job."""
    return {
        "project_name": project_name.strip(),
        "raw_input": raw_input,
        "parsed_scenes": parsed_scenes,
        "selected_aspect_ratio": aspect_ratio,
        "selected_video_duration": video_duration,
        "selected_image_model": image_model,
        "selected_video_model": video_model,
        "selected_resolution": resolution,
        "created_at": datetime.now().isoformat(),
        "status": "draft",
        "job_meta": {
            "aspect_ratio": aspect_ratio,
            "video_duration": video_duration,
            "image_model": image_model,
            "video_model": video_model,
            "resolution": resolution,
            "output_format": "jpg",
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
    ctx = {"jobs": list_jobs()}
    ctx.update(kwargs)
    return render_template("index.html", **ctx)


@app.route("/")
def index():
    return render_index()


@app.route("/parse", methods=["POST"])
def parse():
    raw_text = request.form.get("json_input", "")
    project_name = request.form.get("project_name", "")
    aspect_ratio = request.form.get("aspect_ratio", "16:9")
    resolution = request.form.get("resolution", "2K")
    video_duration = int(request.form.get("video_duration", "10"))
    image_model = request.form.get("image_model", "nano-banana-pro")
    video_model = normalize_video_model(request.form.get("video_model", "veo3_fast"))

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
        scenes=scenes,
        summary=summary,
    )


@app.route("/save", methods=["POST"])
def save():
    raw_text = request.form.get("json_input", "")
    project_name = request.form.get("project_name", "")
    aspect_ratio = request.form.get("aspect_ratio", "16:9")
    resolution = request.form.get("resolution", "2K")
    video_duration = int(request.form.get("video_duration", "10"))
    image_model = request.form.get("image_model", "nano-banana-pro")
    video_model = normalize_video_model(request.form.get("video_model", "veo3_fast"))

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
            task_id = create_image_task(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                output_format=output_format,
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
        "waiting": "Task accepted, waiting in queue",
        "queuing": "Queued for generation",
        "generating": f"Generating {slot_label}",
        "success": "Generation complete",
        "fail": "Generation failed",
    }.get(state, "Processing task")

    response = {
        "task_id": task_id,
        "state": state,
        "state_text": state_text,
        "elapsed_seconds": elapsed_seconds,
    }

    if state == "success":
        urls = result.get("result_urls", [])
        url = urls[0] if urls else ""
        response["url"] = url
        if url:
            job = load_job(job_id)
            if job is not None:
                scene_idx = task_meta["scene_idx"]
                slot = task_meta["slot"]
                scenes = job.get("scenes", [])
                if 0 <= scene_idx < len(scenes):
                    scene = scenes[scene_idx]
                    scene[slot] = scene.get(slot) or {"prompt": None}
                    if slot == "video":
                        scene[slot]["video_url"] = url
                    else:
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
        flash("Проект удалён.", "success")
    else:
        flash("Проект не найден.", "error")
    return redirect(url_for("index"))


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

    summary = compute_summary(job.get("scenes", []))
    return render_template(
        "job.html",
        job_id=job_id,
        job=job,
        scenes=job.get("scenes", []),
        summary=summary,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
