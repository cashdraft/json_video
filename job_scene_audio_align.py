"""
Сопоставление текста сцен с массивом слов ElevenLabs (start_ms/end_ms).

После жадного выравнивания границы между сценами режутся по середине паузы
между сырым концом последнего слова сцены i и сырым началом первого слова сцены i+1.
Хвост после последнего слова до total_duration_ms делится пополам с концом файла.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalize_token(raw: str) -> str:
    """Сравнение токена с таймингом: без ведущих/хвостовых пунктуации и пробелов."""
    t = (raw or "").strip()
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = t.strip(" \t\r\n\"'«»„“”`()[]{}.,:;!?…—–-")
    return t.casefold()


def tokenize_scene_text(text: str) -> list[str]:
    if not (text or "").strip():
        return []
    return [p for p in re.split(r"\s+", text.strip()) if p]


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


def align_scenes_to_word_timings(
    scenes: list[dict[str, Any]],
    words: list[dict[str, Any]],
    *,
    total_duration_ms: int,
    match_window: int = 18,
    low_confidence_ratio: float = 0.85,
) -> list[dict[str, Any]]:
    """
    Возвращает по одному dict на сцену (порядок как в `scenes`) с полями:
    start_ms, end_ms, duration_ms, raw_start_ms, raw_end_ms, match_ratio,
    low_confidence, badge.
    """
    n = len(scenes)
    if n == 0:
        return []

    w0 = int(words[0]["start_ms"]) if words else 0
    total_ms = max(0, int(total_duration_ms))

    raw_s: list[int | None] = [None] * n
    raw_e: list[int | None] = [None] * n
    ratios: list[float] = [0.0] * n
    matched_flags: list[bool] = [False] * n

    j = 0
    nw = len(words)

    for i, sc in enumerate(scenes):
        toks = tokenize_scene_text(str((sc or {}).get("text") or ""))
        norm_toks = [normalize_token(t) for t in toks]
        norm_toks = [t for t in norm_toks if t]
        expected = len(norm_toks)
        if expected == 0:
            ratios[i] = 1.0
            matched_flags[i] = True
            continue

        matched_idx: list[int] = []
        for nt in norm_toks:
            hi = min(j + max(1, int(match_window)), nw)
            found: int | None = None
            for k in range(j, hi):
                wtxt = str((words[k] or {}).get("word") or "")
                if normalize_token(wtxt) == nt:
                    found = k
                    break
            if found is not None:
                matched_idx.append(found)
                j = found + 1

        hit = len(matched_idx)
        ratios[i] = hit / float(expected) if expected else 1.0
        if hit:
            lo, hiw = matched_idx[0], matched_idx[-1]
            raw_s[i] = int(words[lo]["start_ms"])
            raw_e[i] = int(words[hiw]["end_ms"])
            matched_flags[i] = True
        else:
            matched_flags[i] = False

    # Заполнить сырые границы для сцен без совпадений (между соседями по времени).
    def fill_raw_edges() -> tuple[list[int], list[int]]:
        s2: list[int | None] = list(raw_s)
        e2: list[int | None] = list(raw_e)
        for _ in range(n + 2):
            changed = False
            for i in range(n):
                if s2[i] is not None and e2[i] is not None:
                    continue
                left = e2[i - 1] if i > 0 else None
                right = s2[i + 1] if i + 1 < n else None
                if s2[i] is None and e2[i] is None:
                    le = left if left is not None else w0
                    rs = right if right is not None else total_ms
                    mid = (le + rs) // 2
                    s2[i] = e2[i] = mid
                    changed = True
                elif s2[i] is None:
                    s2[i] = left if left is not None else w0
                    changed = True
                elif e2[i] is None:
                    e2[i] = right if right is not None else total_ms
                    changed = True
            if not changed:
                break
        out_s: list[int] = []
        out_e: list[int] = []
        for i in range(n):
            si = int(s2[i] if s2[i] is not None else w0)
            ei = int(e2[i] if e2[i] is not None else total_ms)
            if ei < si:
                ei = si
            out_s.append(si)
            out_e.append(ei)
        return out_s, out_e

    fs, fe = fill_raw_edges()

    # Скорректированные границы с разрезом пауз пополам.
    adj_s = [0] * n
    adj_e = [0] * n
    if n == 1:
        adj_s[0] = fs[0]
        adj_e[0] = (fe[0] + total_ms) // 2
    else:
        boundaries: list[int] = []
        for i in range(n - 1):
            b = (fe[i] + fs[i + 1]) // 2
            if b < fe[i]:
                b = fe[i]
            if b > fs[i + 1]:
                b = fs[i + 1]
            boundaries.append(b)
        adj_s[0] = fs[0]
        adj_e[0] = boundaries[0]
        for i in range(1, n - 1):
            adj_s[i] = boundaries[i - 1]
            adj_e[i] = boundaries[i]
        adj_s[n - 1] = boundaries[n - 2]
        adj_e[n - 1] = (fe[n - 1] + total_ms) // 2

    out: list[dict[str, Any]] = []
    for i in range(n):
        low = (not matched_flags[i]) or (ratios[i] + 1e-9 < low_confidence_ratio)
        sm = adj_s[i]
        em = adj_e[i]
        if em < sm:
            em = sm
            low = True
        dm = em - sm
        badge = format_audio_timing_badge(sm, em)
        rs = raw_s[i]
        re_ = raw_e[i]
        out.append(
            {
                "start_ms": sm,
                "end_ms": em,
                "duration_ms": dm,
                "raw_start_ms": rs,
                "raw_end_ms": re_,
                "match_ratio": round(float(ratios[i]), 4),
                "low_confidence": bool(low),
                "badge": badge,
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
