"""
Kie.ai Nano Banana Pro API client for image generation.
Docs: https://docs.kie.ai/market/google/pro-image-to-image
"""

import json
import os
import time
from typing import Any

import requests

API_BASE = "https://api.kie.ai"
CREATE_TASK = f"{API_BASE}/api/v1/jobs/createTask"
GET_TASK = f"{API_BASE}/api/v1/jobs/recordInfo"


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
