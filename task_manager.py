"""
Browser-independent background task manager for ReWrite long-running jobs.

Каждая задача:
  - Запускается в Python-потоке демоне.
  - События задачи (status / delta / result / error / любые pipeline-чекпоинты)
    пишутся в JSONL-лог на диск (`{project_dir}/_tasks/{task_id}.events.jsonl`),
    с моноtонно растущим `seq` per-event.
  - Метаданные (status, started_at, finished_at, kind, ref_id, last_seq, error)
    сохраняются в `{project_dir}/_tasks/{task_id}.meta.json` и обновляются на
    ключевых переходах + по таймеру.
  - В RAM хранится handle для live-tail (новые подписчики получают историю с
    диска + ждут новых событий через condition).

При перезапуске сервиса все задачи в статусе "running" автоматически
помечаются как "interrupted" (в этой реализации мы НЕ восстанавливаем
работу — браузер увидит, что задача оборвана, и сможет перезапустить).

API (для использования из app.py):
  - start_task(project_dir, kind, ref_id, target, request_payload)
  - get_active_task(project_dir, kind, ref_id) -> meta | None
  - subscribe_events(project_dir, task_id, since_seq=-1) -> generator (NDJSON-ready dicts)
  - cancel_task(project_dir, task_id) -> bool
  - get_task_meta(project_dir, task_id) -> meta | None
  - list_active_tasks_for_project(project_dir) -> list[meta]
  - mark_orphan_running_as_interrupted(root_jobs_dir) — вызывать на старте сервиса.

target — Callable[[emit_fn, cancel_event, request_payload], None].
emit_fn(event_dict) пишет событие в лог (синхронно).
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

# Тип target-функции, которую передают в start_task. Принимает:
#   emit:  Callable[[dict], None] — отправить событие
#   cancel_event: threading.Event — task target должен периодически проверять .is_set()
#   request_payload: dict — все входные данные, которые task должен использовать
TaskTarget = Callable[[Callable[[dict[str, Any]], None], threading.Event, dict[str, Any]], None]


# --- Storage layout helpers ---------------------------------------------------

_TASKS_SUBDIR = "_tasks"


def _tasks_dir(project_dir: Path) -> Path:
    d = project_dir / _TASKS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(project_dir: Path, task_id: str) -> Path:
    return _tasks_dir(project_dir) / f"{task_id}.meta.json"


def _events_path(project_dir: Path, task_id: str) -> Path:
    return _tasks_dir(project_dir) / f"{task_id}.events.jsonl"


# --- Process-wide registry ----------------------------------------------------


class _TaskHandle:
    __slots__ = (
        "task_id",
        "project_dir",
        "kind",
        "ref_id",
        "thread",
        "cancel_event",
        "lock",
        "cond",
        "last_seq",
        "status",
    )

    def __init__(
        self,
        task_id: str,
        project_dir: Path,
        kind: str,
        ref_id: str,
    ) -> None:
        self.task_id = task_id
        self.project_dir = project_dir
        self.kind = kind
        self.ref_id = ref_id
        self.thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.last_seq: int = -1
        self.status: str = "pending"


# task_id -> _TaskHandle
_handles: dict[str, _TaskHandle] = {}
# (project_dir, kind, ref_id) -> task_id  (актуальная активная задача)
_active_index: dict[tuple[str, str, str], str] = {}
# Глобальная блокировка реестра.
_registry_lock = threading.Lock()


def _index_key(project_dir: Path, kind: str, ref_id: str) -> tuple[str, str, str]:
    return (str(project_dir), str(kind or ""), str(ref_id or ""))


# --- Meta / disk helpers ------------------------------------------------------


def _read_meta_file(meta_path: Path) -> Optional[dict[str, Any]]:
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _write_meta_file(meta_path: Path, meta: dict[str, Any]) -> None:
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(meta_path)
    except OSError:
        # Best-effort: просто игнорируем ошибки записи мета (логи всё равно есть).
        pass


def _read_events_from_disk(events_path: Path, since_seq: int) -> list[dict[str, Any]]:
    """Читает все события с seq > since_seq."""
    if not events_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                seq = obj.get("seq")
                try:
                    s = int(seq)
                except (TypeError, ValueError):
                    continue
                if s > since_seq:
                    out.append(obj)
    except OSError:
        return out
    return out


def _append_event_to_disk(events_path: Path, event: dict[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        with events_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# --- Public API ---------------------------------------------------------------


def get_active_task(project_dir: Path, kind: str, ref_id: str) -> Optional[dict[str, Any]]:
    """Если для (kind, ref_id) есть RUNNING задача — вернуть её meta."""
    key = _index_key(project_dir, kind, ref_id)
    with _registry_lock:
        tid = _active_index.get(key)
    if not tid:
        # Дополнительно сканируем диск — задача могла быть запущена другим
        # процессом или сервис только что стартовал и реестр пуст.
        for meta in _scan_project_metas(project_dir):
            if (
                meta.get("kind") == kind
                and meta.get("ref_id") == ref_id
                and meta.get("status") == "running"
            ):
                return meta
        return None
    meta = _read_meta_file(_meta_path(project_dir, tid))
    if meta and meta.get("status") == "running":
        return meta
    return None


def get_task_meta(project_dir: Path, task_id: str) -> Optional[dict[str, Any]]:
    return _read_meta_file(_meta_path(project_dir, task_id))


def list_active_tasks_for_project(project_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for meta in _scan_project_metas(project_dir):
        if meta.get("status") == "running":
            out.append(meta)
    return out


def _scan_project_metas(project_dir: Path) -> list[dict[str, Any]]:
    d = project_dir / _TASKS_SUBDIR
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in d.glob("*.meta.json"):
        m = _read_meta_file(p)
        if isinstance(m, dict):
            out.append(m)
    return out


def cancel_task(project_dir: Path, task_id: str) -> bool:
    with _registry_lock:
        h = _handles.get(task_id)
    if h is not None:
        h.cancel_event.set()
        return True
    # Если в реестре нет — попробуем пометить мету (на случай orphan-run).
    meta = _read_meta_file(_meta_path(project_dir, task_id))
    if meta and meta.get("status") == "running":
        meta["status"] = "cancelled"
        meta["finished_at"] = time.time()
        meta["cancel_requested"] = True
        _write_meta_file(_meta_path(project_dir, task_id), meta)
        return True
    return False


def start_task(
    project_dir: Path,
    kind: str,
    ref_id: str,
    target: TaskTarget,
    request_payload: dict[str, Any],
    *,
    reuse_active: bool = True,
) -> dict[str, Any]:
    """Запускает задачу в фоновом потоке. Возвращает её meta-словарь.

    Если reuse_active=True и для (kind, ref_id) уже идёт running-задача — вернёт её.
    """
    if reuse_active:
        existing = get_active_task(project_dir, kind, ref_id)
        if existing:
            return existing

    project_dir.mkdir(parents=True, exist_ok=True)
    _tasks_dir(project_dir)

    task_id = uuid.uuid4().hex
    h = _TaskHandle(task_id=task_id, project_dir=project_dir, kind=kind, ref_id=ref_id)

    meta: dict[str, Any] = {
        "task_id": task_id,
        "kind": kind,
        "ref_id": ref_id,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "last_seq": -1,
        "error": None,
        "cancel_requested": False,
        "request_payload_keys": sorted(list(request_payload.keys())),
    }
    _write_meta_file(_meta_path(project_dir, task_id), meta)
    # Создаём пустой events-файл, чтобы tail сразу его видел.
    _events_path(project_dir, task_id).touch(exist_ok=True)

    h.status = "running"

    def _emit_factory() -> Callable[[dict[str, Any]], None]:
        events_path = _events_path(project_dir, task_id)
        meta_path = _meta_path(project_dir, task_id)

        def emit(event: dict[str, Any]) -> None:
            if not isinstance(event, dict):
                return
            with h.cond:
                h.last_seq += 1
                seq = h.last_seq
                stamped = dict(event)
                stamped["seq"] = seq
                stamped.setdefault("ts", time.time())
                _append_event_to_disk(events_path, stamped)
                # Каждые ~10 событий обновляем мету (чтобы reconnect знал last_seq).
                if seq % 10 == 0:
                    cur = _read_meta_file(meta_path) or meta
                    cur["last_seq"] = seq
                    _write_meta_file(meta_path, cur)
                h.cond.notify_all()
        return emit

    def _runner() -> None:
        emit = _emit_factory()
        meta_path = _meta_path(project_dir, task_id)
        ended_meta: Optional[dict[str, Any]] = None
        try:
            target(emit, h.cancel_event, request_payload)
            cur = _read_meta_file(meta_path) or meta
            if h.cancel_event.is_set() and cur.get("status") == "running":
                cur["status"] = "cancelled"
            elif cur.get("status") == "running":
                cur["status"] = "completed"
            cur["finished_at"] = time.time()
            cur["last_seq"] = h.last_seq
            ended_meta = cur
        except BaseException as e:  # noqa: BLE001 — нам важно записать любой crash
            tb = traceback.format_exc(limit=12)
            try:
                emit({"type": "error", "message": f"Task crash: {e}", "trace": tb})
            except Exception:
                pass
            cur = _read_meta_file(meta_path) or meta
            cur["status"] = "error"
            cur["error"] = f"{type(e).__name__}: {e}"
            cur["finished_at"] = time.time()
            cur["last_seq"] = h.last_seq
            ended_meta = cur
        finally:
            if ended_meta is not None:
                _write_meta_file(meta_path, ended_meta)
            with h.cond:
                h.status = (ended_meta or {}).get("status", "completed")
                h.cond.notify_all()
            # Снимаем активный индекс, оставляем handle ещё на короткое время,
            # чтобы поздние подписчики сразу увидели final meta из памяти.
            with _registry_lock:
                key = _index_key(project_dir, kind, ref_id)
                if _active_index.get(key) == task_id:
                    del _active_index[key]

    h.thread = threading.Thread(target=_runner, name=f"task-{task_id[:8]}", daemon=True)
    with _registry_lock:
        _handles[task_id] = h
        _active_index[_index_key(project_dir, kind, ref_id)] = task_id
    h.thread.start()

    return meta


def subscribe_events(
    project_dir: Path,
    task_id: str,
    *,
    since_seq: int = -1,
    poll_timeout: float = 15.0,
) -> Iterator[dict[str, Any]]:
    """Генератор NDJSON-готовых dict-событий: история с диска (seq > since_seq) +
    live-tail новых событий, пока задача running. Завершается, когда задача
    выходит из running И на диске нет событий с seq > последнего отданного.

    poll_timeout — максимум секунд ожидания на одной итерации; используется,
    чтобы гарантировать периодический keep-alive в подписке.
    """
    events_path = _events_path(project_dir, task_id)
    meta_path = _meta_path(project_dir, task_id)

    sent_seq = since_seq

    # 1) История с диска.
    history = _read_events_from_disk(events_path, sent_seq)
    for ev in history:
        sent_seq = max(sent_seq, int(ev.get("seq", sent_seq)))
        yield ev

    # 2) Если задача уже завершена — отдаём остаток (может прибавиться, пока читали историю) и выходим.
    with _registry_lock:
        h = _handles.get(task_id)

    def _is_finished_meta() -> bool:
        m = _read_meta_file(meta_path)
        if not m:
            return True
        return m.get("status") not in ("running", "pending")

    if h is None:
        # Задача нам неизвестна in-memory — добиваем хвост с диска и выходим.
        more = _read_events_from_disk(events_path, sent_seq)
        for ev in more:
            sent_seq = max(sent_seq, int(ev.get("seq", sent_seq)))
            yield ev
        return

    # 3) Live-tail.
    while True:
        with h.cond:
            # Если на диске уже есть новые события (могло прийти, пока мы спали),
            # выйдем из ожидания и обработаем их.
            if h.last_seq > sent_seq:
                pass
            else:
                if h.status not in ("running", "pending"):
                    # Финал: на диске мог накопиться остаток.
                    pass
                else:
                    h.cond.wait(timeout=poll_timeout)
        more = _read_events_from_disk(events_path, sent_seq)
        for ev in more:
            sent_seq = max(sent_seq, int(ev.get("seq", sent_seq)))
            yield ev
        if h.status not in ("running", "pending") and _is_finished_meta():
            # Дочитали хвост, и задача завершена.
            return


# --- Startup recovery ---------------------------------------------------------


def mark_orphan_running_as_interrupted(root_jobs_dir: Path) -> int:
    """На старте сервиса: помечаем все meta со status='running' как 'interrupted'.

    Возвращает сколько мета поменяли. Не пытаемся возобновить — задачи
    OpenAI/Claude всё равно потеряли HTTP-соединение, и пайплайн обычно
    нужен с правильным контекстом (snapshot формы), которого у нас нет
    после рестарта.
    """
    if not root_jobs_dir.is_dir():
        return 0
    changed = 0
    for project_dir in root_jobs_dir.iterdir():
        if not project_dir.is_dir():
            continue
        td = project_dir / _TASKS_SUBDIR
        if not td.is_dir():
            continue
        for mp in td.glob("*.meta.json"):
            m = _read_meta_file(mp)
            if not m:
                continue
            if m.get("status") == "running":
                m["status"] = "interrupted"
                m["finished_at"] = time.time()
                m["error"] = m.get("error") or "Сервер был перезапущен во время выполнения задачи."
                _write_meta_file(mp, m)
                changed += 1
    return changed
