# MusicBot

Минималистичный Telegram-бот для поиска и прикрепления музыкальных треков. Без рекламы, без служебных сообщений — только аудио.

## Что умеет

- **Inline-режим:** в любом чате `@имя_бота название трека` → выбираешь результат → трек прикрепляется в чат.
- **Личный чат:** пишешь боту название трека → выбираешь из топ-5 → бот присылает аудио.
- Все скачанные треки кэшируются по `file_id` Telegram, повторные поиски — мгновенные.

## Установка

### 1. Получить токен бота

1. Открой [@BotFather](https://t.me/BotFather) в Telegram.
2. `/newbot` → задай имя и username.
3. Сохрани токен.
4. **Включи inline-режим:** `/setinline` → выбери своего бота → задай placeholder, например `Название трека…`.
5. **Включи inline feedback (обязательно):** `/setinlinefeedback` → `Enabled` (именно `Enabled`, не `Sampled`). Это нужно, чтобы при выборе результата в канале бот мог дозагрузить трек и заменить плейсхолдер на аудио. Без этого инлайн будет работать только для уже скачанных треков.

### 2. Создать канал-хранилище для file_id

Чтобы инлайн в один клик скачивал свежие треки прямо в канал, боту нужно куда-то загружать аудио и получать `file_id` (бот-скоупный идентификатор файла Telegram).

1. Создай **приватный канал** в Telegram (любое имя, например `MusicBot Storage`).
2. Добавь бота **админом** канала с правом **Post messages**.
3. Получи ID канала: пересылай любое сообщение из канала в [@JsonDumpBot](https://t.me/JsonDumpBot) → скопируй `chat.id` вида `-1001234567890`.
4. Этот ID пойдёт в `.env` как `STORAGE_CHAT_ID` (см. шаг «Настроить токен» ниже).

> Альтернатива: вместо канала можно указать ID своего личного чата с ботом — тогда копии скачанных треков будут уходить тебе в ЛС. Менее чисто (захламляет личку), но без создания канала.

### 3. Установить зависимости

**Windows:**
```powershell
# 1. Установить ffmpeg
winget install Gyan.FFmpeg
# (или скачать с https://www.gyan.dev/ffmpeg/builds/ и добавить в PATH)

# 2. Виртуальное окружение
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Зависимости
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
# 1. ffmpeg
sudo apt install ffmpeg          # Debian / Ubuntu
brew install ffmpeg              # macOS

# 2. venv
python3 -m venv .venv
source .venv/bin/activate

# 3. Зависимости
pip install -r requirements.txt
```

### 4. Получить VK-токен

Бот ищет и качает музыку через VK Music. Нужен токен с правами `audio`. Тут есть подвох: **обычные токены с vkhost.github.io (implicit OAuth flow) НЕ работают** — VK в 2025 заблокировал им доступ к `audio.search` (отвечает `error 3: Unknown method passed`). Нужен токен через **direct-auth flow** (Kate Mobile client_id + client_secret).

В репе лежит helper-скрипт, который это делает:

```bash
python scripts/get_vk_audio_token.py
```

Он спросит логин (телефон/email), пароль, при необходимости SMS-код для 2FA, ничего никуда не отправляет кроме `oauth.vk.com/token`, и в конце выводит токен. Используй **левый/тестовый VK-аккаунт** — токен это полный доступ к аудио и подпискам этого аккаунта.

### 5. Настроить `.env`

```bash
cp .env.example .env
```

Открой `.env` и заполни:

```
BOT_TOKEN=123456:ABC...           # токен из BotFather
STORAGE_CHAT_ID=-1001234567890    # ID канала-хранилища (см. шаг 2)
VK_TOKEN=vk1.a.XXXXX...           # токен из шага 4
```

### 6. Запустить

```bash
python bot.py
```

В логах появится `Bot started @your_bot_username` — всё готово.

## Как пользоваться

- **В личке с ботом:** напиши название трека (например `Imagine Dragons Believer`). Бот пришлёт 5 кнопок — нажми любую, получишь аудио.
- **В любом чате (в том числе в своём канале):** напечатай `@your_bot_username Imagine Dragons Believer`. Появятся результаты:
  - Если трек уже в кэше — он прикрепится моментально.
  - Если нет — в чат вставится текст «🎵 Imagine Dragons — Believer ⏳ Скачиваю…», а через 10–60 секунд бот заменит его на аудио (трюк через `editMessageMedia` для inline-сообщений). Файл попадёт и в канал-хранилище, и закэшируется — в следующий раз будет мгновенно.

## Деплой на сервер

Бот работает на long-polling — публичный IP не нужен. Готовый `Dockerfile` в репозитории. Подходящие бесплатные хостинги:

- [Railway.app](https://railway.app) — `railway up`
- [Fly.io](https://fly.io) — `fly launch`
- [Render.com](https://render.com) — Background Worker

Все они умеют держать persistent volume для `data/cache.db`. Если volume не подключён, кэш потеряется при рестарте, но это не критично — треки просто будут перекачиваться при первом обращении.

## Архитектура

- `bot.py` — точка входа, polling + health-эндпоинт для PaaS.
- `vkmusic.py` — поиск/скачивание через **VK Music** (api.vk.com/audio.search + audio.getById, Kate Mobile UA + direct-auth токен). ffmpeg обёртывает любой URL (mp3/HLS) в m4a.
- `cache.py` — SQLite-кэш `video_id → file_id` (где `video_id` = `{owner_id}_{audio_id}` от VK).
- `handlers/inline.py` — `inline_query` (кэш + VK-поиск, Article-плейсхолдеры) и `chosen_inline_result` (фоновое скачивание + `editMessageMedia` → подмена текста на аудио).
- `handlers/messages.py` — `/start dl_<id>`, текстовый поиск в ЛС.
- `handlers/callbacks.py` — кнопки в ЛС (`pick:<id>`) и заглушка для кнопки-индикатора.
- `handlers/common.py` — `materialize_file_id`: единый путь «гарантировать наличие `file_id` для `video_id`» (cache-or-download-or-upload-to-storage), используется и DM, и inline.
- `handlers/_dedup.py` — single-flight: одновременные запросы одного `video_id` ждут одного скачивания.
- `scripts/get_vk_audio_token.py` — helper для получения рабочего VK-токена через direct-auth flow (вместо отвалившегося implicit flow с vkhost.github.io).

## Ограничения

- **Лимит размера файла — 50 MB** (ограничение обычных Telegram-ботов). Очень длинные треки/миксы не пройдут.
- **Только VK** в качестве источника. Каталог большой (мейнстрим + русская сцена), но не всё что есть на YouTube есть в VK.
- **VK-токен — это твой VK-аккаунт.** Используй левый/тестовый аккаунт. Токен не expire'ится автоматически но может быть отозван если VK заметит подозрительную активность.
- **Никакой авторизации/whitelist.** Если бот публичный, любой может им пользоваться.
