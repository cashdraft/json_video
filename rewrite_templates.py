"""
Шаблоны ReWrite — одна подпапка на шаблон (как image_templates/).

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
  Config — целевой объём в символах 500–40 000, шаг 500 (см. parse_template_config; старые chars/duration тоже читаются)
  Hero Prompt, Master Prompt
  Analysis … Scene Writer Prompt (draft1: «Block Writer Prompt.txt», retention_editor: «Retention Editor Prompt.txt», hook_editor: «Hook Editor Prompt.txt», persona_editor: «Persona Editor Prompt.txt», voiceover_editor: «Voiceover Editor Prompt.txt», title_strategist: «Title Strategist …», structure_splitter: «Structure Splitter Prompt.txt», scene_writer: «Scene Writer Prompt.txt», youtube_packaging: «YouTube packaging …»)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from image_templates import (
    is_logo_file,
    remove_logo_files,
    safe_template_dir,
    save_logo_file,
    validate_template_name,
)
from rewrite_pipeline import REWRITE_STAGE_KEYS, clamp_target_chars

META_FILENAME = "template_meta.json"
DEFAULT_REWRITE_TEMPLATE_NAME = "Template"

# Поля, которые входят в редактор шаблона (остальное — настройки проекта, не шаблона).
REWRITE_TEMPLATE_SCOPE_STAGE_KEYS: tuple[str, ...] = ("rewrite", "scene_writer")


def filter_stages_for_template_scope(stages: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    stages = stages if isinstance(stages, dict) else {}
    out: dict[str, dict[str, str]] = {}
    for sk in REWRITE_TEMPLATE_SCOPE_STAGE_KEYS:
        cell = stages.get(sk) if isinstance(stages.get(sk), dict) else {}
        out[sk] = {
            "prompt": str(cell.get("prompt") or ""),
            "user_prompt": str(cell.get("user_prompt") or ""),
            "style_prompt": str(cell.get("style_prompt") or ""),
            "past_prompt": str(cell.get("past_prompt") or ""),
        }
    return out


def resolve_rewrite_template_name(name: str | None) -> str:
    n = str(name or "").strip()
    return n if n else DEFAULT_REWRITE_TEMPLATE_NAME


def allocate_rewrite_template_name(
    name: str | None,
    *,
    exclude: str | None = None,
) -> str:
    """Имя папки шаблона: пустое → «Template»; если занято — Template 1, Template 2, …"""
    desired = resolve_rewrite_template_name(name)
    known = set(list_rewrite_template_names())
    ex = str(exclude or "").strip()
    if ex:
        known.discard(ex)
    if desired not in known:
        return desired
    if desired != DEFAULT_REWRITE_TEMPLATE_NAME:
        return desired
    n = 1
    while n < 10000:
        candidate = f"Template {n}"
        if candidate not in known:
            return candidate
        n += 1
    return f"Template {n}"

DEFAULT_TTS_DEFAULTS: dict[str, Any] = {
    "voice_id": "",
    "voice_name": "",
    "model_id": "eleven_multilingual_v2",
    "stability_pct": 50,
    "similarity_pct": 75,
    "style_pct": 0,
    "speed_pct": 20,
    "use_speaker_boost": True,
}

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
    "rewrite prompt": "stage:rewrite",
    "rewrite system promt": "stage:rewrite",
    "rewrite system prompt": "stage:rewrite",
    "rewrite user promt": "stage_user:rewrite",
    "rewrite user prompt": "stage_user:rewrite",
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
    "title strategist prompt": "stage:title_strategist",
    "title strategist system promt": "stage:title_strategist",
    "title strategist system prompt": "stage:title_strategist",
    "title strategist user promt": "stage_user:title_strategist",
    "title strategist user prompt": "stage_user:title_strategist",
    # Совместимость: старые имена файлов Voice Flow Editor 2 → тот же этап title_strategist.
    "voice flow editor 2 prompt": "stage:title_strategist",
    "voice flow editor 2 system promt": "stage:title_strategist",
    "voice flow editor 2 system prompt": "stage:title_strategist",
    "voice flow editor 2 user promt": "stage_user:title_strategist",
    "voice flow editor 2 user prompt": "stage_user:title_strategist",
    "structure splitter prompt": "stage:structure_splitter",
    "structure splitter system promt": "stage:structure_splitter",
    "structure splitter system prompt": "stage:structure_splitter",
    "structure splitter user promt": "stage_user:structure_splitter",
    "structure splitter user prompt": "stage_user:structure_splitter",
    "scene writer prompt": "stage:scene_writer",
    "scene writer system promt": "stage:scene_writer",
    "scene writer system prompt": "stage:scene_writer",
    "scene writer user promt": "stage_user:scene_writer",
    "scene writer user prompt": "stage_user:scene_writer",
    "scene writer style promt": "stage_style:scene_writer",
    "scene writer style prompt": "stage_style:scene_writer",
    "scene writer past promt": "stage_past:scene_writer",
    "scene writer past prompt": "stage_past:scene_writer",
    "youtube packaging prompt": "stage:youtube_packaging",
    "youtube packaging engine prompt": "stage:youtube_packaging",
    "youtube packaging system promt": "stage:youtube_packaging",
    "youtube packaging system prompt": "stage:youtube_packaging",
    "youtube packaging user promt": "stage_user:youtube_packaging",
    "youtube packaging user prompt": "stage_user:youtube_packaging",
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
    "stage:rewrite": "Rewrite System Promt.txt",
    "stage_user:rewrite": "Rewrite User Promt.txt",
    "stage:retention_editor": "Retention Editor System Promt.txt",
    "stage_user:retention_editor": "Retention Editor User Promt.txt",
    "stage:hook_editor": "Hook Editor System Promt.txt",
    "stage_user:hook_editor": "Hook Editor User Promt.txt",
    "stage:persona_editor": "Persona Editor System Promt.txt",
    "stage_user:persona_editor": "Persona Editor User Promt.txt",
    "stage:voiceover_editor": "Voiceover Editor System Promt.txt",
    "stage_user:voiceover_editor": "Voiceover Editor User Promt.txt",
    "stage:title_strategist": "Title Strategist System Promt.txt",
    "stage_user:title_strategist": "Title Strategist User Promt.txt",
    "stage:structure_splitter": "Structure Splitter System Promt.txt",
    "stage_user:structure_splitter": "Structure Splitter User Promt.txt",
    "stage:scene_writer": "Scene Writer System Promt.txt",
    "stage_user:scene_writer": "Scene Writer User Promt.txt",
    "stage_style:scene_writer": "Scene Writer Style Promt.txt",
    "stage_past:scene_writer": "Scene Writer Past in Promt.txt",
    "stage:youtube_packaging": "YouTube packaging engine System Promt.txt",
    "stage_user:youtube_packaging": "YouTube packaging engine User Promt.txt",
}

_STAGE_TARGETS: dict[str, str] = {
    "analysis": "Analysis",
    "structure": "Architect",
    "draft1": "Block Writer",
    "rewrite": "Rewrite",
    "retention_editor": "Retention Editor",
    "hook_editor": "Hook Editor",
    "persona_editor": "Persona Editor",
    "voiceover_editor": "Voiceover Editor",
    "title_strategist": "Title Strategist",
    "structure_splitter": "Structure Splitter",
    "scene_writer": "Scene Writer",
    "youtube_packaging": "YouTube packaging engine",
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


def parse_target_chars(text: str, default: int = 1500) -> int:
    """500–40 000 симв., шаг 500 (из Config.txt)."""
    t = (text or "").strip()
    if not t:
        return default
    eq = re.search(r"=\s*(\d{1,6})", t)
    if eq:
        return clamp_target_chars(int(eq.group(1)))
    nums = [int(x) for x in re.findall(r"\d{1,6}", t)]
    if not nums:
        return default
    return clamp_target_chars(nums[0])


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
      target_chars, target characters, целевой объём (символы)
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
        if kn in ("target chars", "target characters", "target length", "длина текста"):
            result["target_chars"] = parse_target_chars(val)
        elif kn in ("chars per minute", "cpm", "characters per minute", "voice ratio"):
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


def find_logo_file(template_dir: Path) -> Path | None:
    for f in template_dir.iterdir():
        if f.is_file() and is_logo_file(f):
            return f
    return None


def load_template_meta(template_dir: Path) -> dict[str, Any]:
    """description + tts_defaults из template_meta.json."""
    out: dict[str, Any] = {
        "description": "",
        "tts_defaults": dict(DEFAULT_TTS_DEFAULTS),
    }
    p = template_dir / META_FILENAME
    if not p.is_file():
        return out
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return out
    if not isinstance(raw, dict):
        return out
    out["description"] = str(raw.get("description") or "")
    tts = raw.get("tts_defaults")
    if isinstance(tts, dict):
        merged = dict(DEFAULT_TTS_DEFAULTS)
        for k in DEFAULT_TTS_DEFAULTS:
            if k in tts:
                merged[k] = tts[k]
        out["tts_defaults"] = merged
    return out


def save_template_meta(
    template_dir: Path,
    *,
    description: str,
    tts_defaults: dict[str, Any] | None,
) -> None:
    tts_in: dict[str, Any] = dict(DEFAULT_TTS_DEFAULTS)
    if isinstance(tts_defaults, dict):
        for k in DEFAULT_TTS_DEFAULTS:
            if k in tts_defaults:
                tts_in[k] = tts_defaults[k]
    try:
        tts_in["stability_pct"] = max(0, min(100, int(tts_in.get("stability_pct", 50))))
        tts_in["similarity_pct"] = max(0, min(100, int(tts_in.get("similarity_pct", 75))))
        tts_in["style_pct"] = max(0, min(100, int(tts_in.get("style_pct", 0))))
        tts_in["speed_pct"] = max(0, min(100, int(tts_in.get("speed_pct", 20))))
    except (TypeError, ValueError):
        pass
    raw_boost = tts_in.get("use_speaker_boost", True)
    if isinstance(raw_boost, str):
        tts_in["use_speaker_boost"] = raw_boost.lower() in ("true", "1", "yes", "on")
    else:
        tts_in["use_speaker_boost"] = bool(raw_boost)
    tts_in["voice_id"] = str(tts_in.get("voice_id") or "").strip()
    tts_in["voice_name"] = str(tts_in.get("voice_name") or "").strip()
    tts_in["model_id"] = str(tts_in.get("model_id") or DEFAULT_TTS_DEFAULTS["model_id"]).strip()
    payload = {
        "description": str(description or "").strip(),
        "tts_defaults": tts_in,
    }
    (template_dir / META_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def rename_rewrite_template_dir(old_name: str, new_name: str) -> str | None:
    err = validate_template_name(new_name)
    if err:
        return err
    old_d = safe_template_dir(REWRITE_TEMPLATES_DIR, old_name)
    if not old_d:
        return "Шаблон не найден."
    new_stem = new_name.strip()
    new_d = REWRITE_TEMPLATES_DIR / new_stem
    if new_d.exists() and new_d.resolve() != old_d.resolve():
        return "Шаблон с таким именем уже существует."
    try:
        old_d.rename(new_d)
    except OSError:
        return "Не удалось переименовать шаблон."
    return None


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
    Ключи: name, hero_prompt, target_chars, master_prompt, stages.
    Из Config.txt при наличии: target_chars; иначе (legacy) chars_per_minute, duration_minutes.
    """
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return None
    out: dict = {
        "name": name,
        "hero_prompt": "",
        "target_chars": 1500,
        "chars_per_minute": 344,
        "master_prompt": "",
        "stages": {
            k: {"prompt": "", "user_prompt": "", "style_prompt": "", "past_prompt": ""}
            for k in REWRITE_STAGE_KEYS
        },
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
        elif target.startswith("stage_style:"):
            sk = target.split(":", 1)[1]
            if sk in REWRITE_STAGE_KEYS:
                out["stages"][sk]["style_prompt"] = raw.strip()
        elif target.startswith("stage_past:"):
            sk = target.split(":", 1)[1]
            if sk in REWRITE_STAGE_KEYS:
                out["stages"][sk]["past_prompt"] = raw.strip()

    if config_raw is not None:
        cfg = parse_template_config(config_raw)
        if "target_chars" in cfg:
            out["target_chars"] = clamp_target_chars(int(cfg["target_chars"]))
        else:
            if "chars_per_minute" in cfg:
                out["chars_per_minute"] = int(cfg["chars_per_minute"])
            if "duration_minutes" in cfg:
                out["duration_minutes"] = int(cfg["duration_minutes"])
            try:
                dm = max(1, min(30, int(out.get("duration_minutes", 5))))
            except (TypeError, ValueError):
                dm = 5
            try:
                cpm = max(1, min(2000, int(out.get("chars_per_minute", 344))))
            except (TypeError, ValueError):
                cpm = 344
            out["target_chars"] = clamp_target_chars(dm * cpm)

    meta = load_template_meta(d)
    out["description"] = meta["description"]
    out["tts_defaults"] = meta["tts_defaults"]
    logo = find_logo_file(d)
    out["logo_file"] = logo.name if logo else None

    return out


def save_rewrite_template_to_disk(
    name: str,
    *,
    hero_prompt: str,
    master_prompt: str,
    target_chars: int | None = None,
    stages: dict[str, Any],
    description: str | None = None,
    tts_defaults: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Перезаписывает .txt в подпапке шаблона. Папка должна уже существовать.
    В шаблон пишутся только hero/master, этапы из REWRITE_TEMPLATE_SCOPE_STAGE_KEYS,
    meta (description, tts_defaults). target_chars (Duration) в шаблон не входит.
    Возвращает (True, "") или (False, код_ошибки).
    """
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return False, "not_found"
    if target_chars is not None:
        try:
            tc = clamp_target_chars(int(target_chars))
        except (TypeError, ValueError):
            tc = 1500
        cfg_text = f"target_chars: {tc}\n"
        (d / _TARGET_TO_FILENAME["template_config"]).write_text(cfg_text, encoding="utf-8")
    (d / _TARGET_TO_FILENAME["hero_prompt"]).write_text(
        (hero_prompt or "").rstrip() + "\n", encoding="utf-8"
    )
    (d / _TARGET_TO_FILENAME["master_prompt"]).write_text(
        (master_prompt or "").rstrip() + "\n", encoding="utf-8"
    )
    scoped_stages = filter_stages_for_template_scope(stages)
    for sk in REWRITE_TEMPLATE_SCOPE_STAGE_KEYS:
        cell = scoped_stages.get(sk) if isinstance(scoped_stages.get(sk), dict) else {}
        prompt = str(cell.get("prompt") or "")
        user_prompt = str(cell.get("user_prompt") or "")
        style_prompt = str(cell.get("style_prompt") or "")
        past_prompt = str(cell.get("past_prompt") or "")

        # New naming: explicit System Promt / User Promt files.
        fn = _TARGET_TO_FILENAME.get(f"stage:{sk}")
        if fn:
            (d / fn).write_text(prompt.rstrip() + "\n", encoding="utf-8")
        ufn = _TARGET_TO_FILENAME.get(f"stage_user:{sk}")
        if ufn:
            (d / ufn).write_text(user_prompt.rstrip() + "\n", encoding="utf-8")
        sfn = _TARGET_TO_FILENAME.get(f"stage_style:{sk}")
        if sfn:
            (d / sfn).write_text(style_prompt.rstrip() + "\n", encoding="utf-8")
        pfn = _TARGET_TO_FILENAME.get(f"stage_past:{sk}")
        if pfn:
            (d / pfn).write_text(past_prompt.rstrip() + "\n", encoding="utf-8")

        # Backward-compatible legacy file names.
        legacy_title = _STAGE_TARGETS.get(sk, sk.capitalize())
        legacy_system_fn = f"{legacy_title} Prompt.txt"
        (d / legacy_system_fn).write_text(prompt.rstrip() + "\n", encoding="utf-8")
    if description is not None or tts_defaults is not None:
        cur_meta = load_template_meta(d)
        save_template_meta(
            d,
            description=description if description is not None else cur_meta["description"],
            tts_defaults=tts_defaults if tts_defaults is not None else cur_meta["tts_defaults"],
        )
    return True, ""


def save_rewrite_template_logo(name: str, data: bytes, ext: str = ".png") -> tuple[bool, str]:
    d = safe_template_dir(REWRITE_TEMPLATES_DIR, name)
    if d is None:
        return False, "not_found"
    if not data:
        return False, "empty"
    try:
        save_logo_file(d, data, ext)
    except OSError:
        return False, "write_failed"
    return True, ""
