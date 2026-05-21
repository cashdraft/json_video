"""
ElevenLabs Text-to-Speech API (server-side only).

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
Озвучка только через Eleven v3 (~5000 символов на один запрос; длинный текст режется по предложениям).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import requests

ELEVEN_BASE = "https://api.elevenlabs.io"

TTS_MODEL_ID = "eleven_v3"
TTS_MAX_CHARS = 5000

# Дробление with-timestamps: на длинных кусках Eleven чаще «схлопывает» alignment.
# Для текста > MIN — ориентир ~TARGET_CHUNKS запросов (для 2–3 тыс. симв. обычно 2–4 куска).
TTS_TIMESTAMPS_MIN_TEXT_FOR_SPLIT = 1500
TTS_TIMESTAMPS_TARGET_CHUNKS = 3
TTS_TIMESTAMPS_MIN_CHUNK_CHARS = 400

# Eleven v3: нарезка по предложениям, ориентир ~1000–1500 символов на кусок.
TTS_V3_CHUNK_MIN = 1000
TTS_V3_CHUNK_TARGET = 1250
TTS_V3_CHUNK_MAX = 1500

TTS_MODELS: list[dict[str, Any]] = [
    {
        "id": TTS_MODEL_ID,
        "label": "Eleven v3",
        "max_chars": TTS_MAX_CHARS,
        "hint": "~5 000 символов",
    },
]


def normalize_tts_model_id(value: str | None) -> str:
    """Всегда eleven_v3 (старые model_id из job игнорируются)."""
    return TTS_MODEL_ID


def normalize_tts_script_source(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("manual", "none", "off", ""):
        return "manual"
    if raw in ("elevenlabs_editor", "elevenlabs", "el"):
        return "elevenlabs_editor"
    return "voiceover_editor"

DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"


def _api_key() -> str:
    key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        raise ValueError("ELEVENLABS_API_KEY не задан в .env")
    return key


def max_chars_for_model(model_id: str | None = None) -> int:
    return TTS_MAX_CHARS


def max_chars_for_tts_with_timestamps(text: str, model_id: str | None = None) -> int:
    """Лимит символов на один запрос with-timestamps."""
    model_max = max_chars_for_model(model_id)
    if normalize_tts_model_id(model_id) == TTS_MODEL_ID:
        return min(model_max, TTS_V3_CHUNK_MAX)
    n = len((text or "").strip())
    if n <= TTS_TIMESTAMPS_MIN_TEXT_FOR_SPLIT:
        return model_max
    target = max(2, TTS_TIMESTAMPS_TARGET_CHUNKS)
    alignment_limit = (n + target - 1) // target
    alignment_limit = max(TTS_TIMESTAMPS_MIN_CHUNK_CHARS, alignment_limit)
    return min(model_max, alignment_limit)


_BRACKET_TAG_END_RE = re.compile(r"\[[^\]]+\]\s*$")
_BRACKET_TAG_START_RE = re.compile(r"^\s*\[")


def _ends_with_bracket_tag(s: str) -> bool:
    return bool(_BRACKET_TAG_END_RE.search((s or "").rstrip()))


def _split_oversized_sentence(sentence: str, max_len: int) -> list[str]:
    s = sentence.strip()
    if len(s) <= max_len:
        return [s] if s else []
    parts = re.split(r"(?<=[,;])\s+", s)
    out: list[str] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        trial = f"{buf} {part}".strip() if buf else part
        if len(trial) <= max_len:
            buf = trial
        else:
            if buf:
                out.append(buf)
            if len(part) > max_len:
                start = 0
                while start < len(part):
                    chunk = part[start : start + max_len]
                    if start + max_len < len(part):
                        sp = chunk.rfind(" ")
                        if sp > max_len // 3:
                            chunk = chunk[:sp]
                            start += sp
                        else:
                            start += max_len
                    else:
                        start = len(part)
                    if chunk.strip():
                        out.append(chunk.strip())
                buf = ""
            else:
                buf = part
    if buf:
        out.append(buf)
    return out


def smart_chunk_tts_v3(
    text: str,
    *,
    target: int = TTS_V3_CHUNK_TARGET,
    max_len: int = TTS_V3_CHUNK_MAX,
) -> list[str]:
    """Нарезка для Eleven v3: ~1000–1500 символов, по концу предложения; [тег] не в конце чанка."""
    t = (text or "").strip()
    if not t:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", t)
    sentences = [s.strip() for s in sentences if s and s.strip()]
    if not sentences:
        return [t]

    chunks: list[str] = []
    current = ""
    idx = 0
    n = len(sentences)

    while idx < n:
        s = sentences[idx]
        if len(s) > max_len:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_split_oversized_sentence(s, max_len))
            idx += 1
            continue

        trial = f"{current} {s}".strip() if current else s
        if len(trial) <= max_len:
            current = trial
            idx += 1
            min_flush = min(target, TTS_V3_CHUNK_MIN)
            if (
                len(current) >= target
                and len(current) >= min_flush
                and not _ends_with_bracket_tag(current)
            ):
                chunks.append(current.strip())
                current = ""
            continue

        if current.strip():
            if _ends_with_bracket_tag(current):
                extended = current.strip()
                j = idx
                while j < n:
                    piece = sentences[j].strip()
                    if (
                        extended
                        and _BRACKET_TAG_START_RE.match(piece)
                        and not _ends_with_bracket_tag(extended)
                    ):
                        break
                    attempt = f"{extended} {piece}".strip() if extended else piece
                    if len(attempt) > max_len:
                        break
                    extended = attempt
                    j += 1
                chunks.append(extended)
                idx = j
                current = ""
                continue
            chunks.append(current.strip())
            current = s
            idx += 1
            continue

        current = s
        idx += 1

    if current.strip():
        chunks.append(current.strip())

    if len(chunks) >= 2 and len(chunks[-1]) < 80:
        merged = f"{chunks[-2]} {chunks[-1]}".strip()
        if len(merged) <= max_len:
            chunks = chunks[:-2] + [merged]

    fixed: list[str] = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        if i + 1 < len(chunks) and _ends_with_bracket_tag(c):
            merged = f"{c} {chunks[i + 1]}".strip()
            if len(merged) <= max_len:
                fixed.append(merged)
                i += 2
                continue
        fixed.append(c)
        i += 1
    chunks = fixed

    expected_len = len(re.sub(r"\s+", "", t))
    actual_len = sum(len(re.sub(r"\s+", "", c)) for c in chunks)
    if actual_len != expected_len:
        raise RuntimeError(
            "smart_chunk_tts_v3: потеря/дублирование текста при нарезке "
            f"({actual_len} ≠ {expected_len} символов без пробелов)."
        )
    return chunks


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


def split_tts_text_into_chunks(
    text: str,
    max_chars: int,
    *,
    model_id: str | None = None,
) -> list[str]:
    """Публичная обёртка: для eleven_v3 — ``smart_chunk_tts_v3``, иначе нарезка по точкам."""
    if normalize_tts_model_id(model_id) == TTS_MODEL_ID:
        cap = min(int(max_chars), TTS_V3_CHUNK_MAX)
        return smart_chunk_tts_v3(text, target=TTS_V3_CHUNK_TARGET, max_len=cap)

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


def _voice_title_from_name(name: str) -> str:
    n = (name or "").strip()
    if " - " in n:
        return n.split(" - ", 1)[0].strip() or n
    if " – " in n:
        return n.split(" – ", 1)[0].strip() or n
    return n or "?"


# Категории ElevenLabs, которые показываем в UI (без стандартных premade).
_CUSTOM_VOICE_CATEGORIES = frozenset({"cloned", "generated", "professional"})


def _is_custom_voice(raw: dict[str, Any]) -> bool:
    """Клон, Voice Design или голос, сохранённый из библиотеки — не premade."""
    cat = str(raw.get("category") or raw.get("voice_type") or "").strip().lower()
    if cat == "premade":
        return False
    if cat in _CUSTOM_VOICE_CATEGORIES:
        return True
    # saved / personal без category — оставляем, если явно не premade
    return bool(cat) and cat not in ("default", "community")


def _ingest_voice_row(
    merged: dict[str, dict[str, Any]],
    raw: dict[str, Any],
) -> None:
    if not isinstance(raw, dict) or not _is_custom_voice(raw):
        return
    vid = raw.get("voice_id") or raw.get("voiceId")
    name = (raw.get("name") or vid or "?").strip()
    if not vid:
        return
    category = str(raw.get("category") or raw.get("voice_type") or "").strip().lower()
    merged[str(vid)] = {
        "voice_id": str(vid),
        "name": name,
        "title": _voice_title_from_name(name),
        "category": category,
    }


def _voice_list_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("name") or "").lower()


def _fetch_v2_voices_by_type(
    headers: dict[str, str],
    voice_type: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100, "voice_type": voice_type}
        if page_token:
            params["next_page_token"] = page_token
        resp = requests.get(
            f"{ELEVEN_BASE}/v2/voices",
            headers=headers,
            params=params,
            timeout=45,
        )
        data = resp.json()
        if resp.status_code != 200:
            msg = (
                data.get("detail", {}).get("message")
                if isinstance(data.get("detail"), dict)
                else None
            )
            raise RuntimeError(
                msg or data.get("message") or resp.text or f"HTTP {resp.status_code}"
            )
        chunk = data.get("voices") or []
        if isinstance(chunk, list):
            rows.extend(v for v in chunk if isinstance(v, dict))
        if not data.get("has_more"):
            break
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return rows


def _list_voices_v1_custom_only() -> list[dict[str, Any]]:
    resp = requests.get(
        f"{ELEVEN_BASE}/v1/voices",
        headers={"xi-api-key": _api_key()},
        timeout=45,
    )
    data = resp.json()
    if resp.status_code != 200:
        msg = data.get("detail", {}).get("message") if isinstance(data.get("detail"), dict) else None
        raise RuntimeError(msg or data.get("message") or resp.text or f"HTTP {resp.status_code}")
    merged: dict[str, dict[str, Any]] = {}
    for v in data.get("voices") or []:
        _ingest_voice_row(merged, v)
    out = list(merged.values())
    out.sort(key=_voice_list_sort_key)
    return out


def list_voices() -> list[dict[str, Any]]:
    """Список пользовательских голосов для UI: клоны, дизайн, сохранённые из библиотеки.

    Стандартные premade (Adam, Bella, …) не включаются.
    """
    headers = {"xi-api-key": _api_key()}
    merged: dict[str, dict[str, Any]] = {}
    try:
        for voice_type in ("personal", "saved", "workspace"):
            for v in _fetch_v2_voices_by_type(headers, voice_type):
                _ingest_voice_row(merged, v)
    except Exception:
        return _list_voices_v1_custom_only()

    if not merged:
        return _list_voices_v1_custom_only()

    out = list(merged.values())
    out.sort(key=_voice_list_sort_key)
    return out


def pct_to_unit(pct: float | int) -> float:
    p = float(pct)
    if p < 0:
        p = 0.0
    if p > 100:
        p = 100.0
    return round(p / 100.0, 4)


SPEED_MIN = 0.25
SPEED_MAX = 4.0
# Слайдер 0–100%: 20% ≈ 1.0× (нормальная скорость ElevenLabs).
SPEED_PCT_DEFAULT = 20


def pct_to_speed(pct: float | int) -> float:
    """Слайдер 0–100% → API speed 0.25–4.0 (ElevenLabs voice_settings.speed)."""
    u = pct_to_unit(pct)
    return round(SPEED_MIN + u * (SPEED_MAX - SPEED_MIN), 4)


def speed_to_pct(speed: float | int) -> int:
    """API speed → слайдер 0–100% (для отображения/миграции)."""
    s = float(speed)
    if s <= SPEED_MIN:
        return 0
    if s >= SPEED_MAX:
        return 100
    u = (s - SPEED_MIN) / (SPEED_MAX - SPEED_MIN)
    return int(round(u * 100))


def tts_language_code_payload(language_code: str | None) -> dict[str, Any]:
    """ISO 639-1 для ElevenLabs (`language_code` в теле запроса)."""
    code = str(language_code or "").strip().lower()
    if code in ("ru", "en", "es", "ja"):
        return {"language_code": code}
    return {}


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
    language_code: str | None = None,
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
        **tts_language_code_payload(language_code),
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
    language_code: str | None = None,
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
        **tts_language_code_payload(language_code),
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
    # Защита от «коллапсированного» alignment-а (модель отдаёт alignment, но все
    # character_times равны 0): иначе words.json получится с одинаковыми
    # start_ms/end_ms у большинства слов и тайминги сцен схлопнутся в одну точку.
    if len(out) >= 4:
        zero_dur = sum(1 for w in out if (w["end_ms"] - w["start_ms"]) <= 0)
        same_start = sum(
            1 for k in range(1, len(out)) if out[k]["start_ms"] == out[k - 1]["start_ms"]
        )
        if zero_dur / len(out) >= 0.6 and same_start / max(1, len(out) - 1) >= 0.6:
            raise RuntimeError(
                "ElevenLabs with-timestamps: модель вернула битый character-alignment "
                "(у большинства слов одинаковые start/end). Перегенерируйте озвучку "
                "(eleven_v3). Попробуйте перегенерировать озвучку или используйте "
                "локальный Whisper для пословных таймингов."
            )
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
