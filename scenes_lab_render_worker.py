#!/usr/bin/env python3
"""MP4-рендер LaterInfographic для /scenes-lab."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from montage_render_shared import REMOTION_DIR, REMOTION_NPX, parse_remotion_log_line, touch_progress
from scenes_lab_remotion import (
    load_lab_render_state,
    persist_lab_render_state,
    remotion_out_path,
    remotion_props_path,
    render_log_path,
)

_RENDER_PROC: subprocess.Popen[str] | None = None


def _log_write(log_f: TextIO, line: str) -> None:
    log_f.write(line)
    if not line.endswith("\n"):
        log_f.write("\n")
    log_f.flush()


def _apply_cancel(st: dict[str, Any]) -> bool:
    return bool(st.get("cancel_requested"))


def _kill_render_tree() -> None:
    global _RENDER_PROC
    proc = _RENDER_PROC
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    _RENDER_PROC = None


def _reload_cancel_flag(st: dict[str, Any], task_id: str) -> dict[str, Any]:
    disk = load_lab_render_state()
    if not disk or str(disk.get("task_id") or "") != task_id:
        return st
    if disk.get("cancel_requested"):
        st["cancel_requested"] = True
    return st


def run_render(task_id: str) -> int:
    global _RENDER_PROC
    props_path = remotion_props_path()
    out_path = remotion_out_path()
    log_path = render_log_path()

    st = load_lab_render_state() or {}
    if str(st.get("task_id") or "") != task_id:
        st = {
            "task_id": task_id,
            "job_id": "scenes_lab",
            "state": "queued",
            "progress_pct": 0,
            "stage": "queued",
            "message": "Старт воркера",
            "started_at": time.time(),
            "last_progress_at": time.time(),
        }
    st["supervisor_pid"] = os.getpid()
    st["state"] = "running"
    st["stage"] = "starting"
    st["message"] = "Воркер рендера scenes-lab запущен"
    persist_lab_render_state(st)

    if not props_path.is_file():
        st.update({
            "state": "error",
            "error": "no_props",
            "message": "props.json не найден",
            "finished_at": time.time(),
        })
        persist_lab_render_state(st)
        return 2

    if out_path.exists():
        try:
            out_path.unlink()
        except OSError:
            pass

    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_path.write_text(
            f"=== scenes-lab render worker pid={os.getpid()} task={task_id} ===\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    env = dict(os.environ)
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")

    cmd = [
        REMOTION_NPX,
        "--no",
        "remotion",
        "render",
        "src/index.ts",
        "LaterInfographic",
        str(out_path.resolve()),
        f"--props={props_path.resolve()}",
        "--log=info",
    ]

    touch_progress(st, stage="bundle", message="Запуск Remotion render…")
    persist_lab_render_state(st)

    last_line = ""
    rc = 1

    with log_path.open("a", encoding="utf-8", buffering=1) as log_f:
        _log_write(log_f, f"$ {' '.join(cmd)}\n")
        _log_write(log_f, f"cwd={REMOTION_DIR}\n")

        try:
            _RENDER_PROC = subprocess.Popen(
                cmd,
                cwd=str(REMOTION_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            st.update({
                "state": "error",
                "error": "spawn_failed",
                "error_detail": f"Не удалось запустить npx remotion: {exc}",
                "message": str(exc),
                "finished_at": time.time(),
            })
            _log_write(log_f, f"SPAWN ERROR: {exc}\n")
            persist_lab_render_state(st)
            return 3

        st["pid"] = _RENDER_PROC.pid
        touch_progress(st, message=f"npx pid={_RENDER_PROC.pid}")
        persist_lab_render_state(st)

        if _RENDER_PROC.stdout is not None:
            for raw in _RENDER_PROC.stdout:
                st = _reload_cancel_flag(st, task_id)
                if _apply_cancel(st):
                    _log_write(log_f, "CANCEL: запрошена остановка\n")
                    _kill_render_tree()
                    st.update({
                        "state": "cancelled",
                        "stage": "cancelled",
                        "error": "cancelled",
                        "message": "Рендер остановлен пользователем.",
                        "finished_at": time.time(),
                    })
                    persist_lab_render_state(st)
                    return 0

                line = raw.rstrip("\n")
                last_line = line
                _log_write(log_f, line)
                upd = parse_remotion_log_line(line)
                if "message" in upd:
                    touch_progress(st, **{k: v for k, v in upd.items() if k != "last_log_line"})
                    st.update(upd)
                    persist_lab_render_state(st)

        try:
            rc = _RENDER_PROC.wait()
        except Exception:
            rc = 1
        _RENDER_PROC = None
        _log_write(log_f, f"EXIT rc={rc}\n")

    st = load_lab_render_state() or st
    if _apply_cancel(st):
        st.update({
            "state": "cancelled",
            "stage": "cancelled",
            "error": "cancelled",
            "message": "Рендер остановлен пользователем.",
            "finished_at": time.time(),
        })
        persist_lab_render_state(st)
        return 0

    if rc == 0 and out_path.is_file():
        st.update({
            "state": "done",
            "progress_pct": 100,
            "stage": "done",
            "message": "Рендер MP4 завершён.",
            "finished_at": time.time(),
            "exit_code": 0,
            "output_filename": "out.mp4",
            "output_url": "/scenes-lab/remotion/file/out.mp4",
            "error": None,
            "error_detail": None,
        })
        persist_lab_render_state(st)
        return 0

    prev_stage = str(st.get("stage") or "error")
    err_msg = ("Ошибка рендера: " + (last_line[:240] if last_line else "")) or "Ошибка рендера"
    st.update({
        "state": "error",
        "stage": prev_stage,
        "error": f"exit_code={rc}",
        "exit_code": rc,
        "error_detail": err_msg,
        "last_log_line": last_line[:500] if last_line else None,
        "message": err_msg,
        "finished_at": time.time(),
    })
    persist_lab_render_state(st)
    return rc if rc != 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Scenes-lab LaterInfographic render worker")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    task_id = str(args.task_id).strip()
    if not task_id:
        print("task-id required", file=sys.stderr)
        return 2

    def _on_term(_signum: int, _frame: Any) -> None:
        st = load_lab_render_state()
        if st and str(st.get("task_id") or "") == task_id:
            st["cancel_requested"] = True
            persist_lab_render_state(st)
        _kill_render_tree()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    return run_render(task_id)


if __name__ == "__main__":
    raise SystemExit(main())
