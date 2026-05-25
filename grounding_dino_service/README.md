# Grounding DINO Service

Локальный FastAPI-сервис для open-vocabulary object detection через [Hugging Face Transformers](https://huggingface.co/docs/transformers/model_doc/grounding-dino).

Модель: `IDEA-Research/grounding-dino-base`

## Установка

```bash
cd grounding_dino_service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Первый запуск скачает веса модели с Hugging Face (~1 GB).

## Запуск

```bash
uvicorn app:app --host 0.0.0.0 --port 8010
```

## Health

```bash
curl http://127.0.0.1:8010/health
```

Ответ:

```json
{
  "status": "ok",
  "device": "cuda",
  "model": "IDEA-Research/grounding-dino-base"
}
```

На CPU будет `"device": "cpu"` (медленнее, но работает).

## Detect

```bash
curl -X POST "http://127.0.0.1:8010/detect" \
  -F "image=@test.jpg" \
  -F "prompt=person. face. hands. laptop." \
  -F "box_threshold=0.25" \
  -F "text_threshold=0.25"
```

### Form fields

| Поле | Тип | По умолчанию | Описание |
|------|-----|--------------|----------|
| `image` | file | — | JPEG/PNG/WebP и др. |
| `prompt` | string | — | Запросы через точку: `person. face. hands.` |
| `box_threshold` | float | 0.25 | Порог confidence для bbox |
| `text_threshold` | float | 0.25 | Порог для text grounding |

### Ответ

```json
{
  "image_width": 1024,
  "image_height": 572,
  "prompt": "person. face. hands. laptop.",
  "detections": [
    {
      "label": "person",
      "score": 0.87,
      "box_px": { "x1": 260, "y1": 40, "x2": 700, "y2": 560, "w": 440, "h": 520 },
      "box_pct": { "x_pct": 25.39, "y_pct": 6.99, "w_pct": 42.97, "h_pct": 90.91 }
    }
  ]
}
```

## Test client

```bash
python test_client.py /path/to/image.jpg "person. face. laptop. hands."
```

Опционально четвёртый аргумент — URL API (по умолчанию `http://127.0.0.1:8010/detect`).

## Примечания

- Prompt автоматически приводится к нижнему регистру и формату `term1. term2.`
- CUDA используется автоматически, если доступна
- Папки `uploads/` и `outputs/` зарезервированы для будущих артефактов

## Важно для основного проекта (Flask :5000)

- Устанавливайте зависимости **только** в `grounding_dino_service/.venv`, не в `/srv/json_video/.venv`.
- Не запускайте `pip` и не делайте `git checkout` / `git restore` из корня `json_video` без необходимости.
- Модель в RAM ~1.5–2 GB на CPU; когда Grounding DINO не нужен, останавливайте uvicorn на :8010, чтобы не давить основной Flask.
- Этот сервис **не связан** с `json-video.service` и не перезапускает его.
