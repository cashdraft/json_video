"""
Локальный транскрайбер для пословных таймингов из MP3.

Пайплайн по умолчанию:
  1. **faster-whisper** — распознавание (модель ``small``, VAD, анти-галлюцинации)
  2. **WhisperX align** — уточнение границ слов (wav2vec2, ±20 ms)

``WHISPER_ALIGN=0`` — только слова из faster-whisper (быстрее, грубее).
``WHISPER_ENGINE=whisperx`` — полный цикл WhisperX (тяжелее, без faster-whisper).

Формат JSON: ``whisper_words@1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

_MODEL_CACHE: dict[str, Any] = {}
_ALIGN_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()
_HEARTBEAT_INTERVAL_S = 10

_DEFAULT_MODEL = "small"
_DEFAULT_ALIGN_RU = "jonatasgrosman/wav2vec2-large-xlsr-53-russian"

_ASR_OPTIONS = {
    "beam_size": 5,
    "best_of": 5,
    "temperatures": [0.0],
    "compression_ratio_threshold": 2.4,
    "no_speech_threshold": 0.6,
    "condition_on_previous_text": False,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _resolve_engine_mode() -> str:
    """``faster-whisper`` (по умолчанию) или ``whisperx`` (полный ASR через WhisperX)."""
    raw = (os.getenv("WHISPER_ENGINE") or "faster-whisper").strip().lower()
    if raw in ("whisperx", "wx"):
        return "whisperx"
    return "faster-whisper"


def _use_whisperx_align() -> bool:
    """После faster-whisper прогонять wav2vec2 align (WhisperX). По умолчанию да."""
    if _resolve_engine_mode() == "whisperx":
        return True
    return _env_bool("WHISPER_ALIGN", True)


def _resolve_model_name() -> str:
    return (os.getenv("WHISPER_MODEL") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def _resolve_align_model(language: str | None) -> str | None:
    explicit = (os.getenv("WHISPER_ALIGN_MODEL") or "").strip()
    if explicit:
        return explicit
    lang = (language or "ru").strip().lower()[:2]
    if lang == "ru":
        return _DEFAULT_ALIGN_RU
    return None


def _resolve_vad_method(device: str) -> str:
    raw = (os.getenv("WHISPER_VAD_METHOD") or "").strip().lower()
    if raw in ("silero", "pyannote"):
        return raw
    return "silero" if device == "cpu" else "pyannote"


def _resolve_device() -> tuple[str, str]:
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


def _resolve_batch_size(device: str) -> int:
    raw = (os.getenv("WHISPER_BATCH_SIZE") or "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return 8 if device == "cuda" else 4


def _audio_duration_ms_ffprobe(mp3_path: Path) -> int:
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
    try:
        return int(round(float((r.stdout or "").strip()) * 1000))
    except (TypeError, ValueError):
        return 0


def _words_from_whisperx_aligned(aligned: dict[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for w in aligned.get("word_segments") or []:
        if not isinstance(w, dict):
            continue
        text = str(w.get("word") or "").strip()
        if not text:
            continue
        try:
            start_s = float(w["start"])
            end_s = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        s_ms = max(0, int(round(start_s * 1000)))
        e_ms = max(s_ms, int(round(end_s * 1000)))
        key = (s_ms, e_ms, text)
        if key in seen:
            continue
        seen.add(key)
        words.append({"word": text, "start_ms": s_ms, "end_ms": e_ms})
    if words:
        return words
    for seg in aligned.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        for w in seg.get("words") or []:
            if not isinstance(w, dict):
                continue
            text = str(w.get("word") or "").strip()
            if not text:
                continue
            try:
                start_s = float(w["start"])
                end_s = float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            s_ms = max(0, int(round(start_s * 1000)))
            e_ms = max(s_ms, int(round(end_s * 1000)))
            words.append({"word": text, "start_ms": s_ms, "end_ms": e_ms})
    return words


def _words_from_faster_whisper_segments(segments_iter: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Слова FW + сегменты для WhisperX align."""
    words: list[dict[str, Any]] = []
    wx_segments: list[dict[str, Any]] = []
    seg_count = 0
    last_end_ms = 0
    for seg in segments_iter:
        seg_count += 1
        text = (getattr(seg, "text", "") or "").strip()
        try:
            seg_start = float(getattr(seg, "start", 0.0) or 0.0)
            seg_end = float(getattr(seg, "end", 0.0) or 0.0)
        except (TypeError, ValueError):
            seg_start, seg_end = 0.0, 0.0
        if text:
            wx_segments.append({"text": text, "start": seg_start, "end": seg_end})
        for w in getattr(seg, "words", None) or []:
            try:
                start_s = float(w.start) if w.start is not None else None
                end_s = float(w.end) if w.end is not None else None
            except (TypeError, ValueError):
                continue
            if start_s is None or end_s is None:
                continue
            wtext = (getattr(w, "word", "") or "").strip()
            if not wtext:
                continue
            s_ms = max(0, int(round(start_s * 1000)))
            e_ms = max(s_ms, int(round(end_s * 1000)))
            words.append({"word": wtext, "start_ms": s_ms, "end_ms": e_ms})
            last_end_ms = max(last_end_ms, e_ms)
    return words, wx_segments


