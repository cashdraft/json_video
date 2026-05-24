"""Поиск stock-медиа для Scenes Stock (Pexels video — первый источник)."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from scenes_stock_board import normalize_queries
from scenes_stock_modes import normalize_source

BASE_DIR = Path(__file__).resolve().parent
SCENES_STOCK_MEDIA_DIR = BASE_DIR / "data" / "scenes_stock" / "media"

STOCK_SEARCH_RESULT_LIMIT = 16

PEXELS_ENV_API_KEY = (os.getenv("PEXELS_API_KEY") or "").strip()


def resolve_pexels_api_key(prefs: dict[str, Any] | None = None) -> str:
    if isinstance(prefs, dict):
        from_prefs = str(prefs.get("pexels_api_key") or "").strip()
        if from_prefs:
            return from_prefs
    return PEXELS_ENV_API_KEY


def stock_search_ready(prefs: dict[str, Any] | None = None) -> bool:
    return bool(resolve_pexels_api_key(prefs))


def _target_orientation(aspect_ratio: str) -> str:
    s = str(aspect_ratio or "").strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", s)
    if not m:
        return "landscape"
    try:
        w = float(m.group(1))
        h = float(m.group(2))
    except (TypeError, ValueError):
        return "landscape"
    if w > h:
        return "landscape"
    if h > w:
        return "portrait"
    return "any"


def _orientation_ok(w: int, h: int, want: str) -> bool:
    if w <= 0 or h <= 0:
        return False
    if want == "any":
        return True
    if want == "landscape":
        return w >= h
    if want == "portrait":
        return h >= w
    return True


def _pexels_search_videos(
    *,
    query: str,
    api_key: str,
    per_page: int = 20,
    aspect_ratio: str = "16:9",
) -> tuple[list[dict[str, Any]], str | None]:
    key = str(api_key or "").strip()
    if not key:
        return [], "Укажите Pexels API key на странице (поле под «Источник») и нажмите OK."
    q = str(query or "").strip()
    if not q:
        return [], "Пустой запрос."
    pp = max(1, min(80, int(per_page or 20)))
    want_orient = _target_orientation(aspect_ratio)
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": q, "per_page": pp, "page": 1},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return [], f"Pexels API: {exc}"

    rows = data.get("videos") if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return [], None

    for v in rows:
        if not isinstance(v, dict):
            continue
        files = v.get("video_files") if isinstance(v.get("video_files"), list) else []
        mp4_url = ""
        pick_w = 0
        pick_h = 0
        pick_area = 0
        for f in files:
            if not isinstance(f, dict):
                continue
            link = str(f.get("link") or "")
            ftype = str(f.get("file_type") or "")
            fw = int(f.get("width") or 0)
            fh = int(f.get("height") or 0)
            if not (link and ("mp4" in ftype.lower() or link.lower().endswith(".mp4"))):
                continue
            if not _orientation_ok(fw, fh, want_orient):
                continue
            area = fw * fh
            if area > pick_area:
                pick_area = area
                pick_w = fw
                pick_h = fh
                mp4_url = link
        if not mp4_url:
            vw = int(v.get("width") or 0)
            vh = int(v.get("height") or 0)
            if not _orientation_ok(vw, vh, want_orient):
                continue
            for f in files:
                if not isinstance(f, dict):
                    continue
                link = str(f.get("link") or "")
                ftype = str(f.get("file_type") or "")
                if link and ("mp4" in ftype.lower() or link.lower().endswith(".mp4")):
                    mp4_url = link
                    pick_w = vw
                    pick_h = vh
                    break
        img = str(v.get("image") or "")
        if not mp4_url and not img:
            continue
        user = v.get("user") if isinstance(v.get("user"), dict) else {}
        author_id = str(user.get("id") or "").strip()
        author_name = str(user.get("name") or "").strip()
        out.append({
            "type": "video",
            "thumbnail_url": img,
            "media_url": mp4_url,
            "source_url": str(v.get("url") or ""),
            "author": author_name,
            "author_id": author_id,
            "width": pick_w,
            "height": pick_h,
        })
        if len(out) >= pp:
            break
    return out, None


def _author_key(row: dict[str, Any]) -> str:
    aid = str(row.get("author_id") or "").strip()
    if aid:
        return f"id:{aid}"
    name = str(row.get("author") or "").strip().lower()
    if name:
        return f"name:{name}"
    return ""


def _merge_round_robin(*, queries: list[str], by_kw: dict[str, list[dict[str, Any]]], limit: int = STOCK_SEARCH_RESULT_LIMIT) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_authors: set[str] = set()

    def can_take(row: dict[str, Any]) -> bool:
        url_key = str(row.get("media_url") or row.get("thumbnail_url") or "").strip()
        if not url_key or url_key in seen_urls:
            return False
        author_key = _author_key(row)
        if author_key and author_key in seen_authors:
            return False
        return True

    def take(row: dict[str, Any]) -> None:
        url_key = str(row.get("media_url") or row.get("thumbnail_url") or "").strip()
        author_key = _author_key(row)
        seen_urls.add(url_key)
        if author_key:
            seen_authors.add(author_key)
        items.append(dict(row))

    for round_idx in range(40):
        progressed = False
        for kw in queries:
            pool = by_kw.get(kw) or []
            if round_idx >= len(pool):
                continue
            row = pool[round_idx]
            if not can_take(row):
                continue
            take(row)
            progressed = True
            if len(items) >= limit:
                break
        if len(items) >= limit or not progressed:
            break
    if len(items) < limit:
        for kw in queries:
            for row in by_kw.get(kw) or []:
                if not can_take(row):
                    continue
                take(row)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
    return items


def search_stock_for_queries(
    *,
    source_id: str,
    queries: Any,
    api_key: str = "",
    aspect_ratio: str = "16:9",
    limit: int = STOCK_SEARCH_RESULT_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    normalize_source(source_id)
    key = str(api_key or "").strip()
    kws = normalize_queries(queries)
    if not kws:
        return [], "Нет поисковых запросов."

    by_kw: dict[str, list[dict[str, Any]]] = {}
    for kw in kws:
        chunk, err = _pexels_search_videos(query=kw, api_key=key, per_page=20, aspect_ratio=aspect_ratio)
        if err:
            continue
        uniq: list[dict[str, Any]] = []
        local_seen: set[str] = set()
        local_authors: set[str] = set()
        for it in chunk:
            url_key = str(it.get("media_url") or it.get("thumbnail_url") or "").strip()
            if not url_key or url_key in local_seen:
                continue
            author_key = _author_key(it)
            if author_key and author_key in local_authors:
                continue
            local_seen.add(url_key)
            if author_key:
                local_authors.add(author_key)
            row = dict(it)
            row["found_by_query"] = kw
            uniq.append(row)
        if uniq:
            by_kw[kw] = uniq
    items = _merge_round_robin(queries=kws, by_kw=by_kw, limit=limit)
    if not items:
        return [], "По запросам ничего не найдено в Pexels (проверьте API key и фильтры)."
    return items, None


def _media_ext_from_url(url: str) -> str:
    low = str(url or "").lower().split("?", 1)[0]
    for ext in (".mp4", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp"):
        if low.endswith(ext):
            return ext
    return ".mp4"


def _fetch_url_bytes(url: str, *, cap: int = 80_000_000) -> bytes | None:
    src = str(url or "").strip()
    if not src:
        return None
    try:
        r = requests.get(src, timeout=60, stream=True)
        r.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > cap:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except requests.RequestException:
        return None


def persist_search_items(*, scene_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    SCENES_STOCK_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(scene_id or "scene").strip()) or "scene"
    nonce = int(time.time() * 1000)
    saved: list[dict[str, Any]] = []
    for i, it in enumerate(items, start=1):
        row = dict(it or {})
        media_src = str(row.get("media_url") or "").strip()
        if media_src:
            bts = _fetch_url_bytes(media_src)
            if bts:
                ext = _media_ext_from_url(media_src)
                fname = f"{safe_sid}_{nonce}_{i:02d}{ext}"
                fp = SCENES_STOCK_MEDIA_DIR / fname
                try:
                    fp.write_bytes(bts)
                    row["local_url"] = f"/scenes-stock/media/{fname}"
                except OSError:
                    pass
        if "local_url" not in row:
            row["local_url"] = media_src or str(row.get("thumbnail_url") or "")
        saved.append(row)
    return saved
