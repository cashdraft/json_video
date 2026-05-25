#!/usr/bin/env python3
"""CLI client: frame.jpg + dino_response.json → POST /segment_dino."""

from __future__ import annotations

import json
import mimetypes
import sys
from pathlib import Path
from urllib import error, request

DEFAULT_URL = "http://127.0.0.1:8011/segment_dino"


def _multipart_body(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[bytes, str]:
    boundary = "----sam2-dino-test-client"
    lines: list[bytes] = []

    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(value.encode())
        lines.append(b"\r\n")

    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    lines.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    lines.append(file_bytes)
    lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())

    body = b"".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python test_client_dino_to_sam2.py frame.jpg dino_response.json [url]")
        return 1

    image_path = Path(sys.argv[1])
    json_path = Path(sys.argv[2])
    url = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_URL

    if not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 1
    if not json_path.is_file():
        print(f"JSON not found: {json_path}", file=sys.stderr)
        return 1

    detections_json = json_path.read_text(encoding="utf-8")
    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    file_bytes = image_path.read_bytes()
    body, content_type_header = _multipart_body(
        {
            "detections_json": detections_json,
            "min_score": "0.35",
        },
        "image",
        image_path.name,
        file_bytes,
        content_type,
    )

    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type_header)

    try:
        with request.urlopen(req, timeout=1800) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        status = exc.code
        text = exc.read().decode("utf-8", errors="replace")
    except error.URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        print(text)
        return 1

    out_json = Path("sam2_segment_response.json")
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved response: {out_json.resolve()}")

    preview = None
    merged = payload.get("merged_occupied") or {}
    if isinstance(merged, dict):
        preview = merged.get("preview_path")
    if preview:
        print(f"Preview path (on server): {preview}")

    items = payload.get("items") or []
    mask_paths = [it.get("mask_path") for it in items if isinstance(it, dict) and it.get("mask_path")]
    print(f"Mask files: {len(mask_paths)}")
    for p in mask_paths[:5]:
        print(f"  - {p}")
    if len(mask_paths) > 5:
        print(f"  … and {len(mask_paths) - 5} more")

    if not mask_paths and status < 400:
        print("WARNING: no mask files in response", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
