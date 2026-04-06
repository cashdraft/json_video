"""
Шаблоны ReWrite — одна подпапка на шаблон (как data/image_templates).

  rewrite_templates/
    <имя_шаблона>/          ← имя папки попадает в список в UI (baseline, my_project, …)
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
  Analysis … Final Prompt
"""

from __future__ import annotations

import re
from pathlib import Path

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
    "structure prompt": "stage:structure",
    "draft1 prompt": "stage:draft1",
    "draft2 prompt": "stage:draft2",
    "draft3 prompt": "stage:draft3",
    "final prompt": "stage:final",
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
        "stages": {k: {"prompt": ""} for k in REWRITE_STAGE_KEYS},
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

    if config_raw is not None:
        cfg = parse_template_config(config_raw)
        if "chars_per_minute" in cfg:
            out["chars_per_minute"] = cfg["chars_per_minute"]
        if "duration_minutes" in cfg:
            out["duration_minutes"] = cfg["duration_minutes"]

    return out
