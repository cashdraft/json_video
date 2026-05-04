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
CREATE_GROK_VIDEO_TASK = f"{API_BASE}/api/v1/jobs/createTask"

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
    allowed = {"16:9", "9:16", "1:1", "3:2", "2:3"}
    return raw if raw in allowed else default


def _aspect_ratio_gpt_image2_i2i(ratio: str) -> str:
    """Map normalized W:H to gpt-image-2-image-to-image allowed aspect_ratio values."""
    r = (ratio or "").strip().replace("/", ":").replace(" ", "")
    if r in {"auto", "1:1", "9:16", "16:9", "4:3", "3:4"}:
        return r
    if r == "3:2":
        return "16:9"
    if r == "2:3":
        return "9:16"
    return "16:9"


def _aspect_ratio_wan_27(ratio: str) -> str:
    """Map UI aspect ratio to wan/2-7-image allowed values (text-to-image, no input_urls)."""
    r = (ratio or "").strip().replace("/", ":").replace(" ", "")
    allowed = {"1:1", "16:9", "4:3", "21:9", "3:4", "9:16", "8:1", "1:8"}
    if r in allowed:
        return r
    if r == "3:2":
        return "16:9"
    if r == "2:3":
        return "9:16"
    return "16:9"


def _image_size_qwen2_edit(ratio: str) -> str:
    """Map UI aspect ratio to qwen2/image-edit image_size enum."""
    r = (ratio or "").strip().replace("/", ":").replace(" ", "")
    allowed = {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"}
    if r in allowed:
        return r
    return "16:9"


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
    if mid_raw not in {
        "nano-banana-pro",
        "nano-banana-2",
        "gpt-image-2-image-to-image",
        "grok-imagine/image-to-image",
        "wan/2-7-image",
        "qwen2/image-edit",
    }:
        mid_raw = "nano-banana-pro"

    if mid_raw == "gpt-image-2-image-to-image":
        if not image_input:
            raise ValueError(
                "Для GPT Image 2 (image-to-image) нужны референс-URL: выберите шаблон с изображениями."
            )
        ratio_gpt = _aspect_ratio_gpt_image2_i2i(
            normalize_aspect_ratio(aspect_ratio, "16:9"),
        )
        inp_gpt: dict[str, Any] = {
            "prompt": prompt,
            "input_urls": image_input[:16],
            "aspect_ratio": ratio_gpt,
            "resolution": resolution,
        }
        payload = {"model": "gpt-image-2-image-to-image", "input": inp_gpt}
    elif mid_raw == "grok-imagine/image-to-image":
        if not image_input:
            raise ValueError(
                "Для Grok Imagine (image-to-image) нужны референс-URL: выберите шаблон с изображениями."
            )
        inp_grok: dict[str, Any] = {
            "prompt": prompt,
            "image_urls": image_input[:5],
            "nsfw_checker": False,
        }
        payload = {"model": "grok-imagine/image-to-image", "input": inp_grok}
    elif mid_raw == "wan/2-7-image":
        res = resolution if resolution in ("1K", "2K", "4K") else "2K"
        ratio_wan = _aspect_ratio_wan_27(normalize_aspect_ratio(aspect_ratio, "16:9"))
        inp_wan: dict[str, Any] = {
            "prompt": prompt,
            "n": 1,
            "enable_sequential": False,
            "resolution": res,
            "thinking_mode": False,
            "watermark": False,
            "seed": 0,
            "nsfw_checker": False,
        }
        if image_input:
            # Несколько input_urls в одном createTask у Wan = несколько правок/выходов и списаний.
            # Держим один референс + n=1 → одна платная генерация (см. также prompt/n в доке Kie).
            urls = image_input[:1]
            inp_wan["input_urls"] = urls
            inp_wan["bbox_list"] = [[]]
        else:
            inp_wan["aspect_ratio"] = ratio_wan
        payload = {"model": "wan/2-7-image", "input": inp_wan}
    elif mid_raw == "qwen2/image-edit":
        if not image_input:
            raise ValueError(
                "Для Qwen2 Image Edit нужен URL исходного изображения: выберите шаблон с изображениями."
            )
        ratio_q = _image_size_qwen2_edit(normalize_aspect_ratio(aspect_ratio, "16:9"))
        fmt = (output_format or "png").strip().lower()
        out_fmt = "jpeg" if fmt in {"jpg", "jpeg"} else "png"
        inp_qwen: dict[str, Any] = {
            "prompt": (prompt or "")[:800],
            "image_url": image_input[0],
            "image_size": ratio_q,
            "output_format": out_fmt,
            "nsfw_checker": False,
        }
        payload = {"model": "qwen2/image-edit", "input": inp_qwen}
    else:
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


def create_grok_image_to_video_task(
    *,
    prompt: str,
    image_urls: list[str] | None = None,
    aspect_ratio: str = "16:9",
    duration_seconds: int = 6,
    nsfw_checker: bool = False,
) -> Tuple[str, str]:
    """Create Grok Imagine image-to-video task. Returns (taskId, model field sent in JSON body)."""
    api_key = _get_api_key()
    dur = max(6, min(30, int(duration_seconds)))
    payload: dict[str, Any] = {
        "model": "grok-imagine/image-to-video",
        "input": {
            "prompt": prompt,
            "mode": "normal",
            "aspect_ratio": aspect_ratio,
            "duration": str(dur),
            "resolution": "720p",
            "nsfw_checker": bool(nsfw_checker),
        },
    }
    if image_urls:
        payload["input"]["image_urls"] = image_urls
    resp = requests.post(
        CREATE_GROK_VIDEO_TASK,
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
    return task_id, str(payload["model"])


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
    if resp.status_code == 200 and code == 200:
        url = (data.get("data") or {}).get("resultUrl", "")
        if url:
            return {"ready": True, "url": url}
        return {"ready": False}

    # Not ready yet / validation-in-progress states should be retried.
    if code in (422, 429):
        return {"ready": False}

    raise RuntimeError(f"Kie.ai API error: {data.get('msg', resp.text)}")


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
