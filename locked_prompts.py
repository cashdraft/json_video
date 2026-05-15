"""Хранилище промтов «под пин-кодом».

Идея простая: часть промтов мы хотим хранить ВНЕ git-репозитория шаблонов
и редактировать только из UI после ввода числового пин-кода. Это не
криптографическая защита, а защита «от дурака»: чтобы случайно не
переписать важный системный промт.

Файлы лежат в каталоге `locked_prompts/` рядом с `app.py`. Каждому
промту соответствует ровно один `*.txt` файл. Реестр промтов ниже.

API модуля:
- `list_locked_prompts()` — словарь {name: метаданные}.
- `get_locked_prompt(name)` — текущий текст (с дефолтом, если файла нет).
- `is_locked_prompt_present(name)` — bool «файл существует и непустой».
- `save_locked_prompt(name, content)` — записать (без проверки пина).
- `verify_pin(pin)` — сверить ввод с переменной окружения.

PIN живёт в env `LOCKED_PROMPTS_PIN`. Если переменная не задана —
используется дефолт `1234` (это намеренно: барьер от случайной правки,
а не защита от злоумышленника, см. требование пользователя).
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCKED_PROMPTS_DIR = BASE_DIR / "locked_prompts"

LOCKED_PROMPTS_PIN_ENV = "LOCKED_PROMPTS_PIN"
LOCKED_PROMPTS_PIN_DEFAULT = "1234"

# Реестр известных промтов. Дефолтное значение используется, если файл
# на диске отсутствует — например, при первом запуске после деплоя.
LOCKED_PROMPTS: dict[str, dict] = {
    "translate_to_ru": {
        "label": "Промт перевода на русский",
        "filename": "translate_to_ru.txt",
        "default": (
            "Ты — профессиональный переводчик и редактор русского языка.\n\n"
            "Твоя задача — перевести входной текст на русский язык максимально "
            "естественно, понятно и живо.\n\n"
            "КРИТИЧЕСКИЕ ПРАВИЛА:\n\n"
            "— Сохраняй исходный смысл на 100%\n"
            "— Не сокращай текст\n"
            "— Не добавляй новую информацию\n"
            "— Не меняй факты, цифры, даты и имена\n"
            "— Не упрощай смысл\n"
            "— Не делай пересказ\n"
            "— Не цензурируй эмоциональность автора\n\n"
            "ФОРМАТ ОТВЕТА:\n\n"
            "Верни ТОЛЬКО готовый перевод на русском языке.\n"
            "Без комментариев.\n"
            "Без пояснений.\n"
            "Без оригинального текста."
        ),
    },
    "semantic_text_analyzer_system": {
        "label": "Semantic Text Analyzer — System Promt",
        "filename": "semantic_text_analyzer_system.txt",
        "default": (
            "Ты — Semantic Text Analyzer. Проводишь смысловой разбор русского "
            "текста для последующего сценарного использования.\n\n"
            "Стиль ответа:\n"
            "— На русском языке.\n"
            "— Структурированно, без воды.\n"
            "— Без вступлений и заключений.\n"
            "— Без оригинального текста в ответе."
        ),
    },
    "semantic_text_analyzer_user": {
        "label": "Semantic Text Analyzer — User Promt",
        "filename": "semantic_text_analyzer_user.txt",
        "default": (
            "Проанализируй текст ниже: основная идея, ключевые тезисы, "
            "эмоциональная подача, целевая аудитория, явные тематические "
            "блоки.\n\n"
            "ТЕКСТ ДЛЯ АНАЛИЗА:\n"
        ),
    },
    "system_prompt_voiceover_editor": {
        "label": "Voiceover Editor — System Promt",
        "filename": "system_prompt_voiceover_editor.txt",
        "default": "",
    },
    "voiceover_editor_system_rules": {
        "label": "Voiceover Editor — System Rules",
        "filename": "voiceover_editor_system_rules.txt",
        "default": "",
    },
    "system_prompt_title_strategist": {
        "label": "Title Strategist — System Promt",
        "filename": "system_prompt_title_strategist.txt",
        "default": "",
    },
    "rewrite_system_rules": {
        "label": "Rewrite — System Rules",
        "filename": "rewrite_system_rules.txt",
        "default": (
            "Дополнительные правила для этапа Rewrite (дополняют «Rewrite System Promt» в UI).\n\n"
            "— Не выдумывай факты: имена, цифры и утверждения только из исходного текста.\n"
            "— Меняй формулировки и примеры, сохраняй логику, структуру и факты оригинала.\n"
            "— Стиль: разговорный монолог для YouTube, короткие фразы, без канцелярита.\n"
            "— Ответ: только готовый сценарий, без вступлений и комментариев.\n\n"
            "Плейсхолдеры (подставляются при запуске): "
            "{{LANGUAGE}}, {{DURATION}}, {{ORIGINAL_TITLE}}, {{MASTER_PROMT}}, {{HERO_PROMT}}."
        ),
    },
}

# User Promt по этапам ReWrite — отдельный файл на этап (редактирование только по пин-коду).
_USER_PROMPT_STAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("analysis", "Analysis"),
    ("structure", "Architect"),
    ("draft1", "Block Writer"),
    ("rewrite", "Rewrite"),
    ("retention_editor", "Retention Editor"),
    ("hook_editor", "Hook Editor"),
    ("flow_editor", "Flow Editor"),
    ("persona_editor", "Persona Editor"),
    ("voiceover_editor", "Voiceover Editor"),
    ("title_strategist", "Title Strategist"),
    ("structure_splitter", "Structure Splitter"),
    ("scene_writer", "Scene Writer"),
    ("scene_writer_live", "Scene Writer Live"),
    ("youtube_packaging", "YouTube packaging engine"),
)
for _sk, _lbl in _USER_PROMPT_STAGE_LABELS:
    _nm = f"user_prompt_{_sk}"
    LOCKED_PROMPTS[_nm] = {
        "label": f"{_lbl} — User Promt",
        "filename": f"user_prompt_{_sk}.txt",
        "default": "",
    }


def list_locked_prompts() -> dict[str, dict]:
    """Снимок реестра без поля `default` (только метаданные)."""
    out: dict[str, dict] = {}
    for name, meta in LOCKED_PROMPTS.items():
        out[name] = {
            "label": str(meta.get("label") or name),
            "filename": str(meta.get("filename") or f"{name}.txt"),
        }
    return out


def _prompt_path(name: str) -> Path:
    meta = LOCKED_PROMPTS.get(name)
    if not meta:
        raise KeyError(name)
    filename = str(meta.get("filename") or f"{name}.txt")
    return LOCKED_PROMPTS_DIR / filename


def is_known_prompt(name: str) -> bool:
    return name in LOCKED_PROMPTS


def is_locked_prompt_present(name: str) -> bool:
    """True если файл существует и не пустой (после strip)."""
    try:
        p = _prompt_path(name)
    except KeyError:
        return False
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(text.strip())


def get_locked_prompt(name: str) -> str:
    """Вернуть текущий текст. Если файла нет — отдать `default` из реестра."""
    meta = LOCKED_PROMPTS.get(name)
    if not meta:
        raise KeyError(name)
    p = _prompt_path(name)
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            pass
    return str(meta.get("default") or "")


def save_locked_prompt(name: str, content: str) -> None:
    """Записать промт на диск. Создаёт каталог, если его нет."""
    p = _prompt_path(name)
    LOCKED_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(content or "", encoding="utf-8")


def _expected_pin() -> str:
    raw = (os.getenv(LOCKED_PROMPTS_PIN_ENV) or "").strip()
    return raw or LOCKED_PROMPTS_PIN_DEFAULT


def verify_pin(pin: object) -> bool:
    """Сверить введённый pin (строка/число) с env переменной."""
    if pin is None:
        return False
    candidate = str(pin).strip()
    if not candidate:
        return False
    return candidate == _expected_pin()


def public_state(name: str) -> dict:
    """Лёгкий dict для шаблона/JS: есть ли файл, какой ярлык, есть ли pin."""
    meta = LOCKED_PROMPTS.get(name)
    if not meta:
        return {"name": name, "known": False}
    return {
        "name": name,
        "known": True,
        "label": str(meta.get("label") or name),
        "present": is_locked_prompt_present(name),
    }
