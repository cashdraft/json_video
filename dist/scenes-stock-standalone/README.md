# Scenes Stock — standalone bundle

Подпроект **Finder Agent**: LLM генерирует поисковые запросы для сцен с `visual_source == Stock_Video`, затем по кнопке ↻ на доске сцен идёт поиск в **Pexels** (video + photo).

Вырезан из репозитория `json_video` (маршрут `/scenes-stock` на боевом сервере).

## Что внутри

| Часть | Назначение |
|-------|------------|
| `app.py` | Минимальный Flask: только `/scenes-stock` и API |
| `scenes_stock_*.py` | Логика Finder, board, Pexels, prefs |
| `scenes_map_agent.py` | Общий слой вызова ChatGPT / Claude |
| `rewrite_openai.py`, `claude_kie.py` | HTTP к OpenAI и Kie (Claude) |
| `templates/scenes_stock.html` | UI |
| `static/` | `style.css`, `scenes_map.css`, `scenes_stock.css`, `scenes_stock.js` |
| `scenes_stock/defaults/` | Дефолтные system/user промпты |
| `data/scenes_stock/` | `prefs.json`, превью из Pexels в `media/` |

## Требования

- Python **3.10+**
- Ключ **OpenAI** и/или **KEYAI** (Claude через Kie)
- Ключ **Pexels** — [pexels.com/api](https://www.pexels.com/api/)

## Быстрый старт (localhost)

```bash
cd scenes-stock-standalone
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# отредактируйте .env — вставьте ключи
python run.py
```

Откройте: **http://127.0.0.1:5000/scenes-stock**

Порт/host:

```bash
python run.py --host 0.0.0.0 --port 8080
```

## Переменные окружения (`.env`)

| Переменная | Обязательно | Описание |
|------------|-------------|----------|
| `OPENAI_API_KEY` | для ChatGPT | GPT-4 / 4.1 / 5.4 |
| `KEYAI_API_KEY` | для Claude | Kie.ai, модели Opus / Sonnet |
| `PEXELS_API_KEY` | для поиска ↻ | Можно также ввести в UI и сохранить в prefs |
| `FLASK_SECRET_KEY` | опционально | Сессии Flask |

Достаточно **одного** LLM-ключа под выбранную в UI модель.

## Как пользоваться (кратко)

1. В поле **Сцена** вставьте JSONL массив сцен (как в Job / Scenes Map), с полями `scene_id`, `hero_text`, `visual_source`, …
2. Выберите **Model** и **Источник** (Pexels).
3. **↻** у Finder Agent — LLM вернёт JSONL только для `Stock_Video` сцен.
4. **↻ Обновить сцены** — построится доска с visual intent и queries.
5. **↻** у сцены на доске — поиск в Pexels, превью в `data/scenes_stock/media/`.
6. Кнопка **J** — скачать wire JSON запроса к LLM (отладка).

Проверка **Finder** (Stock_Video in/out) — в браузере, без отдельного API.

## HTTP API (если нужно тестировать curl)

| Method | Path | Описание |
|--------|------|----------|
| GET | `/scenes-stock` | HTML страница |
| GET/POST | `/scenes-stock/api/prefs` | Состояние формы |
| POST | `/scenes-stock/api/generate` | Finder LLM (`agent: "finder"`) |
| POST | `/scenes-stock/api/search` | Pexels по `scene_id` |
| POST | `/scenes-stock/api/export` | Wire export (J) |
| GET | `/scenes-stock/media/<file>` | Локальные превью |

## Отличия от полного `json_video`

- Нет Job, overlay-text, SAM2, Remotion montage.
- Нет locked prompts под PIN — промпты в `data/scenes_stock/prefs.json` и defaults.
- Один процесс Flask, без Redis/Postgres.

## Интеграция обратно в монолит

Исходники синхронизированы с веткой `json_video`:

- Роуты: `app.py` → `scenes_stock_*` (строки ~3920–4120 в полном app).
- При мерже в монолит используйте файлы из этого архива и зарегистрируйте те же `@app.route`.

## Структура исходного репозитория (для справки)

```
json_video/
  app.py                    # полное приложение
  scenes_stock_*.py
  scenes_map_agent.py
  rewrite_openai.py
  claude_kie.py
  templates/scenes_stock.html
  static/scenes_stock.{js,css}
  static/scenes_map.css
  static/style.css
```

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| Model disabled | Заполните `OPENAI_API_KEY` или `KEYAI_API_KEY` |
| Pexels: укажите ключ | `PEXELS_API_KEY` в `.env` или поле на странице + OK |
| Пустая доска | Сначала ↻ Finder, потом «Обновить сцены» |
| Claude 401/403 | Проверьте `KEYAI_API_KEY` и модель в Kie |
| Стили «ломаные» | Убедитесь, что на месте все 3 CSS в `static/` |

## Лицензия / ключи

Не коммитьте `.env` с реальными ключами. `data/scenes_stock/prefs.json` может содержать сохранённый Pexels key — добавьте в `.gitignore` при разработке.

---

Собрано из `json_video` @ `/scenes-stock` (Finder + Pexels board).
