# JSON Video Generator

Веб-интерфейс для парсинга JSON-сценариев и генерации изображений/видео по сценам (KeyAI, Nano Banana, Veo3).

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Настройка API

1. Скопируй `.env.example` в `.env`:
   ```bash
   cp .env.example .env
   ```

2. Открой `.env` и вставь свой API-ключ:
   ```
   KEYAI_API_KEY=твой_ключ
   ```

## Запуск

```bash
python app.py
```

Откроется на http://127.0.0.1:5000

## GitHub Secrets (для CI/CD)

Если используешь GitHub Actions и нужны секреты:
1. Репозиторий → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret**
3. Name: `KEYAI_API_KEY`, Value: твой ключ
