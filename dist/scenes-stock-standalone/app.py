"""Standalone Flask app: Scenes Stock (Finder Agent + Pexels search)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "scenes-stock-dev-change-me")


def _static_mtime(name: str) -> str:
    p = BASE_DIR / "static" / name
    try:
        return str(int(p.stat().st_mtime)) if p.is_file() else "0"
    except OSError:
        return "0"


def _scenes_stock_asset_mtime() -> str:
    parts = [_static_mtime(n) for n in ("scenes_stock.css", "scenes_stock.js")]
    return "-".join(parts) if parts else "0"


@app.route("/")
def index_redirect():
    from flask import redirect

    return redirect("/scenes-stock", code=302)


@app.route("/scenes-stock")
def scenes_stock_page():
    from scenes_stock_agent import finder_api_ready, finder_models_for_ui
    from scenes_stock_search import stock_search_ready
    from scenes_stock_session import prefs_for_page

    prefs = prefs_for_page()
    return render_template(
        "scenes_stock.html",
        api_key_set=finder_api_ready(),
        pexels_key_set=stock_search_ready(prefs),
        models=finder_models_for_ui(),
        prefs=prefs,
        static_style_mtime=_static_mtime("style.css"),
        scenes_stock_asset_mtime=_scenes_stock_asset_mtime(),
    )


@app.route("/scenes-stock/api/prefs", methods=["GET", "POST"])
def scenes_stock_api_prefs():
    from scenes_stock_session import load_prefs, save_prefs

    if request.method == "GET":
        return jsonify({"ok": True, "prefs": load_prefs()})
    body = request.get_json(silent=True) or {}
    saved = save_prefs(body if isinstance(body, dict) else {})
    return jsonify({"ok": True, "prefs": saved})


@app.route("/scenes-stock/api/generate", methods=["POST"])
def scenes_stock_api_generate():
    from scenes_map_agent import model_key_ok
    from scenes_stock_agent import build_finder_generation_context, run_finder_agent
    from scenes_stock_session import apply_prompt_macros, load_prefs, save_prefs

    body = request.get_json(silent=True) or {}
    agent = str(body.get("agent") or "finder").strip().lower()
    if agent != "finder":
        return jsonify({"ok": False, "error": f"Неизвестный агент: {agent}"}), 400

    prefs = load_prefs()
    if isinstance(body, dict):
        prefs = save_prefs(body)

    ctx = build_finder_generation_context(prefs)
    if not model_key_ok(ctx["model"]):
        return jsonify({"ok": False, "error": "Нет API-ключа для выбранной модели."}), 400

    system_prompt = apply_prompt_macros(str(ctx.get("system_prompt") or ""), prefs)
    user_prompt = str(ctx.get("user_prompt") or "")
    scene = str(ctx.get("scene") or "")

    answer, err = run_finder_agent(
        model=str(ctx.get("model") or ""),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        scene=scene,
    )
    if err or answer is None:
        return jsonify({"ok": False, "error": err or "generation_failed", "raw": answer or ""}), 502

    saved = save_prefs({**prefs, "result": answer})
    return jsonify({"ok": True, "result": answer, "prefs": saved, "board": saved.get("board") or []})


@app.route("/scenes-stock/api/export", methods=["POST"])
def scenes_stock_api_export():
    from scenes_stock_export import SCENES_STOCK_EXPORT_ABOUT, export_finder_wire_bodies, merge_prefs_snapshot
    from wire_export import format_openai_wire_payloads_txt

    body = request.get_json(silent=True) or {}
    agent = str(body.get("agent") or "finder").strip().lower()
    if agent != "finder":
        return jsonify({"ok": False, "error": f"Неизвестный агент: {agent}"}), 400

    prefs = merge_prefs_snapshot(body)
    bodies, hdr, err = export_finder_wire_bodies(prefs)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    txt = format_openai_wire_payloads_txt(bodies, header_lines=hdr, about=SCENES_STOCK_EXPORT_ABOUT)
    resp = make_response(txt)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="scenes_stock_finder_openai_request.json"'
    return resp


@app.route("/scenes-stock/media/<path:filename>")
def scenes_stock_media(filename: str):
    from scenes_stock_search import SCENES_STOCK_MEDIA_DIR

    safe = Path(secure_filename(Path(filename).name))
    if not safe.name:
        return "Not found", 404
    path = (SCENES_STOCK_MEDIA_DIR / safe.name).resolve()
    root = SCENES_STOCK_MEDIA_DIR.resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        return "Not found", 404
    resp = send_from_directory(root, safe.name)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/scenes-stock/api/search", methods=["POST"])
def scenes_stock_api_search():
    from scenes_stock_board import find_board_scene, normalize_board, normalize_queries
    from scenes_stock_modes import normalize_source
    from scenes_stock_search import (
        STOCK_SEARCH_RESULT_LIMIT,
        persist_search_items,
        resolve_pexels_api_key,
        search_stock_for_queries,
    )
    from scenes_stock_session import load_prefs, save_prefs

    body = request.get_json(silent=True) or {}
    scene_id = str(body.get("scene_id") or "").strip()
    if not scene_id:
        return jsonify({"ok": False, "error": "missing_scene_id"}), 400

    prefs = load_prefs()
    if isinstance(body, dict) and body.get("board"):
        prefs = save_prefs({**prefs, "board": body.get("board")})

    inline_key = str(body.get("pexels_api_key") or "").strip()
    source = normalize_source(body.get("source") or prefs.get("source"))
    pexels_key = inline_key or resolve_pexels_api_key(prefs)
    if not pexels_key:
        return jsonify({"ok": False, "error": "Укажите Pexels API key на странице и нажмите OK."}), 400

    board = normalize_board(prefs.get("board"))
    scene = find_board_scene(board, scene_id)
    if scene is None:
        return jsonify({"ok": False, "error": "scene_not_found"}), 404

    queries = normalize_queries(body.get("queries") if "queries" in body else scene.get("queries"))
    visual_intent = str(
        body.get("visual_intent") if "visual_intent" in body else scene.get("visual_intent") or ""
    ).strip()

    items, err = search_stock_for_queries(
        source_id=source,
        queries=queries,
        api_key=pexels_key,
        aspect_ratio="16:9",
        limit=STOCK_SEARCH_RESULT_LIMIT,
    )
    if err:
        for row in board:
            if str(row.get("scene_id") or "").strip() == scene_id:
                row["visual_intent"] = visual_intent
                row["queries"] = queries if isinstance(queries, list) else scene.get("queries")
                row["search"] = {
                    "status": "error",
                    "error": err,
                    "items": [],
                    "searched_at": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                }
        saved = save_prefs({**prefs, "board": board})
        return jsonify({"ok": False, "error": err, "board": saved.get("board") or []}), 502

    saved_items = persist_search_items(scene_id=scene_id, items=items)
    searched_at = datetime.now(timezone.utc).isoformat()
    for row in board:
        if str(row.get("scene_id") or "").strip() == scene_id:
            row["visual_intent"] = visual_intent
            row["queries"] = queries if isinstance(queries, list) else scene.get("queries")
            row["search"] = {
                "status": "done",
                "error": "",
                "items": saved_items,
                "searched_at": searched_at,
                "source": source,
            }

    saved = save_prefs({**prefs, "board": board})
    return jsonify({
        "ok": True,
        "scene_id": scene_id,
        "source": source,
        "items": saved_items,
        "board": saved.get("board") or [],
    })
