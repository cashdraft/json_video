"""
Шаблоны ReWrite — одна подпапка на шаблон (как data/image_templates).

  rewrite_templates/
    <имя_шаблона>/          ← имя папки попадает в список в UI (Base Template, my_project, …)
      Config.txt
      Hero Prompt.txt
      Master Prompt.txt
      Analysis Prompt.txt
      …

В корне rewrite_templates лежат только папки шаблонов. Любые .txt прямо в
rewrite_templates/ игнорируются: файлы кладите внутрь нужной подпапки.

Имена файлов внутри шаблона (без учёта регистра, расширение .txt):
  Config — симв./мин и длительность (см. parse_template_config)
  Hero Prompt, Master Prompt
  Analysis … Voiceover Editor Prompt (draft1: «Block Writer Prompt.txt», continuity_editor: «Сontinuity Editor Prompt.txt», retention_editor: «Retention Editor Prompt.txt», hook_editor: «Hook Editor Prompt.txt», flow_editor: «Flow Editor Prompt.txt», persona_editor: «Persona Editor Prompt.txt», voiceover_editor: «Voiceover Editor Prompt.txt»)
"""

from __future__ import annotations

import re
from pathlib import Path

from typing import Any

from image_templates import safe_template_dir
from rewrite_pipeline import REWRITE_STAGE_KEYS

MODULE_DIR = Path(__file__).resolve().parent
REWRITE_TEMPLATES_DIR = MODULE_DIR / "rewrite_templates"

# Нормализованный stem (lower, схлопнутые пробелы) → куда кладём содержимое
_STEM_TO_TARGET: dict[str, str] = {
    "config": "template_config",
    "hero prompt": "hero_prompt",
    "master prompt": "master_prompt",
    "analysis prompt": "stage:analysis",
    "analysis system promt": "stage:analysis",
    "analysis system prompt": "stage:analysis",
    "analysis user promt": "stage_user:analysis",
    "analysis user prompt": "stage_user:analysis",
    "structure prompt": "stage:structure",
    "structure system promt": "stage:structure",
    "structure system prompt": "stage:structure",
    "architect prompt": "stage:structure",
    "architect system promt": "stage:structure",
    "architect system prompt": "stage:structure",
    "architect user promt": "stage_user:structure",
    "architect user prompt": "stage_user:structure",
    "draft1 prompt": "stage:draft1",
    "draft1 rewriter prompt": "stage:draft1",
    "draft1 rewriter system promt": "stage:draft1",
    "draft1 rewriter system prompt": "stage:draft1",
    "draft1 rewriter user promt": "stage_user:draft1",
    "draft1 rewriter user prompt": "stage_user:draft1",
    "block writer prompt": "stage:draft1",
    "block writer system promt": "stage:draft1",
    "block writer system prompt": "stage:draft1",
    "block writer user promt": "stage_user:draft1",
    "block writer user prompt": "stage_user:draft1",
    "continuity editor prompt": "stage:continuity_editor",
    "continuity editor system promt": "stage:continuity_editor",
    "continuity editor system prompt": "stage:continuity_editor",
    "continuity editor user promt": "stage_user:continuity_editor",
    "continuity editor user prompt": "stage_user:continuity_editor",
    "retention editor prompt": "stage:retention_editor",
    "retention editor system promt": "stage:retention_editor",
    "retention editor system prompt": "stage:retention_editor",
    "retention editor user promt": "stage_user:retention_editor",
    "retention editor user prompt": "stage_user:retention_editor",
    "hook editor prompt": "stage:hook_editor",
    "hook editor system promt": "stage:hook_editor",
    "hook editor system prompt": "stage:hook_editor",
    "hook editor user promt": "stage_user:hook_editor",
    "hook editor user prompt": "stage_user:hook_editor",
    "flow editor prompt": "stage:flow_editor",
    "flow editor system promt": "stage:flow_editor",
    "flow editor system prompt": "stage:flow_editor",
    "flow editor user promt": "stage_user:flow_editor",
    "flow editor user prompt": "stage_user:flow_editor",
    "persona editor prompt": "stage:persona_editor",
    "persona editor system promt": "stage:persona_editor",
    "persona editor system prompt": "stage:persona_editor",
    "persona editor user promt": "stage_user:persona_editor",
    "persona editor user prompt": "stage_user:persona_editor",
    "voiceover editor prompt": "stage:voiceover_editor",
    "voiceover editor system promt": "stage:voiceover_editor",
    "voiceover editor system prompt": "stage:voiceover_editor",
    "voiceover editor user promt": "stage_user:voiceover_editor",
    "voiceover editor user prompt": "stage_user:voiceover_editor",
}

