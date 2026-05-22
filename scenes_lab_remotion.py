"""Remotion props и рендер для /scenes-lab (LaterInfographic)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from montage_render_shared import BASE_DIR, render_state_view

SCENES_LAB_REMOTION_DIR = BASE_DIR / "data" / "scenes_lab" / "remotion"
SCENES_LAB_RENDER_WORKER = BASE_DIR / "scenes_lab_render_worker.py"
LAB_RENDER_JOB_ID = "scenes_lab"


def remotion_props_path() -> Path:
    return SCENES_LAB_REMOTION_DIR / "props.json"


def remotion_out_path() -> Path:
    return SCENES_LAB_REMOTION_DIR / "out.mp4"


def render_status_path() -> Path:
    return SCENES_LAB_REMOTION_DIR / "render_status.json"


def render_log_path() -> Path:
    return SCENES_LAB_REMOTION_DIR / "render.log"


def load_lab_render_state() -> dict[str, Any] | None:
    path = render_status_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def persist_lab_render_state(st: dict[str, Any]) -> None:
    st = dict(st)
    st["job_id"] = LAB_RENDER_JOB_ID
    path = render_status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": st.get("task_id"),
            "job_id": LAB_RENDER_JOB_ID,
            "state": st.get("state"),
            "progress_pct": int(st.get("progress_pct") or 0),
            "stage": st.get("stage"),
            "message": st.get("message"),
            "started_at": st.get("started_at"),
            "finished_at": st.get("finished_at"),
            "error": st.get("error"),
            "error_detail": st.get("error_detail"),
            "exit_code": st.get("exit_code"),
            "output_url": st.get("output_url"),
            "output_filename": st.get("output_filename"),
            "pid": st.get("pid"),
            "supervisor_pid": st.get("supervisor_pid"),
            "last_progress_at": st.get("last_progress_at"),
            "last_log_line": st.get("last_log_line"),
            "stuck_at": st.get("stuck_at"),
            "stuck_reason": st.get("stuck_reason"),
            "cancel_requested": bool(st.get("cancel_requested")),
            "updated_at": time.time(),
        }
        fd = st.get("frames_done")
        ft = st.get("frames_total")
        if fd is not None and ft is not None:
            payload["frames_done"] = int(fd)
            payload["frames_total"] = int(ft)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def reconcile_lab_render_state(st: dict[str, Any]) -> dict[str, Any]:
    """Как montage_render_shared.reconcile_render_state, но лог scenes-lab."""
    from montage_render_shared import MONTAGE_RENDER_STUCK_SEC, pid_is_alive

    state = str(st.get("state") or "")
    if state not in ("queued", "running", "stuck"):
        return st
    now = time.time()
    supervisor = st.get("supervisor_pid")
    render_pid = st.get("pid")
    alive_supervisor = pid_is_alive(supervisor) if supervisor is not None else None
    alive_render = pid_is_alive(render_pid) if render_pid is not None else False
    if supervisor is not None and alive_supervisor is False:
        log_tail = tail_lab_render_log(max_bytes=4000).strip()
        last_lines = "\n".join(log_tail.splitlines()[-8:]) if log_tail else ""
        detail = (
            "Процесс воркера рендера завершился (supervisor PID не отвечает). "
            "См. render.log в data/scenes_lab/remotion."
        )
        if last_lines:
            detail += "\n\n--- хвост render.log ---\n" + last_lines[-1200:]
        st.update({
            "state": "error",
            "error": "process_died",
            "error_detail": detail,
            "finished_at": st.get("finished_at") or now,
            "message": st.get("message") or "Воркер рендера не найден",
        })
        return st
    if supervisor is None and render_pid is not None and not alive_render:
        log_tail = tail_lab_render_log(max_bytes=4000).strip()
        last_lines = "\n".join(log_tail.splitlines()[-8:]) if log_tail else ""
        detail = "Процесс Remotion (npx) завершился. См. render.log."
        if last_lines:
            detail += "\n\n--- хвост render.log ---\n" + last_lines[-1200:]
        st.update({
            "state": "error",
            "error": "process_died",
            "error_detail": detail,
            "finished_at": st.get("finished_at") or now,
            "message": st.get("message") or "Процесс рендера не найден",
        })
        return st
    last_prog = float(st.get("last_progress_at") or st.get("started_at") or now)
    idle_sec = max(0.0, now - last_prog)
    if idle_sec >= MONTAGE_RENDER_STUCK_SEC:
        if not st.get("stuck_at"):
            st["stuck_at"] = now
            last_msg = str(st.get("last_log_line") or st.get("message") or "").strip()
            st["stuck_reason"] = (
                f"Нет прогресса {int(idle_sec)} с · этап «{st.get('stage') or '?'}» · "
                f"{int(st.get('progress_pct') or 0)}%"
                + (f" · лог: {last_msg[:200]}" if last_msg else "")
            )
        st["state"] = "stuck"
    elif state == "stuck" and idle_sec < MONTAGE_RENDER_STUCK_SEC:
        st["state"] = "running"
    return st


def sync_lab_render_state(st: dict[str, Any]) -> dict[str, Any]:
    st = dict(st)
    st["job_id"] = LAB_RENDER_JOB_ID
    st = reconcile_lab_render_state(st)
    persist_lab_render_state(st)
    return st


def tail_lab_render_log(max_bytes: int = 64_000) -> str:
    path = render_log_path()
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            raw = f.read()
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def lab_render_state_view(st: dict[str, Any]) -> dict[str, Any]:
    out = render_state_view(st)
    if render_log_path().is_file():
        out["has_render_log"] = True
    return out


def _props_from_svg_and_animation(svg: str, animation: dict[str, Any]) -> dict[str, Any]:
    try:
        fps = max(1, int(float(animation.get("fps") or 30)))
    except (TypeError, ValueError):
        fps = 30
    try:
        duration_sec = max(0.1, float(animation.get("duration_sec") or 5))
    except (TypeError, ValueError):
        duration_sec = 5.0

    tracks = animation.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("В animation нет tracks[].")

    duration_frames = max(1, int(round(duration_sec * fps)))
    return {
        "schema": "later_infographic_props@1",
        "fps": fps,
        "width": 1920,
        "height": 1080,
        "duration_sec": duration_sec,
        "duration_frames": duration_frames,
        "tracks_count": len(tracks),
        "svg": svg,
        "animation": {
            "fps": fps,
            "duration_sec": duration_sec,
            "tracks": tracks,
        },
    }


def build_remotion_props_from_sources(
    *,
    slot_id: str = "",
    svg: str = "",
    anim_text: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """
    props.json из SVG кадра + ответа анимации (===ANIM_START=== … или сырой JSON).
    Источники: явные svg/anim_text, иначе файлы слота img_N (scene.svg, anim_response.txt).
    """
    from later_response_parse import validate_animation_for_svg
    from scenes_lab_img_slots import (
        _slot_dir,
        load_img_slot_anim_response,
        load_img_slot_response,
        load_img_slot_svg_for_remotion,
    )

    sid = (slot_id or "").strip()
    svg_body = (svg or "").strip()
    anim_src = (anim_text or "").strip()

    if sid:
        slot_path = _slot_dir(sid)
        if slot_path is None:
            return None, f"Слот {sid!r} не найден."
        # Remotion всегда берёт SVG/anim с диска слота (не textarea UI — там часто чужой кадр).
        svg_body = load_img_slot_svg_for_remotion(sid).strip()
        anim_src = load_img_slot_anim_response(sid).strip()
        if not anim_src:
            anim_src = load_img_slot_response(sid).strip()

    if not svg_body:
        return None, (
            "SVG пустой — в слоте нужен scene.svg "
            "(после «Анимировать» также пишется scene_at_anim.svg)."
        )
    if not anim_src:
        return None, "Нет ответа анимации — сначала «Анимировать» для этого кадра."

    validation = validate_animation_for_svg(anim_src, svg_body)
    if not validation.get("ok"):
        errs = validation.get("errors") or []
        return None, "; ".join(str(e) for e in errs[:5]) or "Валидация анимации не пройдена."

    parsed = validation.get("parsed") if isinstance(validation.get("parsed"), dict) else {}
    animation = parsed.get("animation")
    if not isinstance(animation, dict):
        return None, "JSON анимации отсутствует или невалиден."

    try:
        props = _props_from_svg_and_animation(svg_body, animation)
    except ValueError as exc:
        return None, str(exc)
    if sid:
        props["slot_id"] = sid
    return props, None


def build_remotion_props_from_session() -> tuple[dict[str, Any] | None, str | None]:
    """Fallback: последний слот с anim_response или старая later_session."""
    from scenes_lab_img_slots import list_img_slot_ids, load_img_slot_anim_response

    for sid in reversed(list_img_slot_ids()):
        if load_img_slot_anim_response(sid).strip():
            props, err = build_remotion_props_from_sources(slot_id=sid)
            if props:
                return props, None
            if err:
                return None, err

    from later_response_parse import process_later_model_response
    from scenes_lab_session import load_later_session

    row = load_later_session()
    if not row:
        return None, "Нет кадра с анимацией — выберите слот и нажмите «Анимировать»."
    text = str(row.get("text") or "").strip()
    if not text:
        return None, "Пустая сессия."

    bundle = process_later_model_response(text)
    validation = bundle.get("validation") if isinstance(bundle.get("validation"), dict) else {}
    parsed = bundle.get("parsed") if isinstance(bundle.get("parsed"), dict) else {}
    svg = str(parsed.get("svg") or "").strip()
    animation = parsed.get("animation")
    anim_raw = str(parsed.get("animation_raw") or "").strip()
    if svg and isinstance(animation, dict):
        try:
            return _props_from_svg_and_animation(svg, animation), None
        except ValueError as exc:
            return None, str(exc)
    if svg and anim_raw:
        return build_remotion_props_from_sources(svg=svg, anim_text=anim_raw)

    errs = validation.get("errors") or []
    msg = "; ".join(str(e) for e in errs[:5]) if errs else "JSON анимации отсутствует или невалиден."
    return None, msg


def write_remotion_props(
    *,
    slot_id: str = "",
    svg: str = "",
    anim_text: str = "",
) -> tuple[Path | None, str | None]:
    """Записать props.json: приоритет — slot_id + anim_text из UI, иначе fallback."""
    if (slot_id or "").strip() or (svg or "").strip() or (anim_text or "").strip():
        props, err = build_remotion_props_from_sources(
            slot_id=slot_id,
            svg=svg,
            anim_text=anim_text,
        )
    else:
        props, err = build_remotion_props_from_session()
    if err or not props:
        return None, err or "Не удалось собрать props."
    path = remotion_props_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, None


def spawn_lab_render_supervisor(task_id: str) -> int | None:
    cmd = [
        sys.executable,
        str(SCENES_LAB_RENDER_WORKER),
        "--task-id",
        task_id,
    ]
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        return int(proc.pid)
    except OSError:
        return None


def lab_render_active() -> dict[str, Any] | None:
    disk = load_lab_render_state()
    if not disk:
        return None
    disk = sync_lab_render_state(disk)
    if str(disk.get("state") or "") in ("queued", "running", "stuck"):
        from montage_render_shared import pid_is_alive

        sup = disk.get("supervisor_pid")
        if sup is not None and pid_is_alive(sup):
            return disk
    return None


def new_render_task_id() -> str:
    return uuid.uuid4().hex
