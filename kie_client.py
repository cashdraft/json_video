"""
Kie.ai API client for image and video generation.
Docs:
- https://docs.kie.ai/market/google/pro-image-to-image
- https://docs.kie.ai/veo3-api/generate-veo-3-video/
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Tuple

import requests

API_BASE = "https://api.kie.ai"
CREATE_TASK = f"{API_BASE}/api/v1/jobs/createTask"
GET_TASK = f"{API_BASE}/api/v1/jobs/recordInfo"
CREATE_VIDEO_TASK = f"{API_BASE}/api/v1/veo/generate"
GET_VIDEO_TASK = f"{API_BASE}/api/v1/veo/record-info"
GET_VIDEO_1080P = f"{API_BASE}/api/v1/veo/get-1080p-video"
_ENV_PATH = Path(__file__).resolve().parent / ".env"
_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Подхватить .env из каталога приложения (рядом с kie_client.py), если ключ ещё не в os.environ."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    if not _ENV_PATH.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_PATH, override=True)
    except OSError:
        return


def _get_api_key() -> str:
    key = (os.getenv("KEYAI_API_KEY") or os.getenv("KIE_API_KEY") or "").strip()
    if not key:
        _load_dotenv_once()
        key = (os.getenv("KEYAI_API_KEY") or os.getenv("KIE_API_KEY") or "").strip()
    if not key:
        raise ValueError(
            "KEYAI_API_KEY or KIE_API_KEY not set in .env — "
            f"expected non-empty value in {_ENV_PATH}; then run: systemctl restart json-video. "
            "If the key is already there, click ↻ again: an old error may still show until a new request."
        )
    return key


def normalize_aspect_ratio(value: str | None, default: str = "16:9") -> str:
    """Normalize UI/user values to Kie.ai format (W:H)."""
    raw = (value or "").strip()
    if not raw:
        return default
    raw = raw.replace("/", ":").replace(" ", "")
    if raw == "9:16":
        return "16:9"
    allowed = {"16:9", "1:1", "3:2", "2:3"}
    return raw if raw in allowed else default


def create_image_task(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "2K",
    output_format: str = "jpg",
    image_input: list[str] | None = None,
    model: str = "nano-banana-pro",
) -> Tuple[str, str]:
    """Create image generation task. Returns (taskId, model field sent in JSON body)."""
    api_key = _get_api_key()
    mid_raw = (model or "").strip().lower()
    if mid_raw not in {"nano-banana-pro", "nano-banana-2"}:
        mid_raw = "nano-banana-pro"

    ratio = normalize_aspect_ratio(aspect_ratio, "16:9")
    inp: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": ratio,
        # Some Kie endpoints/examples use camelCase; send both for compatibility.
        "aspectRatio": ratio,
        "resolution": resolution,
        "output_format": output_format,
    }
    if image_input:
        inp["image_input"] = image_input
    payload = {
        "model": mid_raw,
        "input": inp,
    }
    resp = requests.post(
        CREATE_TASK,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 200:
        msg = data.get("msg", resp.text)
        raise RuntimeError(f"Kie.ai API error: {msg}")
    task_id = data.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError("No taskId in response")
    sent_model = str(payload["model"])
    return task_id, sent_model


def get_task_result(task_id: str) -> dict[str, Any]:
    """Get task status and result. Returns {state, result_urls?, error?}."""
    api_key = _get_api_key()
    resp = requests.get(
        GET_TASK,
        params={"taskId": task_id},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 200:
        raise RuntimeError(f"Kie.ai API error: {data.get('msg', resp.text)}")
    task_data = data.get("data", {})
    state = task_data.get("state", "unknown")
    result = {"state": state}
    if state == "success":
        result_json = task_data.get("resultJson", "{}")
        try:
            parsed = json.loads(result_json)
            urls = parsed.get("resultUrls", [])
            result["result_urls"] = urls
        except json.JSONDecodeError:
            result["result_urls"] = []
    elif state == "fail":
        result["error"] = task_data.get("failMsg", "Generation failed")
    return result


def _map_video_model(model: str) -> str:
    """
    Normalize UI model names to Kie API values.
    UI has "veo3-fast", API expects "veo3_fast".
    """
    normalized = (model or "").strip().lower()
    if normalized == "veo3-fast":
        return "veo3_fast"
    if normalized in {"veo3", "veo3_fast"}:
        return normalized
    return "veo3_fast"


def create_video_task(
    prompt: str,
    model: str = "veo3_fast",
    aspect_ratio: str = "16:9",
    image_urls: list[str] | None = None,
    generation_type: str = "TEXT_2_VIDEO",
) -> Tuple[str, str]:
    """Create video generation task. Returns (taskId, model field sent in JSON body)."""
    api_key = _get_api_key()
    mapped_model = _map_video_model(model)
    payload = {
        "prompt": prompt,
        "model": mapped_model,
        "generationType": generation_type,
        "aspect_ratio": aspect_ratio,
    }
    if image_urls:
        payload["imageUrls"] = image_urls
    resp = requests.post(
        CREATE_VIDEO_TASK,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 200:
        msg = data.get("msg", resp.text)
        raise RuntimeError(f"Kie.ai API error: {msg}")
    task_id = data.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError("No taskId in response")
    return task_id, mapped_model


def get_video_task_result(task_id: str) -> dict[str, Any]:
    """
    Get Veo task status and result.
    Returns {state, result_urls?, error?} where state matches waiting/generating/success/fail.
    """
    api_key = _get_api_key()
    resp = requests.get(
        GET_VIDEO_TASK,
        params={"taskId": task_id},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 200:
        raise RuntimeError(f"Kie.ai API error: {data.get('msg', resp.text)}")

    task_data = data.get("data", {})
    success_flag = task_data.get("successFlag")
    result: dict[str, Any] = {}
    if success_flag == 1:
        result["state"] = "success"
        response_obj = task_data.get("response") or {}
        result["result_urls"] = response_obj.get("resultUrls", []) or []
    elif success_flag in (2, 3):
        result["state"] = "fail"
        result["error"] = task_data.get("errorMessage") or "Video generation failed"
    else:
        # Veo API uses successFlag 0 for in-progress.
        result["state"] = "generating"
    return result


def get_video_1080p_result(task_id: str, index: int = 0) -> dict[str, Any]:
    """
    Request/check 1080p upscaled video by original task id.
    Returns:
      - {"ready": True, "url": "..."} when available
      - {"ready": False} when still processing/not ready
    Raises RuntimeError on non-recoverable API errors.
    """
    api_key = _get_api_key()
    resp = requests.get(
        GET_VIDEO_1080P,
        params={"taskId": task_id, "index": index},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    # Kie endpoints sometimes return business code in JSON while HTTP remains 200.
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Kie.ai API error: {resp.text}")

    code = data.get("code")
    msg = str(data.get("msg") or "").strip()
    msg_l = msg.lower()
    if resp.status_code == 200 and code == 200:
        url = (data.get("data") or {}).get("resultUrl", "") or (data.get("data") or {}).get("result_url", "")
        if url:
            return {"ready": True, "url": url}
        return {"ready": False}

    # Not ready yet — Kie часто отвечает 422/429 или текстом «try again later».
    if code in (422, 429):
        return {"ready": False}
    if any(
        p in msg_l
        for p in (
            "try again later",
            "being generated",
            "still processing",
            "not ready",
            "in progress",
            "please wait",
        )
    ):
        return {"ready": False}

    raise RuntimeError(f"Kie.ai API error: {msg or resp.text}")


def generate_image(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "2K",
    output_format: str = "jpg",
    poll_interval: float = 2.0,
    max_wait: float = 120.0,
    model: str = "nano-banana-pro",
) -> str:
    """
    Create task, poll until done, return first result URL.
    Raises RuntimeError on failure.
    """
    task_id, _sent_model = create_image_task(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_format=output_format,
        model=model,
    )
    start = time.time()
    while time.time() - start < max_wait:
        result = get_task_result(task_id)
        state = result.get("state", "")
        if state == "success":
            urls = result.get("result_urls", [])
            if urls:
                return urls[0]
            raise RuntimeError("No URLs in result")
        if state == "fail":
            raise RuntimeError(result.get("error", "Generation failed"))
        time.sleep(poll_interval)
    raise RuntimeError("Generation timeout")