def _whisperx_align_segments(
    mp3_path: Path,
    segments: list[dict[str, Any]],
    *,
    language: str,
    device: str,
    on_progress: Callable[[dict[str, Any]], None] | None,
    total_ms_estimate: int,
) -> tuple[list[dict[str, Any]], str]:
    import whisperx  # type: ignore[import-not-found]

    def _emit(payload: dict[str, Any]) -> None:
        if on_progress:
            try:
                on_progress(payload)
            except Exception:
                pass

    if not segments:
        return [], ""

    align_name = _resolve_align_model(language)
    align_key = f"align::{align_name}::{device}"
    with _MODEL_LOCK:
        cached = _ALIGN_CACHE.get(align_key)
        if cached is None:
            _emit({"stage": "align_model_load", "align_model": align_name or "auto"})
            align_model, align_metadata = whisperx.load_align_model(
                language_code=language,
                device=device,
                model_name=align_name,
            )
            _ALIGN_CACHE[align_key] = (align_model, align_metadata)
        else:
            align_model, align_metadata = cached

    _emit({"stage": "align_start", "align_model": align_name or language})
    audio = whisperx.load_audio(str(mp3_path))

    def _align_progress(pct: float) -> None:
        _emit(
            {
                "stage": "align",
                "progress_pct": round(pct, 1),
                "current_ms": int(total_ms_estimate * min(1.0, (50 + pct / 2) / 100.0))
                if total_ms_estimate
                else 0,
                "total_ms": total_ms_estimate,
            }
        )

    aligned = whisperx.align(
        segments,
        align_model,
        align_metadata,
        audio,
        device,
        return_char_alignments=False,
        print_progress=False,
        combined_progress=True,
        progress_callback=_align_progress,
    )
    return _words_from_whisperx_aligned(aligned), (align_name or language)


