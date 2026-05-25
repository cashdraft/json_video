# SAM2 Service (standalone smoke test)

Изолированная установка [Meta SAM 2](https://github.com/facebookresearch/sam2) для проверки automatic image segmentation на сервере. **Без** API, FastAPI, overlay, DINO и прочей интеграции с `json_video`.

## Структура

```
sam2_service/
  .venv/                 # локальное окружение (gitignore)
  external/sam2/         # официальный репозиторий (git clone)
  checkpoints/           # веса .pt
  test_images/           # test.jpg
  outputs/               # preview_masks.jpg, masks/*.png, summary.json
  test_inference.py
  README.md
```

## Установка (один раз)

```bash
cd /srv/json_video/sam2_service

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install torch torchvision pillow numpy opencv-python matplotlib

git clone https://github.com/facebookresearch/sam2.git external/sam2
cd external/sam2
pip install -e .
cd ../..

# Чекпоинт (лёгкий — tiny)
cd checkpoints
wget -q https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt
cd ..

# Тестовое фото (если ещё нет)
# положите JPEG/PNG в test_images/test.jpg
```

Альтернатива: скрипт из репозитория SAM2 (все веса):

```bash
cd external/sam2/checkpoints && ./download_ckpts.sh
cp sam2.1_hiera_tiny.pt ../../checkpoints/
```

## API (DINO → SAM2)

Запуск сервиса:

```bash
cd /srv/json_video/sam2_service
source .venv/bin/activate
uvicorn app:app --host 127.0.0.1 --port 8011
```

Health: `GET http://127.0.0.1:8011/health`

Segment: `POST http://127.0.0.1:8011/segment_dino` (multipart)

| Field | Type | Description |
|-------|------|-------------|
| `image` | file | исходный кадр |
| `detections_json` | string | JSON от Grounding DINO |
| `min_score` | float | optional, default 0.35 |

Тест-клиент:

```bash
python test_client_dino_to_sam2.py test_images/test.jpg test_images/dino_sample.json
```

Flask-мост (Remotion Preview UI): `POST /overlay-text/api/remotion-preview/sam2-segment`  
Env: `SAM2_URL=http://127.0.0.1:8011`

## Запуск теста (automatic masks)

```bash
cd /srv/json_video/sam2_service
source .venv/bin/activate
python test_inference.py
```

### Ожидаемый результат

- В консоли: `device`, число масок, время загрузки и inference.
- `outputs/preview_masks.jpg` — исходник с цветными масками.
- `outputs/masks/mask_000.png` … — отдельные бинарные маски (до 32 шт.).
- `outputs/summary.json` — метаданные прогона.

## Требования

- Python ≥ 3.10
- PyTorch ≥ 2.5.1 (при `pip install -e .` в `external/sam2` подтянется нужная версия)
- GPU опционален: на CPU тест идёт дольше; в `test_inference.py` сетка точек уменьшена (`POINTS_PER_SIDE=16`)

## Чекпоинты

| Файл | Размер | Config |
|------|--------|--------|
| `sam2.1_hiera_tiny.pt` | ~39M | `configs/sam2.1/sam2.1_hiera_t.yaml` |
| `sam2.1_hiera_small.pt` | ~46M | `configs/sam2.1/sam2.1_hiera_s.yaml` |

Для `small` в `test_inference.py` замените `CHECKPOINT` и `MODEL_CFG`.

## Примечания

- Папка `external/sam2` — полный клон upstream; не импортируйте Python из родительской директории клона (см. предупреждение в `build_sam.py`).
- Сообщение `Failed to build the SAM 2 CUDA extension` при установке без `nvcc` обычно не мешает inference.
- Артефакты: `.venv/`, `checkpoints/*.pt`, `outputs/*` — в `.gitignore` проекта.
