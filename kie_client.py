"""
Kie.ai API client for image and video generation.
Docs:
- https://docs.kie.ai/market/google/pro-image-to-image
- https://docs.kie.ai/veo3-api/generate-veo-3-video/
"""

import json
import os
import time
from typing import Any

import requests

API_BASE = "https://api.kie.ai"
CREATE_TASK = f"{API_BASE}/api/v1/jobs/createTask"
GET_TASK = f"{API_BASE}/api/v1/jobs/recordInfo"
CREATE_VIDEO_TASK = f"{API_BASE}/api/v1/veo/generate"
GET_VIDEO_TASK = f"{API_BASE}/api/v1/veo/record-info"


def _get_api_key() -> str:
    key = os.getenv("KEYAI_API_KEY") or os.getenv("KIE_API_KEY")
    if not key:
        raise ValueError("KEYAI_API_KEY or KIE_API_KEY not set in .env")
    return key.strip()


def create_image_task(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "2K",
    output_format: str = "jpg",
) -> str:
    """Create image generation task. Returns taskId."""
    api_key = _get_api_key()
    payload = {
        "model": "nano-banana-pro",
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "output_format": output_format,
        },
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
    return task_id


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
) -> str:
    """Create video generation task. Returns taskId."""
    api_key = _get_api_key()
    payload = {
        "prompt": prompt,
        "model": _map_video_model(model),
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
    return task_id


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


def generate_image(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "2K",
    output_format: str = "jpg",
    poll_interval: float = 2.0,
    max_wait: float = 120.0,
) -> str:
    """
    Create task, poll until done, return first result URL.
    Raises RuntimeError on failure.
    """
    task_id = create_image_task(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_format=output_format,
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