def _transcribe_whisperx_full(
    mp3_path: Path,
    *,
    language: str | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    total_ms_estimate: int,
) -> dict[str, Any]:
    """Полный WhisperX (ASR + align) — только если WHISPER_ENGINE=whisperx."""
    import whisperx  # type: ignore[import-not-found]

    name = _resolve_model_name()
    device, compute_type = _resolve_device()
    batch_size = _resolve_batch_size(device)
    vad_method = _resolve_vad_method(device)
    lang = (language or os.getenv("WHISPER_LANGUAGE") or None)
    lang = lang.strip().lower() if isinstance(lang, str) and lang.strip() else None

    def _emit(payload: dict[str, Any]) -> None:
        if on_progress:
            try:
                on_progress(payload)
            except Exception:
                pass

    cache_key = f"wx::{name}::{device}::{compute_type}::{vad_method}::{lang or ''}"
    with _MODEL_LOCK:
        asr = _MODEL_CACHE.get(cache_key)
        if asr is None:
            _emit({"stage": "model_load", "model": name, "engine": "whisperx"})
            asr = whisperx.load_model(
                name,
                device,
                compute_type=compute_type,
                language=lang,
                vad_method=vad_method,
                asr_options=dict(_ASR_OPTIONS),
            )
            _MODEL_CACHE[cache_key] = asr

    _emit(
        {
            "stage": "model_ready",
            "model": name,
            "engine": "whisperx",
            "device": device,
            "compute_type": compute_type,
            "vad_method": vad_method,
        }
    )

    audio = whisperx.load_audio(str(mp3_path))
    _emit({"stage": "transcribe_start", "message": "Транскрипция WhisperX…"})
    result = asr.transcribe(
        audio,
        batch_size=batch_size,
        language=lang,
        print_progress=False,
        verbose=False,
    )
    detected_lang = str(result.get("language") or lang or "ru")
    words, align_name = _whisperx_align_segments(
        mp3_path,
        result.get("segments") or [],
        language=detected_lang,
        device=device,
        on_progress=on_progress,
        total_ms_estimate=total_ms_estimate,
    )
    last_end_ms = max((w["end_ms"] for w in words), default=0)
    final_total_ms = max(total_ms_estimate, last_end_ms)
    return {
        "schema": "whisper_words@1",
        "engine": "whisperx",
        "model": name,
        "align_model": align_name,
        "device": device,
        "compute_type": compute_type,
        "vad_method": vad_method,
        "language": detected_lang,
        "language_probability": None,
        "audio_filename": mp3_path.name,
        "total_words": len(words),
        "total_duration_ms": int(final_total_ms),
        "words": words,
        "asr_options": dict(_ASR_OPTIONS),
    }


