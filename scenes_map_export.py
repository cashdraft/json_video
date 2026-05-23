"""Экспорт wire payload OpenAI для /scenes-map (кнопка J)."""

from __future__ import annotations

from typing import Any

from scenes_map_agent import (
    build_generation_context,
    build_scenemap_generation_context,
    build_wire_payload,
    compose_macromap_user_message,
)
from scenes_map_session import apply_prompt_macros, load_prefs

SCENES_MAP_EXPORT_ABOUT = (
    "Логика входов как у кнопки ↻: тот же JSON со страницы (collectPayload), на сервере те же "
    "apply_prompt_macros и сборка user/system, что и в POST /scenes-map/api/generate. "
    "Дальше: для одного POST на вызов — то же, что перед HTTP: "
    "openai_chat_completions_request_dict / rewrite_chat_completion_wire_payload "
    "(нормализация model, sanitize на system/user, temperature=REWRITE_CHAT_TEMPERATURE). "
    "SceneMap Agent шлёт несколько POST подряд — в requests[] по одному объекту на каждый macro block "
    "(если нет сохранённых scene_map_block_*.jsonl, PREVIOUS_SCENE_TAIL для следующих блоков может "
    "отличаться от живого прогона — см. notes). "
    "Файл — читаемый JSON (UTF-8, отступы); реальное тело POST кодируется компактнее (другой вид сериализации JSON). "
    "Здесь messages[].content и (для Claude) поле system могут быть развёрнуты в объекты и в пометки "
    "{\"_export\":\"text_lines\",\"lines\":[...]} — это только в этом файле для просмотра; в HTTP к API такого нет, там всегда строки."
)


def merge_prefs_snapshot(body: dict[str, Any] | None) -> dict[str, Any]:
    prefs = load_prefs()
    if isinstance(body, dict):
        prefs.update(body)
    return prefs


def export_macromap_wire_bodies(prefs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    ctx = build_generation_context(prefs)
    system_prompt = apply_prompt_macros(str(ctx.get("system_prompt") or ""), prefs)
    user_prompt = str(ctx.get("user_prompt") or "")
    inbox = str(ctx.get("inbox") or "")
    user_body = compose_macromap_user_message(user_prompt=user_prompt, inbox=inbox)
    if not user_body:
        return [], ["[MacroMap] Заполните User Prompt и/или Inbox — тело POST не формируется."], None
    wire = build_wire_payload(
        model=str(ctx.get("model") or ""),
        system_prompt=system_prompt,
        user_body=user_body,
    )
    return [wire], [], None


def _load_saved_block_scenes(state: dict[str, Any], block_index: int) -> list[dict[str, Any]] | None:
    from scenes_map_pipeline import SCENE_MAP_DIR, parse_scenemap_jsonl

    for row in state.get("block_results") or []:
        if not isinstance(row, dict):
            continue
        if row.get("block_index") != block_index or not row.get("ok"):
            continue
        path = SCENE_MAP_DIR / str(row.get("file") or "")
        if not path.is_file():
            return None
        scenes, _ = parse_scenemap_jsonl(path.read_text(encoding="utf-8"))
        return scenes
    return None


def export_scenemap_wire_bodies(prefs: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    from scenes_map_pipeline import (
        apply_scenemap_block_macros,
        load_macromap_from_prefs,
        load_run_state,
        macro_map_strip_text,
        previous_scene_tail,
    )

    blocks, global_summary, err = load_macromap_from_prefs(prefs)
    if err:
        return [], [], err
    if not blocks:
        return [], ["[SceneMap] В Result AS нет macro blocks — тело POST не формируется."], None

    ctx = build_scenemap_generation_context(prefs)
    user_template = str(ctx.get("user_prompt") or "")
    model = str(ctx.get("model") or "")
    system_prompt = str(ctx.get("system_prompt") or "")
    macro_no_text = macro_map_strip_text(blocks)
    state = load_run_state()

    out: list[dict[str, Any]] = []
    accumulated: list[dict[str, Any]] = []
    context_exact = True

    for bi, current in enumerate(blocks):
        tail = previous_scene_tail(accumulated)
        prompt = apply_scenemap_block_macros(
            user_template,
            prefs,
            macro_map_no_text=macro_no_text,
            current_block=dict(current),
            previous_tail=tail,
            global_summary=global_summary,
        )
        out.append(
            build_wire_payload(
                model=model,
                system_prompt=system_prompt,
                user_body=prompt,
            )
        )
        saved = _load_saved_block_scenes(state, bi)
        if saved is not None:
            accumulated.extend(saved)
        elif bi < len(blocks) - 1:
            context_exact = False

    hdr: list[str] = []
    if not context_exact:
        hdr.append(
            "Ниже — те же JSON-тела, что собирает SceneMap Agent перед POST по каждому macro block. "
            "PREVIOUS_SCENE_TAIL для блоков без сохранённых scene_map_block_*.jsonl подставлен из уже "
            "обработанных файлов там, где они есть; иначе — пустой или неполный хвост (так не будет при живом первом прогоне)."
        )
    return out, hdr, None
