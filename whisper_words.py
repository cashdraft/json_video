"""
Локальный Whisper-транскрайбер (faster-whisper) для пословных таймингов из MP3.

Используется как «второе мнение» к character-alignment ElevenLabs: на случай,
когда eleven_v3 отдаёт битый alignment, Whisper всё равно даст реальные
`start_ms` / `end_ms` для каждого слова исходной озвучки.

Формат итогового JSON совпадает с `elevenlabs_with_timestamps_words@1`, чтобы
`align_scenes_to_word_timings` мог его использовать без модификаций.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

# faster-whisper грузим лениво — это тяжёлый импорт и тянет ctranslate2/torch.
# Также внутри транскрипции модель грузится один раз и кэшируется в _MODEL_CACHE.

_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()


def _resolve_model_name() -> str:
    """Размер модели faster-whisper. Можно переопределить через WHISPER_MODEL.

    Хорошие компромиссы скорости/качества на CPU:
      - tiny   (~75 MB):  очень быстро, заметно ошибается на длинных словах.
      - base   (~145 MB): быстро, разумное качество.
      - small  (~480 MB): по умолчанию — лучшая точность пословных таймингов
                          среди CPU-приемлемых моделей.
      - medium (~1.5 GB): заметно медленнее, нужен запас RAM.
    """
    return (os.getenv("WHISPER_MODEL") or "small").strip() or "small"


def _resolve_device() -> tuple[str, str]:
    """Возвращает (device, compute_type) для faster-whisper.

    На CPU `int8` даёт ~2-3x ускорение почти без потери точности; на CUDA —
    `float16`. Можно переопределить через WHISPER_DEVICE / WHISPER_COMPUTE_TYPE.
    """
    dev = (os.getenv("WHISPER_DEVICE") or "").strip().lower()
    if not dev:
        try:
            r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=2)
            dev = "cuda" if (r.returncode == 0 and r.stdout.strip()) else "cpu"
        except (OSError, subprocess.SubprocessError):
            dev = "cpu"
    ct = (os.getenv("WHISPER_COMPUTE_TYPE") or "").strip().lower()
    if not ct:
        ct = "float16" if dev == "cuda" else "int8"
    return dev, ct


def _load_model() -> tuple[Any, str, str, str]:
    """Singleton-загрузка faster-whisper. Возвращает (model, name, device, compute_type)."""
    name = _resolve_model_name()
    device, compute_type = _resolve_device()
    key = f"{name}::{device}::{compute_type}"
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached, name, device, compute_type
        # Импорт внутри — иначе старт Flask стал бы на ~2-3 секунды дольше.
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model = WhisperModel(name, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
        return model, name, device, compute_type


def _audio_duration_ms_ffprobe(mp3_path: Path) -> int:
    """Длительность аудио в мс через ffprobe; 0 если ffprobe недоступен/файл пуст."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not mp3_path.is_file():
        return 0
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if r.returncode != 0:
        return 0
    raw = (r.stdout or "").strip()
    try:
        return int(round(float(raw) * 1000))
    except (TypeError, ValueError):
        return 0


