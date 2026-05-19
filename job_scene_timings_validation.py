"""Проверка результата сопоставления сцен с пословными таймингами озвучки."""

from __future__ import annotations

from statistics import median
from typing import Any

from job_scene_audio_align import format_ms_clock, tokenize_scene_text

# Пороги (потом можно ужать набор проверок)
LOW_MATCH_RATIO = 0.85
GAP_WARN_MS = 3_000
GAP_FAIL_MS = 10_000
HEAD_WARN_MS = 2_000
HEAD_FAIL_MS = 5_000
TAIL_WARN_MS = 3_000
TAIL_FAIL_MS = 15_000
SHORT_SCENE_WARN_MS = 200
LONG_SCENE_WARN_MS = 120_000
TIMELINE_LONG_SCENE_MS = 20_000
TIMELINE_PAUSE_MS = 5_000
OVERLAP_FAIL_MS = 1
DURATION_SUM_WARN_RATIO = 0.55  # сумма сцен / охват озвучки
UNUSED_WORDS_WARN_RATIO = 0.08
LOW_CONFIDENCE_WARN_RATIO = 0.25


def _fmt_ms(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    try:
        return format_ms_clock(int(ms))
    except (TypeError, ValueError):
        return "—"


def _timings_rows_from_scenes(scenes: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        at = sc.get("audio_timing") if isinstance(sc.get("audio_timing"), dict) else {}
        row = dict(at)
        row["scene_id"] = sc.get("scene_id")
        row["scene_index"] = i
        rows.append(row)
    return rows


def _scene_token_count(sc: dict) -> int:
    return len(tokenize_scene_text(str(sc.get("text") or "")))


def _words_list(doc: dict | None) -> list[dict]:
    if not doc or not isinstance(doc, dict):
        return []
    words = doc.get("words")
    return words if isinstance(words, list) else []


def _total_duration_ms(doc: dict | None, words: list[dict]) -> int:
    if doc and isinstance(doc.get("total_duration_ms"), (int, float)):
        try:
            v = int(doc["total_duration_ms"])
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    if words:
        try:
            return int((words[-1] or {}).get("end_ms") or 0)
        except (TypeError, ValueError, IndexError):
            pass
    return 0


def _count_words_outside_intervals(words: list[dict], intervals: list[tuple[int, int]]) -> int:
    if not words or not intervals:
        return len(words) if words else 0
    n = 0
    for w in words:
        if not isinstance(w, dict):
            continue
        try:
            ws = int(w.get("start_ms") or 0)
            we = int(w.get("end_ms") or ws)
        except (TypeError, ValueError):
            continue
        mid = (ws + we) // 2
        inside = False
        for a, b in intervals:
            if a <= mid <= b:
                inside = True
                break
        if not inside:
            n += 1
    return n


def validate_scene_timings(
    scenes: list[dict],
    *,
    timings_rows: list[dict[str, Any]] | None = None,
    words_doc: dict | None = None,
    source: str = "elevenlabs",
    audio_filename: str = "",
    words_source_ok: bool | None = None,
) -> dict[str, Any]:
    """
    Возвращает структуру для UI «Проверка» в блоке Тайминги Scenes:
    ok, status, reasons, warns, checks[{label, ok, detail, failItems, severity}], stats, summary.
    """
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    warns: list[str] = []

    def add_check(
        label: str,
        ok: bool,
        detail: str = "",
        *,
        fail_items: list[str] | None = None,
        severity: str = "fail",
    ) -> None:
        sev = severity if severity in ("fail", "warn", "info") else ("info" if ok else "fail")
        row = {
            "label": label,
            "ok": bool(ok),
            "detail": "" if ok and sev == "info" else str(detail or "").strip(),
            "failItems": [] if ok else (fail_items or []),
            "severity": sev,
        }
        if ok and sev == "warn" and detail:
            row["detail"] = str(detail).strip()
        checks.append(row)
        if not ok and row["detail"]:
            reasons.append(f"{label}: {row['detail']}")
        elif sev == "warn" and row["detail"]:
            warns.append(f"{label}: {row['detail']}")

    if not scenes:
        add_check("Сцены в проекте", False, "нет сцен")
        for label in (
            "Покрытие таймингами",
            "Дубликаты scene_id",
            "Порядок на таймлайне",
            "Перекрытия сцен",
            "Сопоставление слов (match_ratio)",
        ):
            add_check(label, False, "—")
        return {
            "ok": False,
            "status": "no",
            "reasons": reasons,
            "warns": warns,
            "checks": checks,
            "stats": {},
            "summary": "Нет сцен для проверки.",
            "timeline": _build_scene_timeline([], []),
        }

    rows = timings_rows if timings_rows is not None else _timings_rows_from_scenes(scenes)
    n = len(scenes)
    words = _words_list(words_doc)
    total_ms = _total_duration_ms(words_doc, words)
    src_label = "Whisper" if str(source).lower() == "whisper" else "ElevenLabs"

    # --- scene_id / текст ---
    ids: list[str] = []
    dup_ids: list[str] = []
    seen: set[str] = set()
    empty_text_with_timing: list[str] = []
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("scene_id") or f"#{i + 1}")
        ids.append(sid)
        if sid in seen:
            dup_ids.append(sid)
        seen.add(sid)
        tok_n = _scene_token_count(sc)
        at = sc.get("audio_timing") if isinstance(sc.get("audio_timing"), dict) else {}
        has_timing = at.get("start_ms") is not None
        if has_timing and tok_n == 0:
            empty_text_with_timing.append(sid)

    add_check(
        "Дубликаты scene_id",
        len(dup_ids) == 0,
        f"{len(set(dup_ids))} повторов" if dup_ids else "",
        fail_items=dup_ids[:40],
    )

    add_check(
        "Пустой текст с таймингом",
        len(empty_text_with_timing) == 0,
        f"{len(empty_text_with_timing)} сцен" if empty_text_with_timing else "",
        fail_items=empty_text_with_timing[:40],
        severity="warn",
    )

    # --- покрытие ---
    with_timing: list[str] = []
    without_timing: list[str] = []
    for i, sc in enumerate(scenes):
        sid = ids[i] if i < len(ids) else f"#{i + 1}"
        at = sc.get("audio_timing") if isinstance(sc, dict) and isinstance(sc.get("audio_timing"), dict) else {}
        if at.get("start_ms") is not None and at.get("end_ms") is not None:
            with_timing.append(sid)
        else:
            without_timing.append(sid)

    coverage_ok = len(without_timing) == 0 and len(with_timing) == n
    add_check(
        "Покрытие таймингами",
        coverage_ok,
        f"{len(with_timing)}/{n} сцен" if not coverage_ok else f"{n}/{n}",
        fail_items=without_timing[:48],
    )

    # --- match_ratio / low_confidence ---
    low_conf: list[str] = []
    zero_match: list[str] = []
    ratios: list[float] = []
    for i, sc in enumerate(scenes):
        sid = ids[i] if i < len(ids) else f"#{i + 1}"
        at = sc.get("audio_timing") if isinstance(sc, dict) and isinstance(sc.get("audio_timing"), dict) else {}
        r = at.get("match_ratio")
        if r is None and i < len(rows):
            r = rows[i].get("match_ratio")
        try:
            rv = float(r) if r is not None else 0.0
        except (TypeError, ValueError):
            rv = 0.0
        ratios.append(rv)
        lc = at.get("low_confidence")
        if lc is None and i < len(rows):
            lc = rows[i].get("low_confidence")
        if lc or rv + 1e-9 < LOW_MATCH_RATIO:
            low_conf.append(f"{sid} ({rv:.0%})")
        if rv <= 0 and _scene_token_count(sc) > 0:
            zero_match.append(sid)

    add_check(
        "Сопоставление слов (match_ratio)",
        len(zero_match) == 0,
        f"{len(zero_match)} сцен без совпадений" if zero_match else "",
        fail_items=zero_match[:48],
    )

    low_conf_ratio = len(low_conf) / float(n) if n else 0.0
    add_check(
        "Низкая уверенность (low_confidence)",
        low_conf_ratio < LOW_CONFIDENCE_WARN_RATIO,
        f"{len(low_conf)}/{n} сцен ({low_conf_ratio:.0%})",
        fail_items=low_conf[:32],
        severity="warn" if low_conf else "info",
    )

    # --- интервалы ---
    intervals: list[tuple[int, int, str]] = []
    inv_duration: list[str] = []
    zero_dur: list[str] = []
    short_dur: list[str] = []
    long_dur: list[str] = []
    durations_ms: list[int] = []

    for i, sc in enumerate(scenes):
        sid = ids[i] if i < len(ids) else f"#{i + 1}"
        at = sc.get("audio_timing") if isinstance(sc, dict) and isinstance(sc.get("audio_timing"), dict) else {}
        try:
            sm = int(at.get("start_ms"))
            em = int(at.get("end_ms"))
        except (TypeError, ValueError):
            continue
        dm = em - sm
        durations_ms.append(dm)
        intervals.append((sm, em, sid))
        if em < sm:
            inv_duration.append(sid)
        if dm <= 0:
            zero_dur.append(sid)
        elif dm < SHORT_SCENE_WARN_MS:
            short_dur.append(f"{sid} ({dm} мс)")
        elif dm > LONG_SCENE_WARN_MS:
            long_dur.append(f"{sid} ({_fmt_ms(dm)})")

    add_check("start_ms ≤ end_ms", len(inv_duration) == 0, f"{len(inv_duration)} сцен", fail_items=inv_duration[:32])
    add_check("Нулевая длительность сцены", len(zero_dur) == 0, f"{len(zero_dur)} сцен", fail_items=zero_dur[:32])
    add_check(
        "Очень короткие сцены (<0.2 с)",
        len(short_dur) == 0,
        f"{len(short_dur)} сцен" if short_dur else "",
        fail_items=short_dur[:24],
        severity="warn",
    )
    add_check(
        "Очень длинные сцены (>120 с)",
        len(long_dur) == 0,
        f"{len(long_dur)} сцен" if long_dur else "",
        fail_items=long_dur[:24],
        severity="warn",
    )

    overlaps: list[str] = []
    order_gaps: list[str] = []
    big_gaps: list[str] = []
    if len(intervals) >= 2:
        intervals.sort(key=lambda x: x[0])
        for j in range(len(intervals) - 1):
            sm0, em0, sid0 = intervals[j]
            sm1, em1, sid1 = intervals[j + 1]
            if em0 > sm1 + OVERLAP_FAIL_MS:
                overlaps.append(f"{sid0}→{sid1}: {_fmt_ms(sm1)}–{_fmt_ms(em0)}")
            gap = sm1 - em0
            if gap > GAP_FAIL_MS:
                big_gaps.append(f"{sid0}→{sid1}: {_fmt_ms(gap)}")
            elif gap > GAP_WARN_MS:
                order_gaps.append(f"{sid0}→{sid1}: {_fmt_ms(gap)}")

    add_check("Перекрытия сцен", len(overlaps) == 0, f"{len(overlaps)} пар", fail_items=overlaps[:32])
    add_check(
        "Паузы между сценами (>10 с)",
        len(big_gaps) == 0,
        f"{len(big_gaps)} разрывов" if big_gaps else "",
        fail_items=big_gaps[:24],
    )
    add_check(
        "Паузы между сценами (3–10 с)",
        len(order_gaps) == 0,
        f"{len(order_gaps)} пауз" if order_gaps else "",
        fail_items=order_gaps[:24],
        severity="warn",
    )

    first_start = intervals[0][0] if intervals else None
    last_end = intervals[-1][1] if intervals else None
    if first_start is not None:
        if first_start > HEAD_FAIL_MS:
            add_check("Начало первой сцены", False, f"@ {_fmt_ms(first_start)}")
        elif first_start > HEAD_WARN_MS:
            add_check("Начало первой сцены", True, f"@ {_fmt_ms(first_start)} (далеко от 0)", severity="warn")
        else:
            add_check("Начало первой сцены", True, f"@ {_fmt_ms(first_start)}", severity="info")

    if last_end is not None and total_ms > 0:
        tail = total_ms - last_end
        if tail > TAIL_FAIL_MS:
            add_check("Хвост после последней сцены", False, f"{_fmt_ms(tail)} без сцен")
        elif tail > TAIL_WARN_MS:
            add_check("Хвост после последней сцены", True, f"{_fmt_ms(tail)}", severity="warn")
        else:
            add_check("Хвост после последней сцены", True, f"{_fmt_ms(tail)}", severity="info")
    elif last_end is not None:
        add_check("Хвост после последней сцены", True, "нет total_duration_ms", severity="info")

    # --- words vs scenes ---
    interval_pairs = [(a, b) for a, b, _ in intervals]
    unused_words = _count_words_outside_intervals(words, interval_pairs) if words else 0
    clean_word_n = len(words)
    if clean_word_n > 0:
        unused_ratio = unused_words / float(clean_word_n)
        add_check(
            "Слова вне интервалов сцен",
            unused_ratio < UNUSED_WORDS_WARN_RATIO,
            f"{unused_words}/{clean_word_n} слов ({unused_ratio:.1%})",
            fail_items=[],
            severity="warn" if unused_words else "info",
        )
    else:
        add_check("Слова вне интервалов сцен", True, "нет words.json", severity="info")

    sum_dur = sum(durations_ms) if durations_ms else 0
    span = (last_end - first_start) if first_start is not None and last_end is not None else 0
    if span > 0:
        ratio = sum_dur / float(span)
        add_check(
            "Сумма длительностей / охват",
            ratio >= DURATION_SUM_WARN_RATIO,
            f"{_fmt_ms(sum_dur)} / {_fmt_ms(span)} ({ratio:.0%})",
            severity="warn" if ratio < DURATION_SUM_WARN_RATIO else "info",
        )
    else:
        add_check("Сумма длительностей / охват", True, "—", severity="info")

    token_counts = [_scene_token_count(sc) for sc in scenes if isinstance(sc, dict)]
    total_tokens = sum(token_counts)
    avg_tokens = total_tokens / float(n) if n else 0.0
    avg_ratio = sum(ratios) / float(len(ratios)) if ratios else 0.0
    avg_dur_s = (sum(dur / 1000.0 for dur in durations_ms) / len(durations_ms)) if durations_ms else 0.0
    med_dur_s = (median(durations_ms) / 1000.0) if durations_ms else 0.0
    min_dur_s = (min(durations_ms) / 1000.0) if durations_ms else 0.0
    max_dur_s = (max(durations_ms) / 1000.0) if durations_ms else 0.0

    add_check("Источник таймингов", True, src_label, severity="info")
    add_check(
        "Озвучка (audio)",
        True,
        audio_filename or "—",
        severity="info",
    )
    if words_source_ok is not None:
        add_check(
            f"Проверка words ({src_label})",
            bool(words_source_ok),
            "OK" if words_source_ok else "NO — источник слов не прошёл проверку",
            severity="warn" if not words_source_ok else "info",
        )

    add_check("Всего сцен", True, str(n), severity="info")
    add_check("Средняя длительность сцены", True, f"{avg_dur_s:.2f} с", severity="info")
    add_check("Медиана длительности", True, f"{med_dur_s:.2f} с", severity="info")
    add_check("Min / max длительность", True, f"{min_dur_s:.2f} / {max_dur_s:.2f} с", severity="info")
    add_check("Средний match_ratio", True, f"{avg_ratio:.1%}", severity="info")
    add_check("Среднее токенов на сцену", True, f"{avg_tokens:.1f}", severity="info")
    add_check("Всего токенов в сценах", True, str(total_tokens), severity="info")
    if clean_word_n:
        add_check("Слов в words.json", True, str(clean_word_n), severity="info")
    if total_ms > 0:
        add_check("Длительность озвучки", True, _fmt_ms(total_ms), severity="info")

    fail_checks = [c for c in checks if c.get("severity") == "fail" and not c["ok"]]
    ok = len(fail_checks) == 0 and coverage_ok

    stats = {
        "scenes_total": n,
        "scenes_with_timing": len(with_timing),
        "low_confidence_count": len(low_conf),
        "avg_duration_s": round(avg_dur_s, 3),
        "median_duration_s": round(med_dur_s, 3),
        "avg_match_ratio": round(avg_ratio, 4),
        "avg_tokens_per_scene": round(avg_tokens, 2),
        "unused_words": unused_words,
        "source": source,
        "audio_filename": audio_filename,
    }

    summary_parts = [
        f"{len(with_timing)}/{n} сцен",
        f"ср. {avg_dur_s:.2f} с" if durations_ms else "ср. —",
        f"match {_fmt_pct(avg_ratio)}",
        src_label,
    ]
    summary = " · ".join(summary_parts)

    timeline = _build_scene_timeline(scenes, rows if isinstance(rows, list) else [])

    return {
        "ok": ok,
        "status": "ok" if ok else "no",
        "reasons": reasons,
        "warns": warns,
        "checks": checks,
        "stats": stats,
        "summary": summary,
        "timeline": timeline,
    }


def _fmt_pct(x: float) -> str:
    try:
        return f"{float(x):.0%}"
    except (TypeError, ValueError):
        return "—"


def _fmt_duration_s(ms: int | float | None) -> str:
    try:
        return f"{max(0, int(ms)) / 1000.0:.2f}"
    except (TypeError, ValueError):
        return "—"


def _build_scene_timeline(
    scenes: list[dict],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Список сцен и пауз для UI «Сцены по порядку»."""
    items: list[dict[str, Any]] = []
    pause_count = 0
    overlap_count = 0
    long_count = 0
    prev_end: int | None = None

    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("scene_id") or f"scene_{i + 1:03d}")
        at = sc.get("audio_timing") if isinstance(sc.get("audio_timing"), dict) else {}
        row = rows[i] if i < len(rows) else {}

        try:
            start_ms = int(
                at.get("start_ms")
                if at.get("start_ms") is not None
                else row.get("start_ms") or 0
            )
            end_ms = int(
                at.get("end_ms") if at.get("end_ms") is not None else row.get("end_ms") or 0
            )
        except (TypeError, ValueError):
            start_ms = 0
            end_ms = 0

        has_timing = end_ms > start_ms or at.get("start_ms") is not None

        if prev_end is not None and has_timing and start_ms > prev_end:
            gap_ms = start_ms - prev_end
            if gap_ms >= TIMELINE_PAUSE_MS:
                pause_count += 1
                items.append(
                    {
                        "type": "pause",
                        "gap_s": _fmt_duration_s(gap_ms),
                        "gap_clock": f"{_fmt_ms(prev_end)} → {_fmt_ms(start_ms)}",
                    }
                )

        flags: list[str] = []
        if not has_timing:
            flags.append("no_timing")
        dur_ms = max(0, end_ms - start_ms) if has_timing else 0
        if has_timing and prev_end is not None and start_ms < prev_end - OVERLAP_FAIL_MS:
            flags.append("overlap")
            overlap_count += 1
        if has_timing and dur_ms > TIMELINE_LONG_SCENE_MS:
            flags.append("long")
            long_count += 1

        items.append(
            {
                "type": "scene",
                "scene_id": sid,
                "start": _fmt_ms(start_ms) if has_timing else "—",
                "end": _fmt_ms(end_ms) if has_timing else "—",
                "duration_s": _fmt_duration_s(dur_ms) if has_timing else "—",
                "flags": flags,
            }
        )

        if has_timing:
            prev_end = end_ms

    scene_count = sum(1 for it in items if it.get("type") == "scene")
    return {
        "items": items,
        "scene_count": scene_count,
        "pause_count": pause_count,
        "overlap_count": overlap_count,
        "long_count": long_count,
    }
