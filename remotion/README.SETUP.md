# Remotion (json_video)

Папка с проектом Remotion (https://www.remotion.dev). Развёрнут шаблон
`template-helloworld` — две композиции (`HelloWorld`, `OnlyLogo`, 1920×1080 @ 30 fps).

## Окружение

- Node.js: `v22.22.2` (NodeSource APT-репо `node_22.x` для Ubuntu 24.04).
- npm/npx: `10.9.7` (`/usr/bin/npm`, `/usr/bin/npx`).
- В CursorIDE PATH перекрывается своим Node 20.18.2 — для команд remotion явно
  используй `/usr/bin/node` или `PATH=/usr/bin:$PATH`.

## Системные библиотеки (для headless Chromium)

Уже установлены через apt:

```
libnspr4 libnss3 libdbus-1-3 libatk1.0-0t64 libasound2t64
libxrandr2 libxkbcommon0 libxfixes3 libxcomposite1
libxdamage1 libgbm1 libatk-bridge2.0-0t64 libpangocairo-1.0-0
libpango-1.0-0 libcairo2 libcups2t64 fonts-liberation
```

`ffmpeg` (`/usr/bin/ffmpeg`) уже есть в системе.

## Команды

Из `/srv/json_video/remotion/`:

```bash
# Список композиций
PATH=/usr/bin:$PATH npx --no remotion compositions src/index.ts

# Бандл
PATH=/usr/bin:$PATH npx --no remotion bundle src/index.ts

# Рендер MP4 (одна композиция)
PATH=/usr/bin:$PATH npx --no remotion render src/index.ts OnlyLogo /tmp/out.mp4

# Локальная Remotion Studio (HTTP, по умолчанию :3000)
PATH=/usr/bin:$PATH npx --no remotion studio
```

## Что лежит на диске

- `src/index.ts` — точка входа (`registerRoot`).
- `src/Root.tsx` — список композиций.
- `src/HelloWorld.tsx` + `src/HelloWorld/*` — пример сцены.
- `remotion.config.ts` — глобальные настройки рендера.
- `node_modules/` — ~273 MB, в git не уходит (см. `.gitignore` корня проекта).

## Что дальше

Заглушка-стартер. Композиции под json_video будем добавлять отдельными файлами в
`src/` и регистрировать в `src/Root.tsx`. Из бэкенда (`app.py`) рендер можно
вызывать через subprocess `npx remotion render …` либо через `@remotion/renderer`
из отдельного Node-скрипта, который примет JSON со сценой и style_pack.

Источник: https://www.remotion.dev/