def transcribe_words_streaming(
    mp3_path: Path,
    *,
    language: str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Транскрибирует MP3 через faster-whisper с пословными таймингами.

    Возвращает dict со схемой ``whisper_words@1`` (совместимый по полям ``words[*]``
    с ``elevenlabs_with_timestamps_words@1``).

    ``on_progress`` (если задан) получает события на каждом сегменте:
      - ``{"stage":"model_load", "model":..., "device":..., "compute_type":...}``
      - ``{"stage":"segment", "segment_index":N, "segment_total_known":bool,
            "current_ms":int, "total_ms":int, "words_so_far":int}``
      - ``{"stage":"done", "words":N, "total_duration_ms":int}``
    Любое исключение в коллбеке проглатывается — это не должно ронять транскрипцию.
    """
    def _emit(payload: dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(payload)
        except Exception:
            pass

    if not mp3_path.is_file():
        raise FileNotFoundError(f"audio not found: {mp3_path}")

    total_ms_probe = _audio_duration_ms_ffprobe(mp3_path)

    _emit({"stage": "model_load", "model": _resolve_model_name()})
    model, name, device, compute_type = _load_model()
    _emit(
        {
            "stage": "model_ready",
            "model": name,
            "device": device,
            "compute_type": compute_type,
            "total_ms_estimate": total_ms_probe,
        }
    )

    segments_iter, info = model.transcribe(
        str(mp3_path),
        word_timestamps=True,
        vad_filter=False,
        beam_size=1,
        language=(language or None),
        condition_on_previous_text=False,
    )

    info_duration_ms = 0
    try:
        info_duration_ms = int(round(float(getattr(info, "duration", 0.0) or 0.0) * 1000))
    except (TypeError, ValueError):
        info_duration_ms = 0
    total_ms = max(total_ms_probe, info_duration_ms)

    words: list[dict[str, Any]] = []
    seg_count = 0
    last_end_ms = 0
    for seg in segments_iter:
        seg_count += 1
        seg_words = getattr(seg, "words", None) or []
        for w in seg_words:
            try:
                start_s = float(w.start) if w.start is not None else None
                end_s = float(w.end) if w.end is not None else None
            except (TypeError, ValueError):
                continue
            if start_s is None or end_s is None:
                continue
            text = (getattr(w, "word", "") or "").strip()
            if not text:
                continue
            s_ms = max(0, int(round(start_s * 1000)))
            e_ms = max(s_ms, int(round(end_s * 1000)))
            words.append({"word": text, "start_ms": s_ms, "end_ms": e_ms})
            last_end_ms = max(last_end_ms, e_ms)
        cur_ms = int(round(float(getattr(seg, "end", 0.0) or 0.0) * 1000))
        _emit(
            {
                "stage": "segment",
                "segment_index": seg_count,
                "current_ms": cur_ms,
                "total_ms": total_ms,
                "words_so_far": len(words),
            }
        )

    final_total_ms = max(total_ms, last_end_ms)
    doc: dict[str, Any] = {
        "schema": "whisper_words@1",
        "engine": "faster-whisper",
        "model": name,
        "device": device,
        "compute_type": compute_type,
        "language": getattr(info, "language", None),
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "audio_filename": mp3_path.name,
        "total_words": len(words),
        "total_duration_ms": int(final_total_ms),
        "words": words,
    }
    _emit({"stage": "done", "words": len(words), "total_duration_ms": int(final_total_ms)})
    return doc


def iter_progress_events(
    mp3_path: Path,
    *,
    language: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Удобный генератор для NDJSON-стриминга: yield-ит события + финальный
    ``{"stage":"final", "doc":...}`` с готовым документом.

    Использует фоновый поток + очередь, поскольку ``model.transcribe`` блокирующий.
    """
    import queue as _queue

    q: _queue.Queue[dict[str, Any]] = _queue.Queue()
    result: dict[str, Any] = {"doc": None, "error": None}

    def _runner() -> None:
        try:
            doc = transcribe_words_streaming(
                mp3_path,
                language=language,
                on_progress=lambda ev: q.put(ev),
            )
            result["doc"] = doc
        except Exception as e:  # noqa: BLE001 — пробросим в основной поток NDJSON
            result["error"] = str(e)
        finally:
            q.put({"stage": "__done__"})

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    while True:
        try:
            ev = q.get(timeout=30)
        except _queue.Empty:
            yield {"stage": "heartbeat"}
            continue
        if ev.get("stage") == "__done__":
            break
        yield ev
    if result["error"]:
        yield {"stage": "error", "error": result["error"]}
    elif result["doc"] is not None:
        yield {"stage": "final", "doc": result["doc"]}
