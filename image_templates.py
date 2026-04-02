"""
Image reference templates for Nano Banana Pro (Kie image_input).

Layout on disk (user-managed):
  data/image_templates/<TemplateName>/
    logo.png | logo.jpg   — превью в UI (не уходит в API как референс)
    *.jpg / *.png / *.webp — до 3 файлов как image_input (сортировка по имени)
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_REFERENCE_IMAGES = 8  # лимит API; по ТЗ используем максимум 3
MAX_REFERENCE_FOR_PRODUCT = 3

MODULE_DIR = Path(__file__).resolve().parent
IMAGE_TEMPLATES_DIR = MODULE_DIR / "data" / "image_templates"


def is_logo_file(path: Path) -> bool:
    return path.stem.lower() == "logo" and path.suffix.lower() in ALLOWED_IMAGE_EXT


def safe_template_dir(templates_root: Path, name: str) -> Path | None:
    if not name or not str(name).strip():
        return None
    name = str(name).strip()
    if "/" in name or "\\" in name or name in (".", "..") or ".." in name:
        return None
    candidate = (templates_root / name).resolve()
    root = templates_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


def collect_reference_and_logo(template_dir: Path) -> tuple[list[Path], Path | None]:
    logo: Path | None = None
    refs: list[Path] = []
    for f in sorted(template_dir.iterdir(), key=lambda p: p.name.lower()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in ALLOWED_IMAGE_EXT:
            continue
        if is_logo_file(f):
            if logo is None:
                logo = f
            continue
        refs.append(f)
    refs = refs[: min(MAX_REFERENCE_FOR_PRODUCT, MAX_REFERENCE_IMAGES)]
    return refs, logo


def list_templates() -> list[dict]:
    """Список шаблонов для UI и валидации."""
    if not IMAGE_TEMPLATES_DIR.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(IMAGE_TEMPLATES_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        refs, logo = collect_reference_and_logo(p)
        out.append(
            {
                "folder_name": p.name,
                "ref_count": len(refs),
                "logo_file": logo.name if logo else None,
            }
        )
    return out


def build_image_input_urls(base_url: str, folder_name: str, template_dir: Path) -> list[str]:
    """Публичные URL файлов для Kie image_input."""
    refs, _ = collect_reference_and_logo(template_dir)
    base = base_url.rstrip("/")
    urls: list[str] = []
    for f in refs:
        enc_t = quote(folder_name, safe="")
        enc_f = quote(f.name, safe="")
        urls.append(f"{base}/template-assets/{enc_t}/{enc_f}")
    return urls
