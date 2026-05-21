"""
Image reference templates for Nano Banana Pro (Kie image_input).

Layout on disk (user-managed or via UI):
  image_templates/<TemplateName>/
    logo.png | logo.jpg   — превью в UI (не уходит в API как референс)
    Image_1.jpeg … Image_7.jpeg — до 7 файлов как image_input (JPEG, long edge ≤ 2K)
    _refs_order.json — порядок референсов (слева направо в UI = порядок в API)
    _refs_descriptions.json — описания по имени файла (Image_1.jpeg → текст)
"""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path
from typing import Union
from urllib.parse import quote

from PIL import Image

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
REFERENCE_FILE_EXT = ".jpeg"
MAX_REFERENCE_IMAGES = 8  # официальный лимит Kie image_input
MAX_REFERENCE_FOR_PRODUCT = 7  # наш лимит шаблона (≤ Kie)
REFS_ORDER_FILENAME = "_refs_order.json"
REFS_DESCRIPTIONS_FILENAME = "_refs_descriptions.json"
REFERENCE_MAX_LONG_EDGE = 2048
REFERENCE_JPEG_QUALITY = 88

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


def _refs_descriptions_path(template_dir: Path) -> Path:
    return template_dir / REFS_DESCRIPTIONS_FILENAME


def read_refs_descriptions(template_dir: Path) -> dict[str, str]:
    p = _refs_descriptions_path(template_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v or "") for k, v in data.items() if k}


def write_refs_descriptions(template_dir: Path, descriptions: dict[str, str]) -> None:
    p = _refs_descriptions_path(template_dir)
    refs_raw = _list_reference_files_raw(template_dir)
    existing = {f.name for f in refs_raw}
    out: dict[str, str] = {}
    for name in existing:
        out[name] = str(descriptions.get(name, "") or "").strip()
    if not out:
        p.unlink(missing_ok=True)
        return
    try:
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _remap_descriptions_after_rebuild(
    template_dir: Path, old_paths: list[Path], new_names: list[str]
) -> None:
    desc = read_refs_descriptions(template_dir)
    new_desc: dict[str, str] = {}
    for i, new_name in enumerate(new_names):
        old_name = old_paths[i].name if i < len(old_paths) else ""
        new_desc[new_name] = str(desc.get(old_name, "") or "").strip()
    write_refs_descriptions(template_dir, new_desc)


def save_reference_descriptions(
    template_dir: Path, descriptions: dict[str, str]
) -> str | None:
    """Сохранить описания только для существующих референсов. None при успехе."""
    refs_raw = _list_reference_files_raw(template_dir)
    existing = {p.name for p in refs_raw}
    clean: dict[str, str] = {}
    for fn, text in (descriptions or {}).items():
        name = str(fn or "").strip()
        if not name or name not in existing:
            continue
        clean[name] = str(text or "").strip()
    for name in existing:
        clean.setdefault(name, "")
    write_refs_descriptions(template_dir, clean)
    return None


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


def canonical_reference_filename(index: int) -> str:
    """Имя референса по позиции (1..7): Image_1.jpeg, Image_2.jpeg, …"""
    return f"Image_{index}{REFERENCE_FILE_EXT}"


def optimize_reference_image(source: Union[bytes, Path]) -> bytes:
    """Ресайз long edge ≤ 2K, конвертация в JPEG (sRGB), без upscale."""
    if isinstance(source, Path):
        raw = source.read_bytes()
    else:
        raw = source
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > REFERENCE_MAX_LONG_EDGE:
        scale = REFERENCE_MAX_LONG_EDGE / float(long_edge)
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=REFERENCE_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def normalize_reference_filenames(template_dir: Path) -> list[str]:
    """Оптимизировать референсы (2K JPEG) и сохранить как Image_1 … Image_N."""
    refs_raw = _list_reference_files_raw(template_dir)
    order = read_refs_order(template_dir)
    refs = apply_refs_order(refs_raw, order)
    refs = refs[: min(MAX_REFERENCE_FOR_PRODUCT, MAX_REFERENCE_IMAGES)]
    if not refs:
        for f in _list_reference_files_raw(template_dir):
            try:
                f.unlink()
            except OSError:
                pass
        _refs_order_path(template_dir).unlink(missing_ok=True)
        _refs_descriptions_path(template_dir).unlink(missing_ok=True)
        return []

    old_paths = list(refs)
    staged: list[bytes] = []
    for src in refs:
        try:
            staged.append(optimize_reference_image(src))
        except OSError:
            raise
        except Exception as exc:
            raise ValueError(f"Не удалось обработать «{src.name}»: {exc}") from exc

    for f in _list_reference_files_raw(template_dir):
        try:
            f.unlink()
        except OSError:
            pass

    final_names: list[str] = []
    for i, data in enumerate(staged, start=1):
        dest_name = canonical_reference_filename(i)
        (template_dir / dest_name).write_bytes(data)
        final_names.append(dest_name)

    write_refs_order(template_dir, final_names)
    _remap_descriptions_after_rebuild(template_dir, old_paths, final_names)
    return final_names