# Обратно к имени файла при записи на диск (как при чтении).
_TARGET_TO_FILENAME: dict[str, str] = {
    "template_config": "Config.txt",
    "hero_prompt": "Hero Prompt.txt",
    "master_prompt": "Master Prompt.txt",
    "stage:analysis": "Analysis System Promt.txt",
    "stage_user:analysis": "Analysis User Promt.txt",
    "stage:structure": "Architect System Promt.txt",
    "stage_user:structure": "Architect User Promt.txt",
    "stage:draft1": "Block Writer System Promt.txt",
    "stage_user:draft1": "Block Writer User Promt.txt",
    "stage:continuity_editor": "Сontinuity Editor System Promt.txt",
    "stage_user:continuity_editor": "Сontinuity Editor User Promt.txt",
    "stage:retention_editor": "Retention Editor System Promt.txt",
    "stage_user:retention_editor": "Retention Editor User Promt.txt",
    "stage:hook_editor": "Hook Editor System Promt.txt",
    "stage_user:hook_editor": "Hook Editor User Promt.txt",
    "stage:flow_editor": "Flow Editor System Promt.txt",
    "stage_user:flow_editor": "Flow Editor User Promt.txt",
    "stage:persona_editor": "Persona Editor System Promt.txt",
    "stage_user:persona_editor": "Persona Editor User Promt.txt",
    "stage:voiceover_editor": "Voiceover Editor System Promt.txt",
    "stage_user:voiceover_editor": "Voiceover Editor User Promt.txt",
}

_STAGE_TARGETS: dict[str, str] = {
    "analysis": "Analysis",
    "structure": "Architect",
    "draft1": "Block Writer",
    "continuity_editor": "Сontinuity Editor",
    "retention_editor": "Retention Editor",
    "hook_editor": "Hook Editor",
    "flow_editor": "Flow Editor",
    "persona_editor": "Persona Editor",
    "voiceover_editor": "Voiceover Editor",
}


def _norm_stem(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"\s+", " ", s.lower())
    return s


def parse_chars_per_minute(text: str, default: int = 344) -> int:
    """Из текста извлекает символов в минуту (например «344» или «1 min = 344»)."""
    t = (text or "").strip()
    if not t:
        return default
    eq = re.search(r"=\s*(\d{1,5})", t)
    if eq:
        return _clamp_cpm(int(eq.group(1)), default)
    nums = [int(x) for x in re.findall(r"\d{1,5}", t)]
    if len(nums) == 1:
        return _clamp_cpm(nums[0], default)
    if nums:
        reasonable = [n for n in nums if 50 <= n <= 2000]
        if reasonable:
            return reasonable[-1]
        return _clamp_cpm(max(nums), default)
    return default


def _clamp_cpm(n: int, default: int) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return default
    if v < 1:
        return default
    return min(v, 2000)


def parse_duration_minutes(text: str, default: int = 5) -> int:
    """Минуты озвучки 1–30 («13», «15 min»)."""
    t = (text or "").strip()
    if not t:
        return max(1, min(30, default))
    m = re.search(r"(\d{1,2})", t)
    if not m:
        return max(1, min(30, default))
    try:
        v = int(m.group(1))
    except ValueError:
        return max(1, min(30, default))
    return max(1, min(30, v))


def parse_template_config(raw: str) -> dict[str, int]:
    """
    Один файл Config.txt: числовые настройки.

    Строки вида key: value или key = value (ключ без учёта регистра):
      chars_per_minute, cpm, characters per minute, voice ratio
      duration_minutes, duration, target duration, длительность, minutes

    Если ни одной пары ключ/значение нет: две строки только из цифр — первая симв./мин, вторая минуты;
    одна строка — только симв./мин (как раньше в отдельном файле).
    """
    result: dict[str, int] = {}
    lines: list[str] = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)

    for line in lines:
        if ":" not in line and "=" not in line:
            continue
        sep = ":" if ":" in line else "="
        k, v = line.split(sep, 1)
        kn = _norm_stem(k.replace("_", " "))
        val = v.strip()
        if kn in ("chars per minute", "cpm", "characters per minute", "voice ratio"):
            result["chars_per_minute"] = parse_chars_per_minute(val)
        elif kn in ("duration minutes", "duration", "target duration", "длительность", "minutes"):
            result["duration_minutes"] = parse_duration_minutes(val)

    plain = [ln for ln in lines if ":" not in ln and "=" not in ln]
    if not result and len(plain) >= 2:
        if re.fullmatch(r"\d+", plain[0]) and re.fullmatch(r"\d+", plain[1]):
            result["chars_per_minute"] = parse_chars_per_minute(plain[0])
            result["duration_minutes"] = parse_duration_minutes(plain[1])
    elif not result and len(plain) == 1:
        result["chars_per_minute"] = parse_chars_per_minute(plain[0])

    return result