def _load_faster_whisper_model() -> tuple[Any, str, str, str]:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    name = _resolve_model_name()
    device, compute_type = _resolve_device()
    key = f"fw::{name}::{device}::{compute_type}"
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached, name, device, compute_type
        model = WhisperModel(name, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
        return model, name, device, compute_type


def _transcribe_faster_whisper(
    mp3_path: Path,
    *,
    language: str | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    total_ms_estimate: int,
) -> dict[str, Any]:
    def _emit(payload: dict[str, Any]) -> None:
        if on_progress:
            try:
                on_progress(payload)
            except Exception:
                pass

    name = _resolve_model_name()
    device, compute_type = _resolve_device()
    use_align = _use_whisperx_align()

    _emit(
        {
            "stage": "model_load",
            "model": name,
            "engine": "faster-whisper",
            "whisperx_align": use_align,
        }
    )
    model, name, device, compute_type = _load_faster_whisper_model()
    _emit(
        {
            "stage": "model_ready",
            "model": name,
            "engine": "faster-whisper",
            "device": device,
            "compute_type": compute_type,
            "whisperx_align": use_align,
        }
    )

    audio_min = max(1, round(total_ms_estimate / 60_000)) if total_ms_estimate > 0 else 0
    dur_hint = f" (~{audio_min} мин аудио)" if audio_min else ""
    _emit(
        {
            "stage": "transcribe_start",
            "message": f"Распознавание{dur_hint}… (на {device}, это может занять много минут)",
        }
    )

    segments_iter, info = model.transcribe(
        str(mp3_path),
        word_timestamps=True,
        vad_filter=True,
        beam_size=_ASR_OPTIONS["beam_size"],
        temperature=0.0,
        compression_ratio_threshold=_ASR_OPTIONS["compression_ratio_threshold"],
        no_speech_threshold=_ASR_OPTIONS["no_speech_threshold"],
        condition_on_previous_text=_ASR_OPTIONS["condition_on_previous_text"],
        language=(language or None),
    )

    info_duration_ms = 0
    try:
        info_duration_ms = int(round(float(getattr(info, "duration", 0.0) or 0.0) * 1000))
    except (TypeError, ValueError):
        info_duration_ms = 0
    total_ms = max(total_ms_estimate, info_duration_ms)

    fw_words, wx_segments = _words_from_faster_whisper_segments(segments_iter)
    detected_lang = str(getattr(info, "language", None) or language or "ru")
    lang_prob = float(getattr(info, "language_probability", 0.0) or 0.0)

    words = fw_words
    align_model_name: str | None = None
    align_error: str | None = None

    if use_align and wx_segments:
        try:
            aligned_words, align_model_name = _whisperx_align_segments(
                mp3_path,
                wx_segments,
                language=detected_lang,
                device=device,
                on_progress=on_progress,
                total_ms_estimate=total_ms,
            )
            if aligned_words:
                words = aligned_words
        except Exception as exc:
            align_error = str(exc)[:500]
            if on_progress:
                try:
                    on_progress(
                        {
                            "stage": "align_fallback",
                            "message": f"WhisperX align: {align_error}; оставляем тайминги faster-whisper.",
                        }
                    )
                except Exception:
                    pass

    last_end_ms = max((w["end_ms"] for w in words), default=0)
    final_total_ms = max(total_ms, last_end_ms)

    doc: dict[str, Any] = {
        "schema": "whisper_words@1",
        "engine": "faster-whisper",
        "model": name,
        "device": device,
        "compute_type": compute_type,
        "vad_filter": True,
        "language": detected_lang,
        "language_probability": lang_prob,
        "audio_filename": mp3_path.name,
        "total_words": len(words),
        "total_duration_ms": int(final_total_ms),
        "words": words,
        "asr_options": dict(_ASR_OPTIONS),
    }
    if use_align:
        doc["whisperx_align"] = True
        doc["align_model"] = align_model_name
        if align_error:
            doc["align_error"] = align_error
    else:
        doc["whisperx_align"] = False
        doc["align_model"] = None

    return doc


def transcribe_words_streaming(
    mp3_path: Path,
    *,
    language: str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not mp3_path.is_file():
        raise FileNotFoundError(f"audio not found: {mp3_path}")

    total_ms_probe = _audio_duration_ms_ffprobe(mp3_path)

    if _resolve_engine_mode() == "whisperx":
        doc = _transcribe_whisperx_full(
            mp3_path,
            language=language,
            on_progress=on_progress,
            total_ms_estimate=total_ms_probe,
        )
    else:
        doc = _transcribe_faster_whisper(
            mp3_path,
            language=language,
            on_progress=on_progress,
            total_ms_estimate=total_ms_probe,
        )

    if on_progress:
        try:
            on_progress(
                {
                    "stage": "done",
                    "words": doc.get("total_words", 0),
                    "total_duration_ms": doc.get("total_duration_ms", 0),
                    "engine": doc.get("engine"),
                    "model": doc.get("model"),
                    "align_model": doc.get("align_model"),
                }
            )
        except Exception:
            pass
    return doc


def iter_progress_events(
    mp3_path: Path,
    *,
    language: str | None = None,
) -> Iterator[dict[str, Any]]:
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
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
        finally:
            q.put({"stage": "__done__"})

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    while True:
        try:
            ev = q.get(timeout=_HEARTBEAT_INTERVAL_S)
        except _queue.Empty:
            yield {
                "stage": "heartbeat",
                "message": "Распознавание продолжается… (на CPU это может занять много минут)",
            }
            continue
        if ev.get("stage") == "__done__":
            break
        yield ev
    if result["error"]:
        yield {"stage": "error", "error": result["error"]}
    elif result["doc"] is not None:
        yield {"stage": "final", "doc": result["doc"]}