def normalize_all_image_template_references() -> dict[str, list[str]]:
    """Пройти все папки image_templates/ и нормализовать имена референсов."""
    if not IMAGE_TEMPLATES_DIR.is_dir():
        return {}
    out: dict[str, list[str]] = {}
    for p in sorted(IMAGE_TEMPLATES_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        out[p.name] = normalize_reference_filenames(p)
    return out


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
    try:
        normalize_reference_filenames(template_dir)
    except ValueError as e:
        return str(e)
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
    desc_map = read_refs_descriptions(td)
    return {
        "folder_name": td.name,
        "logo_file": logo.name if logo else None,
        "references": [
            {"filename": f.name, "description": desc_map.get(f.name, "")}
            for f in refs
        ],
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
    desc = read_refs_descriptions(template_dir)
    if filename in desc:
        desc.pop(filename, None)
        write_refs_descriptions(template_dir, desc)
    normalize_reference_filenames(template_dir)
    return True


def add_reference_files(
    template_dir: Path, files: list[tuple[str, bytes]]
) -> tuple[list[str], str | None]:
    """Сохранить новые референсы. Возвращает (имена сохранённых, ошибка)."""
    refs, _logo = collect_reference_and_logo(template_dir)
    slots = MAX_REFERENCE_FOR_PRODUCT - len(refs)
    if slots <= 0:
        return [], f"Можно загрузить не более {MAX_REFERENCE_FOR_PRODUCT} фотографий."
    order = read_refs_order(template_dir) or [p.name for p in refs]
    upload_idx = 0
    for _orig_name, data in files[:slots]:
        try:
            jpeg_data = optimize_reference_image(data)
        except ValueError as e:
            return [], str(e)
        except Exception:
            return [], "Не удалось обработать изображение."
        dest = template_dir / f".__upload_{upload_idx}{REFERENCE_FILE_EXT}"
        while dest.exists():
            upload_idx += 1
            dest = template_dir / f".__upload_{upload_idx}{REFERENCE_FILE_EXT}"
        upload_idx += 1
        try:
            dest.write_bytes(jpeg_data)
            order.append(dest.name)
        except OSError:
            return [], "Не удалось сохранить файл."
    if not order:
        return [], None
    write_refs_order(template_dir, order)
    try:
        saved = normalize_reference_filenames(template_dir)
    except ValueError as e:
        return [], str(e)
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


_REF_PROMPT_BLOCK_SEP = "\n---\n"
_REF_PROMPT_SECTION_HEADER = (
    "=== REFERENCE IMAGES (порядок совпадает с image_input: Image 1 = первый URL) ==="
)
_SCENE_PROMPT_SECTION_HEADER = "=== SCENE PROMPT (основной промпт генерации кадра) ==="


def build_image_generation_prompt(scene_prompt: str, template_dir: Path) -> str:
    """
    Промпт для Kie: сначала блоки референсов (Image N + Description), затем start.prompt.
    Порядок блоков = collect_reference_and_logo / image_input.
    """
    refs, _ = collect_reference_and_logo(template_dir)
    scene = str(scene_prompt or "").strip()
    if not refs:
        return scene
    desc_map = read_refs_descriptions(template_dir)
    blocks: list[str] = []
    for i, ref_path in enumerate(refs, start=1):
        filename = ref_path.name
        desc = str(desc_map.get(filename, "") or "").strip()
        desc_line = desc if desc else "(описание не задано)"
        blocks.append(f"Image {i} ({filename})\nDescription: {desc_line}")
    ref_body = _REF_PROMPT_BLOCK_SEP.join(blocks)
    parts = [
        _REF_PROMPT_SECTION_HEADER,
        ref_body,
        _SCENE_PROMPT_SECTION_HEADER,
        scene,
    ]
    return "\n\n".join(parts)
