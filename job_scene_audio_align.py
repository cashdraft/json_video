"""
Сопоставление текста сцен с пословными таймингами (ElevenLabs / Whisper).

Границы сцен: якорная тройка начала следующей сцены в окне W, валидация по числу
слов, пауза между сценами делится пополам.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# --- нормализация (п.1) -------------------------------------------------------

_DIGIT_TO_WORD: dict[str, str] = {
    "0": "ноль",
    "1": "один",
    "2": "два",
    "3": "три",
    "4": "четыре",
    "5": "пять",
    "6": "шесть",
    "7": "семь",
    "8": "восемь",
    "9": "девять",
    "10": "десять",
    "11": "одиннадцать",
    "12": "двенадцать",
    "13": "тринадцать",
    "14": "четырнадцать",
    "15": "пятнадцать",
    "16": "шестнадцать",
    "17": "семнадцать",
    "18": "восемнадцать",
    "19": "девятнадцать",
    "20": "двадцать",
    "30": "тридцать",
    "40": "сорок",
    "50": "пятьдесят",
    "60": "шестьдесят",
    "70": "семьдесят",
    "80": "восемьдесят",
    "90": "девяносто",
    "100": "сто",
}

_ANCHOR_PHRASE_LEN = 3
_ANCHOR_THRESHOLD = 0.75
_ANCHOR_THRESHOLD_WEAK = 0.60
_RATIO_MIN = 0.7
_RATIO_MAX = 1.4
_WINDOW_MULTIPLIER = 1.7
_MIN_SCENE_MS = 300


@dataclass
class WhisperToken:
    word: str
    norm: str
    start_ms: int
    end_ms: int


@dataclass
class _AlignMeta:
    flags: list[str] = field(default_factory=list)
    match_ratio: float = 1.0
    anchor_index: int | None = None


def _strip_punctuation(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum())


def _normalize_yo(s: str) -> str:
    return s.replace("ё", "е").replace("Ё", "е")


def _normalize_number_form(norm: str) -> str:
    if not norm:
        return norm
    if norm.isdigit():
        return _DIGIT_TO_WORD.get(norm, norm)
    return norm


def normalize_token(raw: str) -> str:
    """Единая нормализация токена для сравнения сцен и Whisper."""
    t = (raw or "").strip()
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = _normalize_yo(t)
    t = t.casefold()
    t = _strip_punctuation(t)
    return _normalize_number_form(t)


def tokenize_scene_text(text: str) -> list[str]:
    if not (text or "").strip():
        return []
    return [p for p in re.split(r"\s+", text.strip()) if p]


def normalize_scene_tokens(text: str) -> list[str]:
    out: list[str] = []
    for t in tokenize_scene_text(text):
        n = normalize_token(t)
        if n:
            out.append(n)
    return out


def preprocess_whisper_words(words: list[dict[str, Any]]) -> list[WhisperToken]:
    """Склейка осколков «-то», «-15» с предыдущим словом + нормализация."""
    merged_raw: list[dict[str, Any]] = []
    for w in words:
        if not isinstance(w, dict):
            continue
        word = str(w.get("word") or "")
        try:
            sm = int(w.get("start_ms") or 0)
            em = int(w.get("end_ms") or sm)
        except (TypeError, ValueError):
            continue
        if word.startswith("-") and merged_raw:
            prev = merged_raw[-1]
            prev["word"] = str(prev.get("word") or "") + word
            prev["end_ms"] = em
        else:
            merged_raw.append({"word": word, "start_ms": sm, "end_ms": em})

    out: list[WhisperToken] = []
    for w in merged_raw:
        word = str(w.get("word") or "")
        norm = normalize_token(word)
        if not norm:
            continue
        out.append(
            WhisperToken(
                word=word,
                norm=norm,
                start_ms=int(w["start_ms"]),
                end_ms=int(w["end_ms"]),
            )
        )
    return out


def _token_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _phrase_similarity(anchor: list[str], whisper_slice: list[str]) -> float:
    if not anchor or len(whisper_slice) < len(anchor):
        return 0.0
    scores = [_token_similarity(a, whisper_slice[i]) for i, a in enumerate(anchor)]
    return sum(scores) / float(len(scores))


def _anchor_phrase(scene_tokens: list[str], max_len: int = _ANCHOR_PHRASE_LEN) -> list[str]:
    if not scene_tokens:
        return []
    return scene_tokens[: min(max_len, len(scene_tokens))]


def _compute_search_window(scene_token_counts: list[int]) -> int:
    if not scene_token_counts:
        return 48
    mx = max(scene_token_counts)
    return max(12, int(mx * _WINDOW_MULTIPLIER))


@dataclass
class _AnchorHit:
    index: int
    score: float
    weak: bool = False


def _best_phrase_in_zone(
    whisper: list[WhisperToken],
    anchor: list[str],
    zone_start: int,
    zone_end: int,
    *,
    min_score: float,
    skip_before: int | None = None,
) -> _AnchorHit | None:
    if not anchor:
        return None
    plen = len(anchor)
    nw = len(whisper)
    zone_end = min(zone_end, nw)
    start = zone_start if skip_before is None else max(zone_start, skip_before)
    if start + plen > zone_end:
        return None

    best: _AnchorHit | None = None
    for pos in range(start, zone_end - plen + 1):
        slice_norms = [whisper[pos + i].norm for i in range(plen)]
        score = _phrase_similarity(anchor, slice_norms)
        if score >= min_score and (best is None or score > best.score):
            best = _AnchorHit(index=pos, score=score, weak=min_score < _ANCHOR_THRESHOLD)
    return best


def _validate_anchor_ratio(
    whisper_count: int,
    scene_count: int,
) -> tuple[bool, float]:
    if scene_count <= 0:
        return True, 1.0
    if scene_count <= 2:
        return True, whisper_count / float(scene_count)
    ratio = whisper_count / float(scene_count)
    ok = _RATIO_MIN <= ratio <= _RATIO_MAX
    return ok, ratio


def _find_anchor_for_next_scene(
    whisper: list[WhisperToken],
    *,
    cursor: int,
    window_w: int,
    scene_n_tokens: list[str],
    scene_n_count: int,
    scene_next_tokens: list[str],
    scene_next2_tokens: list[str] | None,
) -> tuple[int | None, _AlignMeta]:
    """Ищет индекс начала сцены N+1 в потоке Whisper."""
    meta = _AlignMeta()
    nw = len(whisper)
    if cursor >= nw:
        return None, meta

    zone_end = min(cursor + window_w, nw)
    anchor = _anchor_phrase(scene_next_tokens)

    def try_find(
        phrase: list[str],
        z0: int,
        z1: int,
        threshold: float,
        *,
        weak_ok: bool,
        skip_before: int | None = None,
    ) -> _AnchorHit | None:
        hit = _best_phrase_in_zone(
            whisper, phrase, z0, z1, min_score=threshold, skip_before=skip_before
        )
        if hit and weak_ok and hit.score < _ANCHOR_THRESHOLD:
            meta.flags.append("слабо привязанная")
        return hit

    # Ш.3–4 случай A / B
    hit = try_find(anchor, cursor, zone_end, _ANCHOR_THRESHOLD, weak_ok=False)
    if hit is None:
        hit = try_find(anchor, cursor, zone_end, _ANCHOR_THRESHOLD_WEAK, weak_ok=True)
    if hit is None and len(scene_next_tokens) >= 2:
        single = [scene_next_tokens[1]]
        hit = try_find(single, cursor, zone_end, _ANCHOR_THRESHOLD, weak_ok=False)
    if hit is None and scene_next2_tokens:
        anchor2 = _anchor_phrase(scene_next2_tokens)
        hit = try_find(anchor2, cursor, min(cursor + window_w * 2, nw), _ANCHOR_THRESHOLD, weak_ok=True)
        if hit is not None:
            meta.flags.append("якорь со сцены N+2")
            span_end = hit.index
            span_start = cursor
            span_len = max(1, span_end - span_start)
            n_words = max(1, scene_n_count)
            n1_words = max(1, len(scene_next_tokens))
            split = span_start + int(span_len * (n_words / float(n_words + n1_words)))
            return max(cursor + 1, min(split, span_end)), meta

    if hit is None:
        return None, meta

    anchor_idx = hit.index
    meta.anchor_index = anchor_idx

    # Ш.5 валидация по числу слов
    if scene_n_count > 2:
        whisper_count = anchor_idx - cursor
        ok, ratio = _validate_anchor_ratio(whisper_count, scene_n_count)
        meta.match_ratio = ratio

        if not ok and ratio < _RATIO_MIN:
            hit2 = try_find(
                anchor,
                cursor,
                zone_end,
                _ANCHOR_THRESHOLD,
                weak_ok=False,
                skip_before=anchor_idx + 1,
            )
            if hit2 is not None:
                anchor_idx = hit2.index
                meta.anchor_index = anchor_idx
                whisper_count = anchor_idx - cursor
                _, ratio = _validate_anchor_ratio(whisper_count, scene_n_count)
                meta.match_ratio = ratio
            else:
                meta.flags.append("подозрительная по длине")

        elif not ok and ratio > _RATIO_MAX:
            narrow_end = min(zone_end, cursor + max(3, int(scene_n_count * _RATIO_MAX)))
            hit3 = try_find(anchor, cursor, narrow_end, _ANCHOR_THRESHOLD, weak_ok=False)
            if hit3 is not None:
                anchor_idx = hit3.index
                meta.anchor_index = anchor_idx
                whisper_count = anchor_idx - cursor
                _, ratio = _validate_anchor_ratio(whisper_count, scene_n_count)
                meta.match_ratio = ratio
            else:
                meta.flags.append("подозрительная по длине")

        _, ratio_final = _validate_anchor_ratio(anchor_idx - cursor, scene_n_count)
        if not (_RATIO_MIN <= ratio_final <= _RATIO_MAX):
            if "подозрительная по длине" not in meta.flags:
                meta.flags.append("подозрительная по длине")

    if hit.weak and "слабо привязанная" not in meta.flags:
        meta.flags.append("слабо привязанная")

    return anchor_idx, meta


def _boundary_ms(
    whisper: list[WhisperToken],
    last_idx: int,
    first_idx: int,
) -> int:
    """Ш.6: середина паузы между словами."""
    if last_idx < 0 or first_idx >= len(whisper):
        return whisper[min(first_idx, len(whisper) - 1)].start_ms if whisper else 0
    last_end = whisper[last_idx].end_ms
    first_start = whisper[first_idx].start_ms
    pause = first_start - last_end
    if pause > 0:
        return last_end + pause // 2
    return last_end


def _proportional_boundaries(
    whisper: list[WhisperToken],
    *,
    cursor: int,
    end_ms: int,
    scene_counts: list[int],
    start_scene_idx: int,
) -> list[int]:
    """Делит [cursor..end] между сценами по числу слов (фолбэк Б.4)."""
    counts = [max(1, c) for c in scene_counts]
    total_words = sum(counts)
    if cursor >= len(whisper):
        return [end_ms] * (len(scene_counts) + 1)

    t0 = whisper[cursor].start_ms
    t1 = end_ms
    span = max(1, t1 - t0)
    boundaries = [t0]
    acc = 0
    for i, cnt in enumerate(counts):
        acc += cnt
        if i == len(counts) - 1:
            boundaries.append(t1)
        else:
            boundaries.append(t0 + int(span * (acc / float(total_words))))
    return boundaries


def _fmt_ts(ms: int) -> str:
    ms = max(0, int(ms))
    m, r = divmod(ms, 60000)
    sec = r // 1000
    centi = (r % 1000) // 10
    return f"{m}:{sec:02d}.{centi:02d}"


def format_audio_timing_badge(start_ms: int, end_ms: int) -> str:
    dur_ms = max(0, int(end_ms) - int(start_ms))
    dur_s = dur_ms / 1000.0
    return f"{_fmt_ts(start_ms)} → {_fmt_ts(end_ms)} · {dur_s:.2f} с"


def format_ms_clock(ms: int) -> str:
    return _fmt_ts(int(ms))


def format_duration_seconds(ms: int) -> str:
    return f"{max(0, int(ms)) / 1000.0:.2f}"


def _sanity_fix_boundaries(bounds: list[int], total_ms: int) -> list[int]:
    """Финальный санити-чек: строго возрастают, минимум длительности."""
    if not bounds:
        return [0, total_ms]
    out = [int(bounds[0])]
    for i in range(1, len(bounds)):
        b = int(bounds[i])
        prev = out[-1]
        if b <= prev:
            b = prev + _MIN_SCENE_MS
        out.append(min(b, total_ms))
    out[-1] = total_ms
    for i in range(len(out) - 1):
        if out[i + 1] - out[i] < _MIN_SCENE_MS:
            out[i + 1] = min(total_ms, out[i] + _MIN_SCENE_MS)
    out[-1] = total_ms
    return out


def align_scenes_to_word_timings(
    scenes: list[dict[str, Any]],
    words: list[dict[str, Any]],
    *,
    total_duration_ms: int,
    match_window: int | None = None,
    low_confidence_ratio: float = 0.85,
) -> list[dict[str, Any]]:
    """
    Выравнивание сцен по пословным таймингам (якорные тройки + валидация по длине).

    ``match_window`` — если задан, переопределяет авто-окно W (для совместимости API).
    """
    n = len(scenes)
    if n == 0:
        return []

    whisper = preprocess_whisper_words(words)
    nw = len(whisper)
    total_ms = max(0, int(total_duration_ms))
    if total_ms <= 0 and whisper:
        total_ms = whisper[-1].end_ms

    scene_tokens: list[list[str]] = [
        normalize_scene_tokens(str((sc or {}).get("text") or "")) for sc in scenes
    ]
    scene_counts = [len(t) for t in scene_tokens]

    if match_window is not None and int(match_window) > 0:
        window_w = int(match_window)
    else:
        window_w = _compute_search_window(scene_counts)

    # Границы: n+1 точек (начало сцены 0 .. конец сцены n-1)
    bounds: list[int | None] = [0] + [None] * n
    metas: list[_AlignMeta] = [_AlignMeta() for _ in range(n)]
    cursor = 0

    i = 0
    while i < n - 1:
        if cursor >= nw:
            remaining = list(range(i, n))
            sub_counts = [max(1, scene_counts[j]) for j in remaining]
            est = _proportional_boundaries(
                whisper,
                cursor=cursor,
                end_ms=total_ms,
                scene_counts=sub_counts,
                start_scene_idx=i,
            )
            for k, b in enumerate(est[1:], start=i):
                bounds[k + 1] = b
                metas[k].flags.append("оценочная")
                metas[k].match_ratio = 0.0
            break

        next_tokens = scene_tokens[i + 1]
        next2 = scene_tokens[i + 2] if i + 2 < n else None

        anchor_idx, meta = _find_anchor_for_next_scene(
            whisper,
            cursor=cursor,
            window_w=window_w,
            scene_n_tokens=scene_tokens[i],
            scene_n_count=scene_counts[i],
            scene_next_tokens=next_tokens,
            scene_next2_tokens=next2,
        )
        metas[i] = meta

        if anchor_idx is None:
            # Б.4: пропорция для текущей и следующих до следующего якоря
            run_counts = [max(1, scene_counts[i])]
            j = i + 1
            while j < n - 1:
                probe, _ = _find_anchor_for_next_scene(
                    whisper,
                    cursor=cursor,
                    window_w=window_w * 2,
                    scene_n_tokens=scene_tokens[j],
                    scene_n_count=scene_counts[j],
                    scene_next_tokens=scene_tokens[j + 1],
                    scene_next2_tokens=scene_tokens[j + 2] if j + 2 < n else None,
                )
                if probe is not None:
                    break
                run_counts.append(max(1, scene_counts[j]))
                j += 1
            end_ms = total_ms
            if j < n - 1 and probe is not None:
                end_ms = whisper[probe].start_ms
            est = _proportional_boundaries(
                whisper,
                cursor=cursor,
                end_ms=end_ms,
                scene_counts=run_counts,
                start_scene_idx=i,
            )
            for k, b in enumerate(est[1:]):
                idx = i + k
                bounds[idx + 1] = b
                metas[idx].flags.append("оценочная")
                metas[idx].match_ratio = 0.0
            cursor = max(cursor, 1)
            if j < n - 1 and probe is not None:
                i = j
                continue
            i = j
            continue

        last_idx = anchor_idx - 1
        boundary = _boundary_ms(whisper, last_idx, anchor_idx)
        bounds[i + 1] = boundary
        cursor = anchor_idx
        i += 1

    if bounds[-1] is None:
        bounds[-1] = total_ms

    # Заполнить пропуски
    for k in range(1, n):
        if bounds[k] is None:
            bounds[k] = bounds[k - 1] if bounds[k - 1] is not None else 0

    int_bounds = _sanity_fix_boundaries([int(b or 0) for b in bounds], total_ms)

    raw_s: list[int] = []
    raw_e: list[int] = []
    for i in range(n):
        sm = int_bounds[i]
        em = int_bounds[i + 1]
        raw_s.append(sm)
        raw_e.append(em)

    out: list[dict[str, Any]] = []
    for i in range(n):
        sm = int_bounds[i]
        em = int_bounds[i + 1]
        if em < sm:
            em = sm
        dm = em - sm
        m = metas[i]
        low = (
            "оценочная" in m.flags
            or "слабо привязанная" in m.flags
            or "подозрительная по длине" in m.flags
            or m.match_ratio + 1e-9 < low_confidence_ratio
        )
        out.append(
            {
                "start_ms": sm,
                "end_ms": em,
                "duration_ms": dm,
                "raw_start_ms": raw_s[i],
                "raw_end_ms": raw_e[i],
                "match_ratio": round(float(m.match_ratio), 4),
                "low_confidence": bool(low),
                "align_flags": list(m.flags),
                "badge": format_audio_timing_badge(sm, em),
                "start_time": format_ms_clock(sm),
                "start_end": format_ms_clock(em),
                "duration_s": format_duration_seconds(dm),
            }
        )
    return out


def merge_audio_timing_into_scenes(
    scenes: list[dict[str, Any]],
    timings: list[dict[str, Any]],
    *,
    audio_filename: str,
) -> None:
    """Пишет `audio_timing` в каждый dict сцены (на месте)."""
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        if i >= len(timings):
            continue
        t = dict(timings[i])
        t["audio_filename"] = audio_filename
        sc["audio_timing"] = t
