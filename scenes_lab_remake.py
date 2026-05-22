"""Переделать кадр: промт редактора + PNG последнего слота на сервере → новый слот img_N+1."""

from __future__ import annotations

from typing import Any

from later_response_parse import process_later_model_response
from scenes_lab_img_slots import (
    latest_img_slot_id,
    load_img_slot_response,
    next_img_slot_id,
    save_img_slot,
)
from scenes_lab_later import apply_later_prompt_macros, run_later_model_request
from scenes_lab_svg_patch import DEFAULT_SVG_PATCH_SYSTEM_PROMPT

REMAKE_SYSTEM_PROMPT = DEFAULT_SVG_PATCH_SYSTEM_PROMPT


def build_remake_user_message(
    editor_prompt: str,
    previous_response: str,
    *,
    scene_description: str = "",
    scene_duration_sec: str = "",
) -> str:
    parts = []
    ep = apply_later_prompt_macros(
        editor_prompt or "",
        scene_description=scene_description,
        scene_duration_sec=scene_duration_sec,
    ).strip()
    if ep:
        parts.append(f"Запрос на переделку:\n{ep}")
    prev = (previous_response or "").strip()
    if prev:
        parts.append(
            "Текущий ответ модели (исходник — правь по запросу и приложенному PNG-растру):\n"
            + prev
        )
    else:
        parts.append(
            "По приложенному PNG-растру сгенерируй улучшенную версию инфографики."
        )
    parts.append(
        "Верни только блок ===SVG_START=== … ===SVG_END=== (NOTES опционально). "
        "Без JSON анимации и без markdown ```."
    )
    return "\n\n".join(parts)


def run_remake_flow(
    *,
    model: str,
    editor_prompt: str,
    public_base: str,
    source_slot_id: str | None = None,
    slot_response: str = "",
    scene_description: str = "",
    scene_duration_sec: str = "",
) -> dict[str, Any]:
    """
    Берёт последний слот img_N на сервере (или явный source_slot_id): PNG + response.txt с диска.
    Отправляет в модель, сохраняет результат в следующий слот (img_2, img_3, …).
    """
    sid = (source_slot_id or "").strip() or latest_img_slot_id()
    if not sid:
        return {"ok": False, "error": "Нет сохранённых кадров (img_1). Сначала «Проверить и собрать»."}

    from scenes_lab_img_slots import img_slot_preview_public_url

    preview_url = img_slot_preview_public_url(sid, public_base, full=True)
    if not preview_url:
        return {"ok": False, "error": f"В слоте {sid} нет preview.png — сначала соберите кадр."}

    previous_response = load_img_slot_response(sid).strip()
    user_body = build_remake_user_message(
        editor_prompt,
        previous_response,
        scene_description=scene_description,
        scene_duration_sec=scene_duration_sec,
    )

    answer, err = run_later_model_request(
        model=model,
        user_prompt=user_body,
        image_url=preview_url,
        system_prompt=REMAKE_SYSTEM_PROMPT,
    )
    if err:
        return {"ok": False, "error": err, "source_slot_id": sid}

    bundle = process_later_model_response(answer or "")
    validation = bundle.get("validation") if isinstance(bundle.get("validation"), dict) else {}
    parsed = bundle.get("parsed") if isinstance(bundle.get("parsed"), dict) else {}
    svg = str(parsed.get("svg") or "").strip()
    if not validation.get("ok") or not svg:
        errs = validation.get("errors") or []
        return {
            "ok": False,
            "error": "; ".join(str(e) for e in errs[:5]) or "Валидация SVG не пройдена.",
            "text": answer or "",
            "parsed": parsed,
            "validation": validation,
            "source_slot_id": sid,
        }

    new_slot = next_img_slot_id()
    img_slot = save_img_slot(
        new_slot,
        full_text=answer or "",
        svg=svg,
        public_base=public_base,
    )
    return {
        "ok": True,
        "text": answer or "",
        "parsed": parsed,
        "validation": validation,
        "pipeline_ok": True,
        "source_slot_id": sid,
        "img_slot": img_slot,
        "slot_id": new_slot,
    }
