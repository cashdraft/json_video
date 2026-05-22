"""Анимация для выбранного слота: промт + ответ модели кадра → JSON таймлайна."""

from __future__ import annotations

from typing import Any

from later_anim_dictionary import anim_dictionary_prompt_block
from later_response_parse import (
    process_animation_model_response,
    replace_animation_in_later_text,
)
from scenes_lab_img_slots import (
    img_slot_preview_public_url,
    load_img_slot_repaired_response,
    load_img_slot_response,
    save_img_slot_anim_response,
    update_img_slot_response_text,
)
from scenes_lab_later import apply_later_prompt_macros, run_later_model_request

ANIM_SYSTEM_PROMPT = (
    "Ты генерируешь только JSON-таймлайн анимации для уже готовой SVG-инфографики. "
    "SVG менять запрещено. К запросу приложено превью кадра — учти композицию и порядок появления. "
    "Язык пояснений в NOTES не нужен.\n\n"
    "Верни РОВНО один блок, без markdown ```:\n"
    "===ANIM_START===\n"
    '{"duration_sec": 5.4, "fps": 30, "tracks": [{"id": "word-1", "anim": "fade-in", "start": 0, "end": 12}]}\n'
    "===ANIM_END===\n\n"
    + anim_dictionary_prompt_block()
    + "\n\ntracks[].id — id группы <g> из SVG этого кадра. anim — ТОЛЬКО из словаря выше. "
    "end ≤ duration_sec × fps."
)


def build_animate_user_message(
    anim_prompt: str,
    slot_response: str,
    *,
    scene_description: str = "",
    scene_duration_sec: str = "",
) -> str:
    parts = []
    ap = apply_later_prompt_macros(
        anim_prompt or "",
        scene_description=scene_description,
        scene_duration_sec=scene_duration_sec,
    ).strip()
    if ap:
        parts.append(f"Запрос на анимацию:\n{ap}")
    prev = (slot_response or "").strip()
    if prev:
        parts.append(
            "Текущий ответ модели по этому кадру (SVG уже зафиксирован — "
            "не меняй SVG, верни только блок ===ANIM_START=== … ===ANIM_END===):\n"
            + prev
        )
    else:
        parts.append(
            "По приложенному превью кадра сгенерируй JSON анимации для элементов SVG."
        )
    parts.append(
        "Не возвращай ===SVG_START=== и не дублируй весь старый ответ — только ANIM."
    )
    return "\n\n".join(parts)


def run_animate_flow(
    *,
    model: str,
    anim_prompt: str,
    slot_id: str,
    public_base: str,
    slot_response: str = "",
    scene_description: str = "",
    scene_duration_sec: str = "",
) -> dict[str, Any]:
    sid = (slot_id or "").strip()
    if not sid:
        return {"ok": False, "error": "Не указан слот кадра (img_N)."}

    from scenes_lab_img_slots import _slot_dir

    slot_path = _slot_dir(sid)
    if slot_path is None:
        return {"ok": False, "error": f"Слот {sid!r} не найден."}

    svg_path = slot_path / "scene.svg"
    if not svg_path.is_file():
        return {"ok": False, "error": f"В слоте {sid} нет scene.svg — сначала соберите кадр."}

    svg = svg_path.read_text(encoding="utf-8")
    # Всегда ответ и превью одного слота с диска; SVG в тексте — починенный из scene.svg.
    previous_response = load_img_slot_repaired_response(sid).strip()
    if not previous_response and (slot_response or "").strip():
        previous_response = (slot_response or "").strip()

    preview_url = img_slot_preview_public_url(sid, public_base, full=False)
    if not preview_url:
        preview_url = img_slot_preview_public_url(sid, public_base, full=True)
    if not preview_url:
        return {"ok": False, "error": f"В слоте {sid} нет превью PNG."}

    user_body = build_animate_user_message(
        anim_prompt,
        previous_response,
        scene_description=scene_description,
        scene_duration_sec=scene_duration_sec,
    )

    answer, err = run_later_model_request(
        model=model,
        user_prompt=user_body,
        image_url=preview_url,
        system_prompt=ANIM_SYSTEM_PROMPT,
    )
    if err:
        return {"ok": False, "error": err, "slot_id": sid}

    bundle = process_animation_model_response(answer or "", svg)
    validation = bundle.get("validation") if isinstance(bundle.get("validation"), dict) else {}
    parsed = bundle.get("parsed") if isinstance(bundle.get("parsed"), dict) else {}
    anim_raw = str(parsed.get("animation_raw") or "").strip()
    if not validation.get("ok") or not anim_raw:
        errs = validation.get("errors") or []
        return {
            "ok": False,
            "error": "; ".join(str(e) for e in errs[:5]) or "Валидация анимации не пройдена.",
            "text": answer or "",
            "anim_text": answer or "",
            "parsed": parsed,
            "validation": validation,
            "slot_id": sid,
        }

    merged_text, merge_err = replace_animation_in_later_text(previous_response, anim_raw)
    if merge_err:
        return {
            "ok": False,
            "error": merge_err,
            "text": answer or "",
            "anim_text": answer or "",
            "slot_id": sid,
        }

    update_img_slot_response_text(sid, merged_text)
    save_img_slot_anim_response(sid, answer or "", svg_snapshot=svg)

    return {
        "ok": True,
        "text": merged_text,
        "merged_text": merged_text,
        "anim_text": answer or "",
        "parsed": parsed,
        "validation": validation,
        "pipeline_ok": True,
        "slot_id": sid,
    }
