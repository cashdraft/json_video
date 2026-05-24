#!/usr/bin/env python3
"""Validate and export Walter 59½ scene JSON."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "walter_59half_scenes_raw.txt"
OUT_LINES = ROOT / "walter_59half_scenes_fixed.txt"
OUT_JSONL = ROOT / "walter_59half_scenes.jsonl"
REPORT = ROOT / "walter_59half_scenes_report.txt"


def extract_json_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("{"):
            lines.append(s)
            continue
        idx = s.find("{")
        if idx >= 0:
            lines.append(s[idx:].strip())
        else:
            raise ValueError(f"Line without JSON: {s[:80]!r}")
    return lines


def merge_scenes(json_lines: list[str]) -> list[dict]:
    scenes: list[dict] = []
    current: dict | None = None
    for ln in json_lines:
        obj = json.loads(ln)
        if "scene_id" in obj and len(obj) == 1:
            if current:
                scenes.append(current)
            current = {"scene_id": obj["scene_id"]}
            continue
        if current is None:
            raise ValueError(f"Block before scene_id: {ln[:80]}")
        if len(obj) != 1:
            raise ValueError(f"Multi-key line: {ln[:80]}")
        key = next(iter(obj))
        current[key] = obj[key]
    if current:
        scenes.append(current)
    return scenes


def main() -> int:
    raw = RAW.read_text(encoding="utf-8")
    json_lines = extract_json_lines(raw)
    errors: list[str] = []
    for i, ln in enumerate(json_lines, 1):
        try:
            json.loads(ln)
        except json.JSONDecodeError as e:
            errors.append(f"line {i}: {e.msg} @ {e.pos}: {ln[max(0, e.pos-30):e.pos+30]!r}")

    scenes = merge_scenes(json_lines)
    nums = [int(re.search(r"scene_(\d+)", s["scene_id"]).group(1)) for s in scenes]
    gaps = [f"scene_{nums[i-1]} -> scene_{nums[i]}" for i in range(1, len(nums)) if nums[i] != nums[i - 1] + 1]

    required = ("scene_id", "text", "text_ru", "start", "video")
    missing = []
    for s in scenes:
        for k in required:
            if k not in s:
                missing.append(f"{s.get('scene_id')}: missing {k}")

    OUT_LINES.write_text("\n\n".join(
        "\n".join([
            json.dumps({"scene_id": s["scene_id"]}, ensure_ascii=False),
            json.dumps({"text": s["text"]}, ensure_ascii=False),
            json.dumps({"text_ru": s["text_ru"]}, ensure_ascii=False),
            json.dumps({"start": s["start"]}, ensure_ascii=False),
            json.dumps({"video": s["video"]}, ensure_ascii=False),
        ])
        for s in scenes
    ) + "\n", encoding="utf-8")

    OUT_JSONL.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in scenes) + "\n",
        encoding="utf-8",
    )

    report = [
        f"json_lines={len(json_lines)}",
        f"scenes={len(scenes)}",
        f"first={scenes[0]['scene_id']}",
        f"last={scenes[-1]['scene_id']}",
        f"syntax_errors={len(errors)}",
        f"missing_fields={len(missing)}",
        f"gaps={gaps or 'none'}",
    ]
    report.extend(errors[:20])
    report.extend(missing[:20])
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 1 if errors or missing or gaps else 0


if __name__ == "__main__":
    sys.exit(main())
