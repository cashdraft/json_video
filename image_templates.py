"""
Image reference templates for Nano Banana Pro (Kie image_input).

Layout on disk (user-managed or via UI):
  image_templates/<TemplateName>/
    logo.png | logo.jpg   — превью в UI (не уходит в API как референс)
    *.jpg / *.png / *.webp — до 7 файлов как image_input
    _refs_order.json — порядок референсов (слева направо в UI = порядок в API)
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_REFERENCE_IMAGES = 8  # официальный лимит Kie image_input
MAX_REFERENCE_FOR_PRODUCT = 7  # наш лимит шаблона (≤ Kie)
REFS_ORDER_FILENAME = "_refs_order.json"

MODULE_DIR = Path(__file__).resolve().parent
IMAGE_TEMPLATES_DIR = MODULE_DIR / "image_templates"

_INVALID_NAME_CHARS = re.compile(r'[/\\<>:"|?*\x00]')


def validate_template_name(name: str) -> str | None:
    """None если имя допустимо, иначе текст ошибки."""
    n = str(name or "").strip()
    if not n:
        return "Введите название шаблона."
    if n in (".", "..") or ".." in n:
        return "Недопустимое имя шаблона."
    if _INVALID_NAME_CHARS.search(n):
        return "Имя не должно содержать / \\ и другие спецсимволы."
    return None


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


def _refs_order_path(template_dir: Path) -> Path:
    return template_dir / REFS_ORDER_FILENAME


def read_refs_order(template_dir: Path) -> list[str] | None:
    p = _refs_order_path(template_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return [str(x) for x in data if x]


def write_refs_order(template_dir: Path, order: list[str]) -> None:
    p = _refs_order_path(template_dir)
    try:
        p.write_text(json.dumps(order, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _list_reference_files_raw(template_dir: Path) -> list[Path]:
    refs: list[Path] = []
    for f in template_dir.iterdir():
        if not f.is_file():
            continue
        if f.name == REFS_ORDER_FILENAME:
            continue
        if f.suffix.lower() not in ALLOWED_IMAGE_EXT:
            continue
        if is_logo_file(f):
            continue
        refs.append(f)
    return refs


def apply_refs_order(refs: list[Path], order: list[str] | None) -> list[Path]:
    if not order:
        return sorted(refs, key=lambda p: p.name.lower())
    by_name = {p.name: p for p in refs}
    out: list[Path] = []
    seen: set[str] = set()
    for name in order:
        p = by_name.get(name)
        if p is not None and name not in seen:
            out.append(p)
            seen.add(name)
    for p in sorted(refs, key=lambda x: x.name.lower()):
        if p.name not in seen:
            out.append(p)
    return out


def collect_reference_and_logo(template_dir: Path) -> tuple[list[Path], Path | None]:
    logo: Path | None = None
    for f in template_dir.iterdir():
        if f.is_file() and is_logo_file(f):
            logo = f
            break
    refs_raw = _list_reference_files_raw(template_dir)
    order = read_refs_order(template_dir)
    refs = apply_refs_order(refs_raw, order)
    refs = refs[: min(MAX_REFERENCE_FOR_PRODUCT, MAX_REFERENCE_IMAGES)]
    return refs, logo


def save_reference_order(template_dir: Path, filenames: list[str]) -> str | None:
    """Сохранить порядок референсов. None при успехе."""
    refs_raw = _list_reference_files_raw(template_dir)
    existing = {p.name for p in refs_raw}
    if not existing:
        _refs_order_path(template_dir).unlink(missing_ok=True)
        return None
    order: list[str] = []
    seen: set[str] = set()
    for fn in filenames:
        name = str(fn or "").strip()
        if not name or name not in existing or name in seen:
            continue
        order.append(name)
        seen.add(name)
    for name in sorted(existing - seen, key=str.lower):
        order.append(name)
    write_refs_order(template_dir, order)
    return None


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


def template_detail(folder_name: str) -> dict | None:
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return None
    refs, logo = collect_reference_and_logo(td)
    return {
        "folder_name": td.name,
        "logo_file": logo.name if logo else None,
        "references": [{"filename": f.name} for f in refs],
    }


def create_template_dir(name: str) -> tuple[Path | None, str | None]:
    err = validate_template_name(name)
    if err:
        return None, err
    IMAGE_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_TEMPLATES_DIR / name.strip()
    if target.exists():
        return None, "Шаблон с таким именем уже существует."
    try:
        target.mkdir(parents=False)
    except OSError:
        return None, "Не удалось создать папку шаблона."
    return target, None


def delete_template_dir(folder_name: str) -> str | None:
    """Удалить папку шаблона целиком. None при успехе."""
    td = safe_template_dir(IMAGE_TEMPLATES_DIR, folder_name)
    if not td:
        return "Шаблон не найден."
    try:
        shutil.rmtree(td)
    except OSError:
        return "Не удалось удалить шаблон."
    return None


def rename_template_dir(old_name: str, new_name: str) -> str | None:
    err = validate_template_name(new_name)
    if err:
        return err
    old_d = safe_template_dir(IMAGE_TEMPLATES_DIR, old_name)
    if not old_d:
        return "Шаблон не найден."
    new_stem = new_name.strip()
    new_d = IMAGE_TEMPLATES_DIR / new_stem
    if new_d.exists() and new_d.resolve() != old_d.resolve():
        return "Шаблон с таким именем уже существует."
    try:
        old_d.rename(new_d)
    except OSError:
        return "Не удалось переименовать шаблон."
    return None


def remove_logo_files(template_dir: Path) -> None:
    for f in template_dir.iterdir():
        if f.is_file() and is_logo_file(f):
            try:
                f.unlink()
            except OSError:
                pass


def save_logo_file(template_dir: Path, data: bytes, ext: str = ".png") -> Path:
    remove_logo_files(template_dir)
    ext = ext if ext in ALLOWED_IMAGE_EXT else ".png"
    dest = template_dir / f"logo{ext}"
    dest.write_bytes(data)
    return dest


def delete_reference_file(template_dir: Path, filename: str) -> bool:
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    target = (template_dir / filename).resolve()
    try:
        target.relative_to(template_dir.resolve())
    except ValueError:
        return False
    if not target.is_file() or is_logo_file(target):
        return False
    try:
        target.unlink()
    except OSError:
        return False
    order = read_refs_order(template_dir)
    if order:
        order = [n for n in order if n != filename]
        if order:
            write_refs_order(template_dir, order)
        else:
            _refs_order_path(template_dir).unlink(missing_ok=True)
    return True


def add_reference_files(
    template_dir: Path, files: list[tuple[str, bytes]]
) -> tuple[list[str], str | None]:
    """Сохранить новые референсы. Возвращает (имена сохранённых, ошибка)."""
    refs, _logo = collect_reference_and_logo(template_dir)
    slots = MAX_REFERENCE_FOR_PRODUCT - len(refs)
    if slots <= 0:
        return [], f"Можно загрузить не более {MAX_REFERENCE_FOR_PRODUCT} фотографий."
    saved: list[str] = []
    order = read_refs_order(template_dir) or [p.name for p in refs]
    for orig_name, data in files[:slots]:
        ext = Path(orig_name).suffix.lower()
        if ext not in ALLOWED_IMAGE_EXT:
            ext = ".png"
        base = Path(orig_name).stem or "ref"
        base = re.sub(r"[^\w.\- ]+", "_", base)[:80] or "ref"
        dest = template_dir / f"{base}{ext}"
        n = 1
        while dest.exists():
            dest = template_dir / f"{base}_{n}{ext}"
            n += 1
        try:
            dest.write_bytes(data)
            saved.append(dest.name)
            order.append(dest.name)
        except OSError:
            return saved, "Не удалось сохранить файл."
    if saved:
        write_refs_order(template_dir, order)
    return saved, None


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
