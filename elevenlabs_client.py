"""
ElevenLabs Text-to-Speech API (server-side only).

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
Лимиты символов на один запрос зависят от модели (см. TTS_MODELS и help.elevenlabs.io).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import requests

ELEVEN_BASE = "https://api.elevenlabs.io"

# Ориентиры по лимитам на один запрос (ElevenLabs может менять — при 422 уменьшите текст).
TTS_MODELS: list[dict[str, Any]] = [
    {
        "id": "eleven_multilingual_v2",
        "label": "Multilingual v2",
        "max_chars": 10000,
        "hint": "~10 000 символов на запрос",
    },
    {
        "id": "eleven_turbo_v2_5",
        "label": "Turbo v2.5",
        "max_chars": 40000,
        "hint": "до ~40 000 символов",
    },
    {
        "id": "eleven_flash_v2_5",
        "label": "Flash v2.5",
        "max_chars": 40000,
        "hint": "до ~40 000 символов",
    },
    {
        "id": "eleven_v3",
        "label": "Eleven v3",
        "max_chars": 5000,
        "hint": "~5 000 символов",
    },
]

DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"


def _api_key() -> str:
    key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        raise ValueError("ELEVENLABS_API_KEY не задан в .env")
    return key


def max_chars_for_model(model_id: str) -> int:
    for m in TTS_MODELS:
        if m["id"] == model_id:
            return int(m["max_chars"])
    return 10000


def sentences_split_by_dot(text: str) -> list[str]:
    """
    Делим текст по символу точки (.). Каждый фрагмент снова заканчивается точкой.
    К предложению (кроме первого) дописывается ведущий пробел — чтобы при склейке
    в один чанк границы предложений не слипались в `"…elit.Lorem ipsum…"`.
    Если точек нет — один элемент (как есть).
    """
    t = text.strip()
    if not t:
        return []
    if "." not in t:
        return [t]
    out: list[str] = []
    for idx, part in enumerate(t.split(".")):
        part = part.strip()
        if not part:
            continue
        out.append((" " if out else "") + part + ".")
    return out if out else [t]


def pack_sentences_into_chunks(sentences: list[str], max_chars: int) -> list[str]:
    """
    Склеиваем предложения в строки длиной не больше max_chars.
    Предложение длиннее max_chars режется на куски фиксированной длины.

    Инварианты на выходе (assert):
      - каждый чанк ≤ max_chars;
      - ни одно входное предложение не появилось в двух чанках одновременно
        (соседняя дедупликация — защита от регрессий в логике flush()).
    """
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append("".join(buf))
            buf = []
            buf_len = 0

    for s in sentences:
        if len(s) > max_chars:
            flush()
            for i in range(0, len(s), max_chars):
                chunks.append(s[i : i + max_chars])
            continue
        if buf_len + len(s) <= max_chars:
            buf.append(s)
            buf_len += len(s)
        else:
            flush()
            buf = [s]
            buf_len = len(s)
    flush()

    # Защита от регрессий: ни один чанк не должен превышать лимита модели.
    # «Дубликаты соседних чанков» как сигнал не используем: при монотонном
    # источнике (одно длинное предложение без точек, например "А"*12000) слайсы
    # `s[i:i+max]` легитимно одинаковые — это не bug. Реальное дублирование
    # ловится через инвариант на суммарную длину в split_tts_text_into_chunks.
    for i, c in enumerate(chunks):
        if len(c) > max_chars:
            raise RuntimeError(
                f"pack_sentences_into_chunks: chunk[{i}] длиной {len(c)} > max_chars={max_chars}"
            )
    return chunks


def split_tts_text_into_chunks(text: str, max_chars: int) -> list[str]:
    """Публичная обёртка: точки → предложения → пакеты ≤ max_chars.

    После пакования делает sanity-check: длина «склейки» чанков должна совпадать
    с длиной нормализованного источника (получаемого через тот же
    `sentences_split_by_dot`). Это ловит любые регрессии, при которых одно и то же
    предложение случайно попало в два чанка.
    """
    sentences = sentences_split_by_dot(text)
    chunks = pack_sentences_into_chunks(sentences, max_chars)
    expected_len = sum(len(s) for s in sentences)
    actual_len = sum(len(c) for c in chunks)
    if actual_len != expected_len:
        raise RuntimeError(
            "split_tts_text_into_chunks: суммарная длина чанков "
            f"({actual_len}) ≠ нормализованной длине источника ({expected_len}). "
            "Возможна потеря/дублирование предложений."
        )
    return chunks


def merge_mp3_files_ffmpeg(part_paths: list[Path], out_path: Path) -> None:
    """Склейка MP3 через ffmpeg concat demuxer (-c copy)."""
    if not part_paths:
        raise ValueError("Нет фрагментов для склейки")
    if len(part_paths) == 1:
        shutil.copyfile(part_paths[0], out_path)
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "Установите ffmpeg на сервере (например: apt install ffmpeg) — нужен для склейки длинной озвучки."
        )

    def _escape_concat_path(p: Path) -> str:
        s = str(p.resolve())
        return s.replace("'", "'\\''")

    list_fd, list_path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(list_fd, "w") as f:
            for p in part_paths:
                f.write(f"file '{_escape_concat_path(p)}'\n")
        r = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"ffmpeg не смог склеить MP3: {err or r.returncode}")
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


def list_voices() -> list[dict[str, str]]:
    """Список голосов аккаунта: voice_id, name."""
    resp = requests.get(
        f"{ELEVEN_BASE}/v1/voices",
        headers={"xi-api-key": _api_key()},
        timeout=45,
    )
    data = resp.json()
    if resp.status_code != 200:
        msg = data.get("detail", {}).get("message") if isinstance(data.get("detail"), dict) else None
        raise RuntimeError(msg or data.get("message") or resp.text or f"HTTP {resp.status_code}")
    voices = data.get("voices") or []
    out: list[dict[str, str]] = []
    for v in voices:
        vid = v.get("voice_id") or v.get("voiceId")
        name = v.get("name") or vid or "?"
        if vid:
            out.append({"voice_id": vid, "name": name})
    out.sort(key=lambda x: x["name"].lower())
    return out


def pct_to_unit(pct: float | int) -> float:
    p = float(pct)
    if p < 0:
        p = 0.0
    if p > 100:
        p = 100.0
    return round(p / 100.0, 4)


def pct_to_speed(pct: float | int) -> float:
    """0% = медленнее (0.7), 100% = быстрее (1.2), как в UI ElevenLabs."""
    u = pct_to_unit(pct)
    return round(0.7 + u * 0.5, 4)


def text_to_speech_bytes(
    *,
    voice_id: str,
    text: str,
    model_id: str,
    stability_pct: float,
    similarity_pct: float,
    style_pct: float,
    speed_pct: float,
    use_speaker_boost: bool,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> bytes:
    url = f"{ELEVEN_BASE}/v1/text-to-speech/{voice_id}"
    params = {"output_format": output_format}
    payload: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": pct_to_unit(stability_pct),
            "similarity_boost": pct_to_unit(similarity_pct),
            "style": pct_to_unit(style_pct),
            "speed": pct_to_speed(speed_pct),
            "use_speaker_boost": bool(use_speaker_boost),
        },
    }
    resp = requests.post(
        url,
        params=params,
        headers={"xi-api-key": _api_key(), "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code == 200:
        return resp.content
    try:
        err = resp.json()
        detail = err.get("detail")
        if isinstance(detail, list) and detail:
            msg = detail[0].get("msg") or str(detail[0])
        elif isinstance(detail, dict):
            msg = detail.get("message") or str(detail)
        else:
            msg = err.get("message") or err.get("error") or resp.text
    except Exception:
        msg = resp.text or f"HTTP {resp.status_code}"
    raise RuntimeError(msg or f"ElevenLabs HTTP {resp.status_code}")


def _extract_eleven_error_message(resp: requests.Response) -> str:
    """Унифицированный разбор ошибки ElevenLabs (одинаково для всех endpoint)."""
    try:
        err = resp.json()
        detail = err.get("detail")
        if isinstance(detail, list) and detail:
            return str(detail[0].get("msg") or detail[0])
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        return str(err.get("message") or err.get("error") or resp.text or f"HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001 - best-effort error formatting
        return resp.text or f"ElevenLabs HTTP {resp.status_code}"


def text_to_speech_with_timestamps(
    *,
    voice_id: str,
    text: str,
    model_id: str,
    stability_pct: float,
    similarity_pct: float,
    style_pct: float,
    speed_pct: float,
    use_speaker_boost: bool,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> tuple[bytes, dict[str, Any]]:
    """POST /v1/text-to-speech/{voice_id}/with-timestamps.

    Возвращает (audio_bytes, alignment_dict), где alignment_dict — это
    `alignment` из ответа (с `characters`, `character_start_times_seconds`,
    `character_end_times_seconds`). При HTTP-ошибке/невалидном JSON поднимается
    RuntimeError с понятным сообщением.

    Для длинного текста (> max_chars модели) предварительно режьте через
    `split_tts_text_into_chunks` и вызывайте функцию по каждому чанку отдельно,
    накапливая offset длительности через `mp3_duration_seconds_ffprobe`.
    """
    url = f"{ELEVEN_BASE}/v1/text-to-speech/{voice_id}/with-timestamps"
    params = {"output_format": output_format}
    payload: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": pct_to_unit(stability_pct),
            "similarity_boost": pct_to_unit(similarity_pct),
            "style": pct_to_unit(style_pct),
            "speed": pct_to_speed(speed_pct),
            "use_speaker_boost": bool(use_speaker_boost),
        },
    }
    resp = requests.post(
        url,
        params=params,
        headers={"xi-api-key": _api_key(), "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(_extract_eleven_error_message(resp))
    try:
        data = resp.json()
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"ElevenLabs with-timestamps: невалидный JSON ({e}).") from e

    b64 = data.get("audio_base64")
    if not isinstance(b64, str) or not b64:
        raise RuntimeError(
            "ElevenLabs with-timestamps: модель не вернула audio_base64. "
            "Возможно, выбранная модель не поддерживает timestamps — попробуйте Multilingual v2 / Turbo v2.5 / Flash v2.5."
        )
    try:
        audio = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"ElevenLabs with-timestamps: некорректный base64 ({e}).") from e

    alignment = data.get("alignment")
    if not isinstance(alignment, dict):
        raise RuntimeError(
            "ElevenLabs with-timestamps: в ответе нет alignment. "
            "Возможно, модель не поддерживает timestamps (попробуйте Multilingual v2 / Turbo v2.5 / Flash v2.5)."
        )
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not (isinstance(chars, list) and isinstance(starts, list) and isinstance(ends, list)):
        raise RuntimeError("ElevenLabs with-timestamps: alignment без characters/times — неподдерживаемый ответ модели.")
    if not (len(chars) == len(starts) == len(ends)):
        raise RuntimeError(
            f"ElevenLabs with-timestamps: рассогласованные массивы alignment "
            f"(chars={len(chars)}, starts={len(starts)}, ends={len(ends)})."
        )
    return audio, alignment


def chars_to_words_ms(
    text: str,
    char_starts_seconds: list[float],
    char_ends_seconds: list[float],
    *,
    time_offset_ms: int = 0,
) -> list[dict[str, Any]]:
    """Преобразует char-alignment ElevenLabs в массив слов с миллисекундами.

    Слово = непрерывная последовательность не-пробельных символов исходного `text`.
    Пунктуация остаётся приклеенной к слову (`"Привет,"`, `"мир."`), что даёт
    естественные `end_ms` с учётом паузы после знака.

    `time_offset_ms` — глобальный сдвиг (накопленная длительность предыдущих чанков),
    позволяет собрать «сквозные» миллисекунды для многочанкового аудио.
    """
    if not (len(text) == len(char_starts_seconds) == len(char_ends_seconds)):
        raise RuntimeError(
            f"chars_to_words_ms: длины не совпадают (text={len(text)}, "
            f"starts={len(char_starts_seconds)}, ends={len(char_ends_seconds)})."
        )
    out: list[dict[str, Any]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not text[j].isspace():
            j += 1
        try:
            s_sec = float(char_starts_seconds[i])
            e_sec = float(char_ends_seconds[j - 1])
        except (TypeError, ValueError):
            i = j
            continue
        out.append(
            {
                "word": text[i:j],
                "start_ms": int(round(s_sec * 1000)) + int(time_offset_ms),
                "end_ms": int(round(e_sec * 1000)) + int(time_offset_ms),
            }
        )
        i = j
    return out


def mp3_duration_seconds_ffprobe(mp3_path: Path) -> float:
    """Длительность MP3 в секундах через `ffprobe`. 0.0 если ffprobe недоступен/файл пустой."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not Path(mp3_path).is_file():
        return 0.0
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
        return 0.0
    if r.returncode != 0:
        return 0.0
    raw = (r.stdout or "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
