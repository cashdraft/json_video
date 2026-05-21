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


def build_remotion_props_from_session() -> tuple[dict[str, Any] | None, str | None]:
    """Собрать props.json из валидированной later_session."""
    from later_response_parse import process_later_model_response
    from scenes_lab_session import load_later_session

    row = load_later_session()
    if not row:
        return None, "Нет сохранённой сессии Later…"
    text = str(row.get("text") or "").strip()
    if not text:
        return None, "Пустая сессия."

    bundle = process_later_model_response(text)
    validation = bundle.get("validation") if isinstance(bundle.get("validation"), dict) else {}
    if not validation.get("ok"):
        errs = validation.get("errors") or []
        msg = "; ".join(str(e) for e in errs[:5]) if errs else "Валидация не пройдена."
        return None, msg

    parsed = bundle.get("parsed") if isinstance(bundle.get("parsed"), dict) else {}
    svg = str(parsed.get("svg") or "").strip()
    animation = parsed.get("animation")
    if not svg:
        return None, "SVG пустой."
    if not isinstance(animation, dict):
        return None, "JSON анимации отсутствует или невалиден."

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
        return None, "В animation нет tracks[]."

    duration_frames = max(1, int(round(duration_sec * fps)))

    props: dict[str, Any] = {
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
    return props, None


def write_remotion_props() -> tuple[Path | None, str | None]:
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