def list_rewrite_template_names() -> list[str]:
    root = REWRITE_TEMPLATES_DIR
    if not root.is_dir():
        return []
    names = [
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in ("__pycache__",)
    ]
    return sorted(names, key=str.lower)


def load_rewrite_template(name: str) -> dict | None:
    """
    Читает шаблон с диска. Возвращает dict или None.
    Ключи: name, hero_prompt, chars_per_minute, master_prompt, stages.
    Из Config.txt при наличии: chars_per_minute, duration_minutes.
    """
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return None
    out: dict = {
        "name": name,
        "hero_prompt": "",
        "chars_per_minute": 344,
        "master_prompt": "",
        "stages": {k: {"prompt": "", "user_prompt": ""} for k in REWRITE_STAGE_KEYS},
    }
    config_raw: str | None = None
    for f in sorted(d.glob("*.txt"), key=lambda p: p.name.lower()):
        if not f.is_file():
            continue
        stem = _norm_stem(f.stem)
        target = _STEM_TO_TARGET.get(stem)
        if not target:
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if target == "template_config":
            config_raw = raw
            continue
        if target == "hero_prompt":
            out["hero_prompt"] = raw.strip()
        elif target == "master_prompt":
            out["master_prompt"] = raw.strip()
        elif target.startswith("stage:"):
            sk = target.split(":", 1)[1]
            if sk in REWRITE_STAGE_KEYS:
                out["stages"][sk]["prompt"] = raw.strip()
        elif target.startswith("stage_user:"):
            sk = target.split(":", 1)[1]
            if sk in REWRITE_STAGE_KEYS:
                out["stages"][sk]["user_prompt"] = raw.strip()

    if config_raw is not None:
        cfg = parse_template_config(config_raw)
        if "chars_per_minute" in cfg:
            out["chars_per_minute"] = cfg["chars_per_minute"]
        if "duration_minutes" in cfg:
            out["duration_minutes"] = cfg["duration_minutes"]

    return out


def save_rewrite_template_to_disk(
    name: str,
    *,
    hero_prompt: str,
    master_prompt: str,
    chars_per_minute: int,
    duration_minutes: int,
    stages: dict[str, Any],
) -> tuple[bool, str]:
    """
    Перезаписывает .txt в подпапке шаблона. Папка должна уже существовать.
    Возвращает (True, "") или (False, код_ошибки).
    """
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return False, "not_found"
    try:
        cpm = max(1, min(2000, int(chars_per_minute)))
        dm = max(1, min(30, int(duration_minutes)))
    except (TypeError, ValueError):
        cpm, dm = 344, 5
    cfg_text = f"chars_per_minute: {cpm}\nduration_minutes: {dm}\n"
    (d / _TARGET_TO_FILENAME["template_config"]).write_text(cfg_text, encoding="utf-8")
    (d / _TARGET_TO_FILENAME["hero_prompt"]).write_text(
        (hero_prompt or "").rstrip() + "\n", encoding="utf-8"
    )
    (d / _TARGET_TO_FILENAME["master_prompt"]).write_text(
        (master_prompt or "").rstrip() + "\n", encoding="utf-8"
    )
    for sk in REWRITE_STAGE_KEYS:
        cell = stages.get(sk) if isinstance(stages, dict) else None
        prompt = ""
        user_prompt = ""
        if isinstance(cell, dict):
            prompt = str(cell.get("prompt") or "")
            user_prompt = str(cell.get("user_prompt") or "")

        # New naming: explicit System Promt / User Promt files.
        fn = _TARGET_TO_FILENAME.get(f"stage:{sk}")
        if fn:
            (d / fn).write_text(prompt.rstrip() + "\n", encoding="utf-8")
        ufn = _TARGET_TO_FILENAME.get(f"stage_user:{sk}")
        if ufn:
            (d / ufn).write_text(user_prompt.rstrip() + "\n", encoding="utf-8")

        # Backward-compatible legacy file names.
        legacy_title = _STAGE_TARGETS.get(sk, sk.capitalize())
        legacy_system_fn = f"{legacy_title} Prompt.txt"
        (d / legacy_system_fn).write_text(prompt.rstrip() + "\n", encoding="utf-8")
    return True, ""
