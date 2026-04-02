"""
ElevenLabs Text-to-Speech API (server-side only).

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
Лимиты символов на один запрос зависят от модели (см. TTS_MODELS и help.elevenlabs.io).
"""

from __future__ import annotations

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
    Если точек нет — один элемент (как есть).
    """
    t = text.strip()
    if not t:
        return []
    if "." not in t:
        return [t]
    out: list[str] = []
    for part in t.split("."):
        part = part.strip()
        if not part:
            continue
        out.append(part + ".")
    return out if out else [t]


def pack_sentences_into_chunks(sentences: list[str], max_chars: int) -> list[str]:
    """
    Склеиваем предложения в строки длиной не больше max_chars.
    Предложение длиннее max_chars режется на куски фиксированной длины.
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
    return chunks


def split_tts_text_into_chunks(text: str, max_chars: int) -> list[str]:
    """Публичная обёртка: точки → предложения → пакеты ≤ max_chars."""
    return pack_sentences_into_chunks(sentences_split_by_dot(text), max_chars)


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
