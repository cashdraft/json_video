"""Общая логика MP4-рендера Remotion: статус на диске и render.log (без Flask)."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
JOB_REMOTION_DIR = BASE_DIR / "data" / "job_remotion"
REMOTION_DIR = BASE_DIR / "remotion"
REMOTION_NPX = os.environ.get("REMOTION_NPX", "") or __import__("shutil").which("npx") or "/usr/bin/npx"

MONTAGE_RENDER_STUCK_SEC = max(
    60,
    int(os.environ.get("MONTAGE_RENDER_STUCK_SEC", "300") or 300),
)

PCT_RE = re.compile(r"(\d{1,3})\s*%")
FRAMES_RE = re.compile(r"Rendered\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def job_remotion_dir(job_id: str) -> Path:
    return JOB_REMOTION_DIR / job_id


def render_status_path(job_id: str) -> Path:
    return job_remotion_dir(job_id) / "render_status.json"


def render_log_path(job_id: str) -> Path:
    return job_remotion_dir(job_id) / "render.log"


def pid_is_alive(pid: Any) -> bool:
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return False
    if p <= 0:
        return False
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False


def touch_progress(st: dict[str, Any], **fields: Any) -> None:
    sig_before = (
        st.get("progress_pct"),
        st.get("frames_done"),
        st.get("frames_total"),
        st.get("stage"),
    )
    progressish = any(
        k in fields
        for k in ("progress_pct", "frames_done", "frames_total", "stage", "message")
    )
    if not progressish:
        return
    sig_after = (
        fields.get("progress_pct", st.get("progress_pct")),
        fields.get("frames_done", st.get("frames_done")),
        fields.get("frames_total", st.get("frames_total")),
        fields.get("stage", st.get("stage")),
    )
    if sig_after != sig_before:
        st["last_progress_at"] = time.time()
        st.pop("stuck_at", None)
        st.pop("stuck_reason", None)
        if st.get("state") == "stuck":
            st["state"] = "running"


def persist_render_state(st: dict[str, Any]) -> None:
    job_id = str(st.get("job_id") or "").strip()
    if not job_id:
        return
    path = render_status_path(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": st.get("task_id"),
            "job_id": job_id,
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
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def load_render_state(job_id: str) -> dict[str, Any] | None:
    path = render_status_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def tail_render_log(job_id: str, max_bytes: int = 64_000) -> str:
    path = render_log_path(job_id)
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


def parse_remotion_log_line(line: str) -> dict[str, Any]:
    """Разбор одной строки stdout Remotion → поля для render_status."""
    upd: dict[str, Any] = {"message": line[:240], "last_log_line": line[:500]}
    stage = None
    pct = None
    frames_done = None
    frames_total = None
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
    m = PCT_RE.search(line)
    if m:
        try:
            p = int(m.group(1))
            if 0 <= p <= 100:
                pct = p
        except ValueError:
            pct = None
    mf = FRAMES_RE.search(line)
    if mf:
        try:
            cur_f = int(mf.group(1))
            tot_f = int(mf.group(2))
            if tot_f > 0 and cur_f >= 0:
                frames_done = cur_f
                frames_total = tot_f
                pct = min(100, max(0, int(round(100.0 * cur_f / tot_f))))
                stage = stage or "rendering"
        except ValueError:
            pass
    if stage:
        upd["stage"] = stage
    if pct is not None:
        upd["progress_pct"] = pct
    if frames_done is not None:
        upd["frames_done"] = frames_done
    if frames_total is not None:
        upd["frames_total"] = frames_total
    return upd


def reconcile_render_state(st: dict[str, Any]) -> dict[str, Any]:
    state = str(st.get("state") or "")
    if state not in ("queued", "running", "stuck"):
        return st
    now = time.time()
    supervisor = st.get("supervisor_pid")
    render_pid = st.get("pid")
    alive_supervisor = pid_is_alive(supervisor) if supervisor is not None else None
    alive_render = pid_is_alive(render_pid) if render_pid is not None else False
    if supervisor is not None and alive_supervisor is False:
        log_tail = tail_render_log(str(st.get("job_id") or ""), max_bytes=4000).strip()
        last_lines = "\n".join(log_tail.splitlines()[-8:]) if log_tail else ""
        detail = (
            "Процесс воркера рендера завершился (supervisor PID не отвечает). "
            "Частые причины: падение npx/Chrome, нехватка RAM/диска, ручной kill. "
            "Рестарт json-video не должен останавливать воркер — см. render.log."
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
        log_tail = tail_render_log(str(st.get("job_id") or ""), max_bytes=4000).strip()
        last_lines = "\n".join(log_tail.splitlines()[-8:]) if log_tail else ""
        detail = (
            "Процесс Remotion (npx) завершился. См. render.log в каталоге job_remotion."
        )
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
    if render_pid is not None and not alive_render and alive_supervisor is False:
        st.update({
            "state": "error",
            "error": "process_died",
            "error_detail": (
                "Процесс Remotion (npx) завершился без записи кода выхода воркером. "
                "См. render.log в каталоге job_remotion."
            ),
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
                f"{int(st.get('progress_pct') or 0)}% · кадры "
                f"{st.get('frames_done') if st.get('frames_done') is not None else '?'}/"
                f"{st.get('frames_total') if st.get('frames_total') is not None else '?'}"
                + (f" · лог: {last_msg[:200]}" if last_msg else "")
            )
        st["state"] = "stuck"
    elif state == "stuck" and idle_sec < MONTAGE_RENDER_STUCK_SEC:
        st["state"] = "running"
    return st


def sync_render_state_disk(st: dict[str, Any]) -> dict[str, Any]:
    st = reconcile_render_state(st)
    persist_render_state(st)
    return st


def render_state_view(st: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "task_id": st.get("task_id"),
        "job_id": st.get("job_id"),
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
        "last_progress_at": st.get("last_progress_at"),
        "last_log_line": st.get("last_log_line"),
        "stuck_at": st.get("stuck_at"),
        "stuck_reason": st.get("stuck_reason"),
        "updated_at": st.get("updated_at"),
        "supervisor_pid": st.get("supervisor_pid"),
        "log_url": None,
    }
    fd = st.get("frames_done")
    ft = st.get("frames_total")
    if fd is not None and ft is not None:
        try:
            out["frames_done"] = int(fd)
            out["frames_total"] = int(ft)
        except (TypeError, ValueError):
            pass
    job_id = str(st.get("job_id") or "").strip()
    if job_id and render_log_path(job_id).is_file():
        out["has_render_log"] = True
    return out
