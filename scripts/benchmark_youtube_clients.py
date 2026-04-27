#!/usr/bin/env python3
"""
Time yt-dlp per YouTube player_client (тот же URL, минимальный по filesize format).

  YBENCH_URL=... /srv/json_video/.venv/bin/python3 scripts/benchmark_youtube_clients.py

Если client не отдаёт formats (часто web без EJS) — в таблице «skip». Итог — цепочка
только из успешных, по убыванию MiB/s (быстрее = раньше в YOUTUBE_PLAYER_CLIENT_FALLBACK).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

URL = (os.getenv("YBENCH_URL") or "https://www.youtube.com/watch?v=X-GZ57mRwUo").strip()
CLIENTS = [x.strip().lower() for x in (os.getenv("YBENCH_CLIENTS") or "android,web,ios,mweb").split(",") if x.strip()]
VENV = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "yt-dlp"
SOCKET = os.getenv("YBENCH_SOCKET", "60")


def _json_from_ytdlp_out(s: str) -> dict | None:
    s = (s or "").strip()
    if "{" in s:
        s = s[s.find("{") :]
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def extract_json(url: str, client: str) -> dict | None:
    r = subprocess.run(
        [str(VENV), "-J", "--no-warnings", "--skip-download", "--extractor-args", f"youtube:player_client={client}", url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    d = _json_from_ytdlp_out(r.stdout or "")
    if d is not None:
        return d
    return _json_from_ytdlp_out((r.stderr or ""))


def pick_smallest_format_id(data: dict) -> str | None:
    """Один сегмент с min filesize: не mhtml, не сабы."""
    fs0 = [f for f in (data.get("formats") or []) if isinstance(f, dict) and f.get("format_id") is not None]
    fs = [f for f in fs0 if f.get("ext") != "mhtml" and (f.get("vcodec") != "none" or f.get("acodec") != "none")]

    def approx_bytes(f: dict) -> int:
        for k in ("filesize", "filesize_approx"):
            v = f.get(k)
            if isinstance(v, (int, float)) and v and v > 0:
                return int(v)
        t = f.get("tbr") or f.get("vbr") or 0
        d = f.get("duration")
        if isinstance(t, (int, float)) and t and isinstance(d, (int, float)) and d:
            return int((t * 1000.0 * d) / 8)  # rough
        return 10**12

    if not fs:
        return str(data.get("format_id") or "") or None
    fs.sort(key=approx_bytes)
    fid = fs[0].get("format_id")
    if fid is None:
        return None
    return str(int(fid) if str(fid).isdigit() else fid)


def download_url(url: str, client: str, format_spec: str, out: Path) -> tuple[float, int, str]:
    t0 = time.perf_counter()
    if out.parent.exists():
        for p in out.parent.iterdir():
            if p.is_file() and p.name.startswith(out.stem):
                p.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    r = subprocess.run(
        [
            str(VENV),
            "--no-warnings",
            "--no-cache-dir",
            "--socket-timeout",
            str(SOCKET),
            "--retries",
            "0",
            "--fragment-retries",
            "0",
            "--extractor-args",
            f"youtube:player_client={client}",
            "-f",
            format_spec,
            "-o",
            str(out),
            url,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    t = time.perf_counter() - t0
    s = int(out.stat().st_size) if out.is_file() else 0
    err = ""
    if r.returncode != 0 or s < 2000:
        err = (r.stderr or r.stdout or "")[:600]
    return t, s, err


def main() -> int:
    if not VENV.is_file():
        print(f"no yt-dlp: {VENV}", file=sys.stderr)
        return 1

    print(f"YBENCH_URL={URL!r} socket={SOCKET}\n", file=sys.stderr)
    print("client,format_id,sec,MiB,avg_MiB_s,status", file=sys.stderr)
    good: list[tuple[str, float, float, float, str]] = []

    for i, c in enumerate(CLIENTS):
        d = extract_json(URL, c)
        if d is None:
            print(f"{c},,0,0,0,skip: no json / extract", file=sys.stderr)
            if i < len(CLIENTS) - 1:
                time.sleep(0.5)
            continue
        fmt = pick_smallest_format_id(d)
        if not fmt:
            print(f"{c},,0,0,0,skip: no format_id", file=sys.stderr)
            if i < len(CLIENTS) - 1:
                time.sleep(0.5)
            continue
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / f"d_{c}.bin"
            sec, sz, err = download_url(URL, c, fmt, p)
        mib = sz / 1024 / 1024
        mps = (mib / sec) if sec > 0.05 and sz > 0 and not err else 0.0
        err_s = (err[:120] if err else "")
        st = f"ok {mib:.2f} MiB" if not err and sz > 0 else f"err {err_s!r}"
        print(f"{c},{fmt},{sec:.2f},{mib:.3f},{mps:.3f},{st}", file=sys.stderr)
        if not err and sz > 0:
            good.append((c, mps, sec, mib, str(fmt)))
        if i < len(CLIENTS) - 1:
            time.sleep(1.0)

    print("", file=sys.stderr)
    if not good:
        print("RECOMMENDED_CHAIN: (no successful run — EJS/deno для web, cookies, или другой IP)", file=sys.stderr)
        print("YOUTUBE_PLAYER_CLIENT_FALLBACK=web,ios,mweb", file=sys.stderr)
        return 2
    # по скорости, при равенстве по времени
    good.sort(key=lambda r: (-r[1], r[2], -r[3]))
    chain = ",".join(x[0] for x in good)
    order = "RECOMMENDED_ORDER (fastest first) = " + ",".join(f"{x[0]}~{x[1]:.2f}MiB/s" for x in good)
    print(order, file=sys.stderr)
    # stdout: single line for .env
    print(chain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
