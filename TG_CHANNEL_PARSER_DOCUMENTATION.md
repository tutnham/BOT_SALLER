# Техническая документация: сервис парсинга Telegram-канала (MTProto, userbot)

Версия: 1.0
Статус относительно основного проекта («бот закупок»): **отдельный сервис**, не связан по бизнес-логике с ботом закупок (`TECHNICAL_DOCUMENTATION.md`). Использует другой протокол доступа к Telegram (MTProto от имени личного аккаунта, а не Bot API), другую БД/схему и другой контур авторизации. Причина разделения: Bot API и MTProto-клиент личного аккаунта — принципиально разные механизмы аутентификации и разные ограничения (см. раздел 2), их смешивание в одном процессе увеличивает риск потери сессии аккаунта.

---

## 1. Назначение и границы

Сервис читает публикации из одного или нескольких Telegram-каналов, в которых уже состоит личный аккаунт пользователя, сохраняет их (текст, метаданные, медиа) в PostgreSQL и отдаёт через свой API. Сервис один, но у каждого канала есть признак `purpose` (таблица `parser_channels`, раздел 9), определяющий, что с постами канала делается дальше:

- **`monitoring`** — личный канал для самостоятельного мониторинга: текст передаётся в модуль ии-обработки для классификации/суммаризации (раздел 8).
- **`supplier_price_source`** — канал поставщика бота закупок (`TECHNICAL_DOCUMENTATION.md`, алгоритм 9.5.1): текст сохраняется без AI-классификации и отдаётся по API внешнему потребителю (раздел 12).

ИИ не участвует в получении доступа к каналу и не заменяет MTProto-клиент — он получает только уже извлечённые сервисом данные, и только для каналов с `purpose='monitoring'`.

Вне скоупа: боты (Bot API), заходы в каналы, где аккаунт не состоит, автоматическое взаимодействие с каналом (лайки, комментарии, репосты) — сервис только читает. OCR/распознавание текста на изображениях не реализовано — если канал с `purpose='supplier_price_source'` публикует цены только в виде фото/файла, такой пост сохраняется в `parser_posts`, но его `raw_text` будет пустым — дальше он не распознаётся.

---

## 2. Почему MTProto (Pyrogram), а не Bot API

Официальный Bot API не даёт боту доступа к истории сообщений канала и не позволяет читать посты каналов, в которых бот не является администратором с явным правом чтения; кроме того, боты не могут выступать «обычным подписчиком». Требование ТЗ — доступ от имени личного аккаунта, уже состоящего в канале, — реализуется только через MTProto-клиент (Pyrogram/Telethon), авторизованный тем же аккаунтом.

Выбор библиотеки: **Pyrogram** (asyncio, есть `export_session_string()` для хранения сессии в строке без файла сессии на диске, есть готовый метод `get_media_group()` для альбомов) [Pyrogram Client.export_session_string](https://docs.pyrogram.org/api/methods/export_session_string), [Pyrogram Storage Engines / Session Strings](https://docs.pyrogram.org/topics/storage-engines), [Pyrogram Client.get_chat_history](https://docs.pyrogram.org/api/methods/get_chat_history), [Pyrogram Client.get_media_group](https://docs.pyrogram.org/api/methods/get_media_group).

Telethon — равноценная альтернатива (`StringSession`, `iter_messages`, `FloodWaitError`) [Telethon RPC Errors](https://docs.telethon.dev/en/stable/concepts/errors.html). Ниже документация написана под Pyrogram; при выборе Telethon меняются только конкретные вызовы API, архитектура и схема БД не меняются.

---

## 3. Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│ Telegram (MTProto)                                               │
│  Канал, в котором состоит личный аккаунт пользователя            │
└───────────────────────────────┬───────────────────────────────────┘
                                 │ авторизованная сессия личного аккаунта
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ tg-listener (Python, Pyrogram)                                   │
│  • auth.py — первичная интерактивная авторизация (разово)        │
│  • backfill.py — режим 1: история за период / с message_id       │
│  • realtime.py — режим 2: обработчик новых сообщений             │
│  • пишет в БД: parser_posts (status=new) + parser_tasks           │
└───────────────────────────────┬───────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ PostgreSQL — parser_channels, parser_posts, parser_media_files,  │
│              parser_tasks, parser_ai_results                     │
└───────────────────────────────┬───────────────────────────────────┘
                                 │ опрос задач (polling, без Redis на MVP)
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ tg-worker (Python, asyncio-цикл)                                  │
│  • task_type=download_media → скачивание в local/S3, хеш, mime    │
│  • task_type=ai_process → вызов AI-модуля, валидация JSON схемой  │
│  • retry с экспоненциальной задержкой, лимит попыток               │
└───────────────────────────────┬───────────────────────────────────┘
                                 │ HTTP
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ AI-модуль (заменяемый интерфейс AIProvider)                       │
│  • OllamaProvider — HTTP к локальному Ollama, format=JSON Schema  │
│    [Ollama Structured Outputs], [Ollama /api/generate]            │
│  • место для внешнего API-провайдера без переписывания worker'а   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ api (FastAPI) — просмотр данных и управление обработкой           │
│  защищено токеном/JWT, отдельный процесс, читает ту же БД         │
└─────────────────────────────────────────────────────────────────┘
```

Redis/очередь задач (Celery, arq) — не входит в MVP; таблица `parser_tasks` с опросом (polling) закрывает требование «задачи обработки» без дополнительной инфраструктуры. Переход на Redis-очередь возможен позже без изменения схемы БД (раздел 13, пункт «расширение»).

---

## 4. Структура репозитория

```
tg-channel-parser/
├── app/
│   ├── config.py                 # Pydantic Settings из .env
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── migrations/           # Alembic, отдельные от бота закупок
│   ├── mtproto/
│   │   ├── client_factory.py     # создание Pyrogram Client из session_string
│   │   ├── auth_cli.py           # интерактивная первичная авторизация
│   │   ├── backfill.py           # режим 1
│   │   └── realtime.py           # режим 2
│   ├── extraction/
│   │   ├── post_mapper.py        # Message → dict полей (раздел 7)
│   │   └── media_mapper.py       # вложения → dict (раздел 8)
│   ├── storage/
│   │   ├── base.py               # интерфейс MediaStorage
│   │   ├── local_storage.py
│   │   └── s3_storage.py
│   ├── ai/
│   │   ├── base.py               # интерфейс AIProvider.process(text) -> dict
│   │   ├── ollama_provider.py
│   │   ├── schemas.py            # Pydantic-схема результата ИИ (раздел 11)
│   │   └── prompts/
│   ├── worker/
│   │   └── task_worker.py        # опрос parser_tasks, обработка, retry/backoff
│   ├── api/
│   │   ├── main.py               # FastAPI приложение
│   │   ├── auth.py                # проверка токена/JWT
│   │   └── routers/
│   │       ├── channels.py
│   │       ├── posts.py
│   │       └── ai_results.py
│   └── logging_setup.py          # структурные логи, редактирование секретов
├── tests/
├── alembic.ini
├── requirements.txt
├── Dockerfile.listener
├── Dockerfile.worker
├── Dockerfile.api
├── docker-compose.tg-parser.yml
├── .env.example
└── README.md
```

---

## 5. Авторизация MTProto

### 5.1 Правило хранения секретов

`api_id`, `api_hash`, номер телефона, пароль 2FA, `session_string` — только в переменных окружения (`.env`), которые:

- добавлены в `.gitignore` (обязательная строка `.env` и `*.session`);
- не логируются ни при каком уровне логирования (раздел 14);
- не хранятся в коде и не передаются как аргументы командной строки в открытом виде в скриптах, которые могут попасть в shell-историю без разбора (использовать `getpass` для пароля 2FA в `auth_cli.py`).

### 5.2 Первичная авторизация (одноразовый интерактивный шаг)

```
python -m app.mtproto.auth_cli

1. Клиент Pyrogram создаётся in-memory: Client(":memory:", api_id=API_ID, api_hash=API_HASH)
2. Pyrogram запрашивает номер телефона (или берётся из TELEGRAM_PHONE_NUMBER, если задан)
3. Telegram присылает код в приложение — ввод кода в интерактивном режиме
4. Если включена 2FA — запрос пароля через getpass (не через argv/env в этом шаге)
5. После успешной авторизации:
     session_string = await app.export_session_string()
6. Скрипт печатает session_string в терминал — пользователь сам копирует
   значение в TELEGRAM_SESSION_STRING в .env (сервис не пишет .env автоматически,
   чтобы не перезаписать другие переменные и не оставить след в истории команд)
7. .session файл (если создавался) — удаляется; хранится только session_string
```

Источник метода: [Pyrogram Client.export_session_string](https://docs.pyrogram.org/api/methods/export_session_string), [Pyrogram Session Strings](https://docs.pyrogram.org/topics/storage-engines).

### 5.3 Последующие запуски

`tg-listener`, `tg-worker` создают клиент так:

```python
Client(
    "listener",
    api_id=settings.TELEGRAM_API_ID,
    api_hash=settings.TELEGRAM_API_HASH,
    session_string=settings.TELEGRAM_SESSION_STRING,
    in_memory=True,
)
```

Номер телефона и пароль 2FA после этого шага не требуются процессу и могут быть удалены из `.env` (оставлены только на случай повторной интерактивной авторизации, если сессия аннулирована вручную из другого клиента Telegram).

### 5.4 Ротация/инвалидация сессии

Если Telegram аннулирует сессию (пользователь вручную завершил её в «Настройки → Устройства», либо сработала защита от подозрительной активности), Pyrogram выбросит ошибку авторизации при следующем вызове — сервис должен: записать событие в лог с уровнем `CRITICAL`, остановить `tg-listener`/`tg-worker`, не пытаться автоматически повторно ввести номер/2FA (это требует присутствия человека) — уведомление подготовить отдельно (например, через существующий бот закупок в чат администратора, если такая интеграция нужна отдельным тикетом).

---

## 6. Режимы работы

Сервис обслуживает любое количество каналов одним процессом под одной сессией личного аккаунта; список активных каналов хранится в таблице `parser_channels` (раздел 9), а не в одной переменной окружения.

### 6.0 Регистрация нового канала

```
CLI: python -m app.mtproto.add_channel --channel @supplier_x_price \
       --purpose supplier_price_source
     (для личного канала: --purpose monitoring, значение по умолчанию)

1. chat = await app.get_chat(channel)   # проверка, что аккаунт состоит в канале; ошибка, если нет
2. INSERT parser_channels (channel_id=chat.id, username=chat.username, title=chat.title, purpose=purpose)
   ON CONFLICT (channel_id) DO UPDATE SET purpose=EXCLUDED.purpose
3. (опционально) сразу запустить backfill для этого канала (6.1)
4. перезагрузить tg-listener, чтобы реалтайм-обработчик (6.2) взял в работу новый канал
```

Перезагрузка контейнера на шаге 4 — **не обязательна**: listener перечитывает `parser_channels` каждые ~30s (`listener_reload_interval_seconds`) и обновляет Pyrogram filter без restart.

### 6.1 Backfill (первичная загрузка истории)

```
CLI: python -m app.mtproto.backfill --channel @my_channel \
       [--from-date 2026-01-01] [--to-date 2026-07-01] \
       [--from-message-id 12345]

(канал должен быть уже зарегистрирован командой 6.0; если нет — backfill регистрирует его сам с purpose='monitoring' по умолчанию)

1. resolve chat: chat = await app.get_chat(channel)   # проверка, что аккаунт состоит в канале
2. UPSERT parser_channels (channel_id=chat.id, username=chat.username, title=chat.title)
3. async for message in app.get_chat_history(
       chat_id=chat.id,
       offset_date=from_date,      # либо
       offset_id=from_message_id,  # взаимоисключимо с offset_date
   ):
     если message.date > to_date (при заданном to_date) → skip/break по направлению итерации
     upsert_post(message)   # раздел 7, идемпотентно по (channel_id, message_id)
4. По завершении: UPDATE parser_channels SET last_synced_message_id = MAX(message_id)
```

Метод и параметры: [Pyrogram Client.get_chat_history](https://docs.pyrogram.org/api/methods/get_chat_history) (`chat_id`, `limit`, `offset`, `offset_id`, `offset_date`; возвращает async-генератор сообщений).

### 6.2 Realtime (постоянное отслеживание)

```
# список активных каналов читается из parser_channels при старте процесса
_active_channel_ids = [c.channel_id for c in parser_channels WHERE is_active=true]

@app.on_message(filters.chat(_active_channel_ids))
async def on_new_post(client, message):
    upsert_post(message)   # раздел 7, включая 7.4 (создание задач по purpose)

python -m app.mtproto.realtime   # long-running процесс, автопереподключение Pyrogram
```

`filters.chat()` принимает лист идентификаторов чатов — один обработчик обслуживает все каналы независимо от их `purpose`; ветвление по назначению происходит внутри `upsert_post` (7.4). Список каналов фиксируется при старте процесса — добавление нового канала (6.0) требует перезагрузки `tg-listener`.

Если канал прислал альбом (несколько сообщений с одним `media_group_id`), обработчик получает каждое сообщение отдельным событием; для получения всех элементов альбома сразу используется `await client.get_media_group(chat_id, message.id)` [Pyrogram Client.get_media_group](https://docs.pyrogram.org/api/methods/get_media_group). Дедупликация альбома — по `grouped_id` (см. раздел 7.2): все сообщения альбома сохраняются как отдельные строки `parser_posts` с одинаковым `grouped_id`, повторная агрегация делается на уровне чтения (API), не на уровне записи.

### 6.3 Идемпотентность между режимами

Backfill и realtime могут обработать один и тот же `message_id`, если временные окна пересекаются (например, backfill запущен повторно после сбоя, пока realtime уже видел новое сообщение). Уникальный индекс `(channel_id, message_id)` в `parser_posts` (раздел 9) гарантирует, что вставка выполняется через `INSERT ... ON CONFLICT (channel_id, message_id) DO NOTHING` — пост не обрабатывается дважды.

---

## 7. Извлекаемые данные поста

### 7.1 Обязательные поля (для любого типа контента)

| Поле | Источник Pyrogram |
|---|---|
| `channel_id` | `message.chat.id` |
| `message_id` | `message.id` |
| `date` | `message.date` |
| `text_or_caption` | `message.text or message.caption` |
| `message_link` | `message.link` (Pyrogram формирует `t.me/username/id` или `t.me/c/internal_id/id` автоматически) |
| `content_type` | классификация по `message.media` (см. 7.3) |
| `grouped_id` | `message.media_group_id` (`None`, если пост не альбом) |
| `raw_metadata` | сериализованный `message` (JSON) целиком, для аудита без потерь |

### 7.2 Медиагруппы (альбомы)

Все сообщения одного альбома имеют одинаковый `media_group_id`. Каждое сообщение альбома сохраняется отдельной строкой `parser_posts` со своим `message_id`, но общим `grouped_id`. Подпись к альбому (caption) в Telegram присутствует, как правило, только у одного сообщения группы — это фиксируется как есть, без «размножения» текста на остальные элементы (raw-данные не изменяются).

### 7.3 Типы контента (`content_type`)

| `content_type` | Условие |
|---|---|
| `text` | `message.text` есть, `message.media is None` |
| `photo` | `message.photo` |
| `video` | `message.video` |
| `document` | `message.document` |
| `audio` | `message.audio` |
| `voice` | `message.voice` |
| `media_group` | `message.media_group_id is not None` (доп. флаг вместе с конкретным типом медиа внутри) |
| `other` | иной/неподдерживаемый тип медиа — сохраняется `raw_metadata`, `status='failed'` с `error_text='unsupported_media_type'` только для скачивания, текст всё равно сохраняется |

### 7.4 Создание задач после сохранения поста (`upsert_post`)

После `INSERT ... parser_posts ON CONFLICT (channel_id, message_id) DO NOTHING` создание задач в `parser_tasks` зависит от `parser_channels.purpose` этого канала:

```
ЕСЛИ вставка не была no-op (пост действительно новый):
  ЕСЛИ post.content_type in ('photo','video','document','audio','voice','media_group'):
      INSERT parser_tasks (post_id, task_type='download_media')   -- всегда, независимо от purpose
  ЕСЛИ channel.purpose == 'monitoring':
      INSERT parser_tasks (post_id, task_type='ai_process')
  ЕСЛИ channel.purpose == 'supplier_price_source':
      -- ai_process НЕ создаётся: разбор прайса делает бот закупок через свой llm.parse_price_list
      -- (см. TECHNICAL_DOCUMENTATION.md, 9.5.1), пост уже доступен через GET /posts
      pass
```

Это правило исключает двойную обработку и рассинхронизацию схем: один и тот же код `tg-listener`/`tg-worker` обслуживает оба типа каналов, разница — только в том, какая задача ставится в очередь.

---

## 8. Вложения и хранилище медиа

Для каждого файла вложения фиксируются в `parser_media_files`: `file_unique_id`, `file_id` (может истекать — не использовать как постоянный идентификатор для дедупликации, только `file_unique_id` + `sha256_hash` после скачивания), `media_type`, `mime_type`, `file_size` (из атрибутов `message.photo/video/document/audio/voice`), `storage_path`, `sha256_hash` (считается после скачивания, до записи в БД).

### 8.1 Backend хранилища (интерфейс `MediaStorage`)

```python
class MediaStorage(Protocol):
    async def save(self, local_tmp_path: str, key: str) -> str:  # -> итоговый storage_path/URL
        ...
```

- `LocalStorage` — сохраняет в `MEDIA_LOCAL_PATH/{channel_id}/{message_id}/{file_unique_id}.{ext}`.
- `S3Storage` — совместим с S3 (AWS S3, MinIO, Yandex Object Storage, Supabase Storage через S3-протокол); ключ такой же, бакет/эндпоинт из `.env`.

Выбор бэкенда — переменная `MEDIA_STORAGE_BACKEND=local|s3`, не требует изменения кода `tg-worker`.

### 8.2 Алгоритм скачивания (`task_type=download_media`)

```
1. tmp_path = await client.download_media(message, file_name=tmp_dir/...)
2. sha256_hash = hashlib.sha256(open(tmp_path,'rb').read()).hexdigest()
3. mime_type = mimetypes.guess_type(tmp_path) или атрибут message.<media>.mime_type, если есть
4. storage_path = await storage.save(tmp_path, key=...)
5. UPDATE parser_media_files SET storage_path, file_size, mime_type, sha256_hash, download_status='downloaded'
6. os.remove(tmp_path)   # временный файл не остаётся на диске сервиса после S3-загрузки
```

---

## 9. Схема базы данных (PostgreSQL DDL)

Рекомендация: отдельная база данных `tg_parser` в том же экземпляре PostgreSQL, что и БД бота закупок (изоляция схем, разные Alembic-цепочки миграций, разные учётные данные подключения).

```sql
CREATE TYPE parser_content_type AS ENUM (
  'text', 'photo', 'video', 'document', 'audio', 'voice', 'media_group', 'other'
);

CREATE TYPE parser_status AS ENUM (
  'new', 'downloaded', 'processing', 'processed', 'failed'
);

CREATE TYPE parser_task_type AS ENUM ('download_media', 'ai_process');
CREATE TYPE parser_media_type AS ENUM ('photo', 'video', 'document', 'audio', 'voice');
CREATE TYPE parser_storage_backend AS ENUM ('local', 's3');
CREATE TYPE parser_channel_purpose AS ENUM ('monitoring', 'supplier_price_source');

CREATE TABLE parser_channels (
  id BIGSERIAL PRIMARY KEY,
  channel_id BIGINT UNIQUE NOT NULL,
  username TEXT,
  title TEXT,
  purpose parser_channel_purpose NOT NULL DEFAULT 'monitoring',
  -- 'monitoring'            — личный канал пользователя, посты проходят AI-классификацию (раздел 8)
  -- 'supplier_price_source' — канал поставщика бота закупок; AI-классификация не запускается, см. раздел 12
  is_active BOOLEAN NOT NULL DEFAULT true,
  last_synced_message_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE parser_posts (
  id BIGSERIAL PRIMARY KEY,
  channel_id BIGINT NOT NULL REFERENCES parser_channels(channel_id),
  message_id BIGINT NOT NULL,
  grouped_id BIGINT,
  post_date TIMESTAMPTZ NOT NULL,
  content_type parser_content_type NOT NULL,
  raw_text TEXT,                      -- НЕИЗМЕНЯЕМЫЙ оригинал текста/подписи
  message_link TEXT,
  raw_metadata JSONB NOT NULL,        -- полный сериализованный message, для аудита
  status parser_status NOT NULL DEFAULT 'new',
  excluded BOOLEAN NOT NULL DEFAULT false,
  excluded_reason TEXT,
  excluded_at TIMESTAMPTZ,
  error_text TEXT,
  attempts INT NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel_id, message_id)
);

CREATE TABLE parser_media_files (
  id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL REFERENCES parser_posts(id) ON DELETE CASCADE,
  media_type parser_media_type NOT NULL,
  file_unique_id TEXT NOT NULL,
  file_id TEXT,                        -- временный, может истекать, не для дедупликации
  storage_backend parser_storage_backend NOT NULL,
  storage_path TEXT,
  file_size BIGINT,
  mime_type TEXT,
  sha256_hash TEXT,
  download_status parser_status NOT NULL DEFAULT 'new',
  error_text TEXT,
  downloaded_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (post_id, file_unique_id)
);

CREATE TABLE parser_tasks (
  id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL REFERENCES parser_posts(id) ON DELETE CASCADE,
  task_type parser_task_type NOT NULL,
  status parser_status NOT NULL DEFAULT 'new',
  attempts INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 3,
  error_text TEXT,
  scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- когда задачу можно забрать (backoff)
  last_attempt_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_parser_tasks_pickup ON parser_tasks (status, scheduled_at);

CREATE TABLE parser_ai_results (
  id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL REFERENCES parser_posts(id) ON DELETE CASCADE,
  is_current BOOLEAN NOT NULL DEFAULT true,   -- при реобработке старые версии остаются, но is_current=false
  summary TEXT,
  category TEXT,
  entities JSONB,
  key_topics JSONB,
  toxicity_score REAL,
  has_profanity BOOLEAN,
  cleaned_text TEXT,                          -- очищенная/преобразованная версия (не оригинал!)
  classification_reason TEXT,
  schema_valid BOOLEAN NOT NULL,
  model_used TEXT NOT NULL,
  raw_ai_response JSONB,                       -- сырой ответ модели, для отладки валидации
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_parser_ai_results_post ON parser_ai_results (post_id, is_current);
```

Ключевые гарантии схемы:

- Уникальность поста — `UNIQUE (channel_id, message_id)` в `parser_posts`; повторная вставка при перезапуске/повторном backfill/дублирующемся realtime-событии обрабатывается через `INSERT ... ON CONFLICT DO NOTHING`.
- Оригинал (`raw_text`, `raw_metadata`) физически отделён от результата ИИ (`parser_ai_results.cleaned_text`, `summary` и т.д.) — разные таблицы, `raw_text` в `parser_posts` никогда не перезаписывается модулем ИИ.
- Статусная модель `new → downloaded → processing → processed / failed` реализована как в `parser_posts.status` (общий статус пайплайна поста), так и в `parser_tasks.status` (статус конкретной задачи: скачивание или ИИ-обработка) — с `error_text` и `last_attempt_at` на обоих уровнях.
- Исключение поста из обработки — `parser_posts.excluded=true`; воркер обязан пропускать такие посты при выборке новых задач (`WHERE excluded=false`).

---

## 10. Модуль ИИ-обработки

### 10.1 Схема результата (Pydantic)

```python
from pydantic import BaseModel, Field
from typing import Literal

class AIPostResult(BaseModel):
    summary: str
    category: str
    entities: list[str] = Field(default_factory=list)
    key_topics: list[str] = Field(default_factory=list)
    toxicity_score: float = Field(ge=0, le=1)
    has_profanity: bool
    cleaned_text: str
    classification_reason: str
```

Ответ модели проверяется через `AIPostResult.model_validate(raw_json)`; при `ValidationError` — задача помечается `failed` с текстом ошибки в `parser_tasks.error_text`, оригинальный пост и `raw_text` не изменяются и не удаляются.

### 10.2 Интерфейс провайдера (заменяемость)

```python
class AIProvider(Protocol):
    async def process(self, text: str) -> dict:
        """Возвращает сырой JSON (до валидации Pydantic)."""
```

`task_worker.py` работает только с `AIProvider`, не знает про конкретного вендора — переключение на внешний API (OpenAI-совместимый, Anthropic и т.д.) требует только новой реализации `AIProvider`, без изменений в `worker`, БД-слое или MTProto-части.

### 10.3 Ollama-реализация (этап 1)

```python
class OllamaProvider:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    async def process(self, text: str) -> dict:
        response = await http_client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": build_prompt(text),
                "format": AIPostResult.model_json_schema(),  # JSON Schema, не просто "json"
                "stream": False,
            },
        )
        return json.loads(response.json()["response"])
```

Передача полноценной JSON Schema в поле `format` (а не строки `"json"`) заставляет Ollama ограничивать генерацию под структуру схемы (constrained decoding) начиная с Ollama ≥0.5 — источник: [Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs), [Ollama API: Generate a response](https://docs.ollama.com/api/generate).

### 10.4 Retry-политика ИИ-обработки

```
attempts = task.attempts
if attempts >= task.max_attempts:
    UPDATE parser_tasks SET status='failed'
    UPDATE parser_posts SET status='failed', error_text=...
else:
    delay = AI_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempts)   # экспоненциальная задержка
    UPDATE parser_tasks SET attempts=attempts+1, scheduled_at=now()+delay, status='new'
```

Пост не теряется при любом количестве неудачных попыток ИИ — таблица `parser_ai_results` создаётся только при успешной валидации; неуспешные попытки фиксируются исключительно в `parser_tasks.error_text`.

---

## 11. Backend API (просмотр и управление)

Все эндпоинты требуют заголовок `Authorization: Bearer {API_AUTH_TOKEN}` (простой статический токен на MVP; переход на JWT/несколько пользователей — расширение без изменения схемы БД). Отдельный потребитель этого же API — backend бота закупок, вызывающий `GET /posts` с отдельным сервисным токеном (`PARSER_API_TOKEN` в его `.env`) — см. `TECHNICAL_DOCUMENTATION.md`, алгоритм 9.5.1.

| Метод | Путь | Действие |
|---|---|---|
| `GET` | `/channels` | список отслеживаемых каналов (JSON-массив) |
| `GET` | `/channels?purpose=` | фильтр `monitoring` / `supplier_price_source` |
| `POST` | `/channels` | регистрация канала: `{"handle":"@x","purpose":"supplier_price_source"}` → 202, `status: pending` |
| `DELETE` | `/channels/{id}` | деактивация (`id` = PK `parser_channels.id`) |
| `GET` | `/posts?channel_id=&content_type=&from=&limit=&cursor=` | paginated: `{"items":[...],"next_cursor":...}`; `from` по `post_date` |
| `GET` | `/posts/{id}` | один пост |
| `GET` | `/health` | без auth: `{"status":"ok","db":"ok\|error"}` |

---

## 12. Docker Compose (production)

Файл: `docker-compose.tg-parser.yml`. Coolify: base directory `tg-channel-parser/`, **без domains**, env через UI.

Сервисы: `tg-parser-postgres`, `tg-parser-migrate` (one-shot Alembic), `tg-listener`, `tg-worker`, `tg-parser-api`.

Host ports **не** публикуются. Ollama в текущем MVP **не** включён.

Подробный prod runbook: **`СЕРВИСЫ.md`**.

```yaml
# Упрощённая схема (см. актуальный файл в репо)
services:
  tg-parser-postgres:
    image: postgres:15
    # без ports:
  tg-parser-migrate:
    command: ["alembic", "upgrade", "head"]
    restart: "no"
  tg-listener:
    command: ["python", "-m", "app.mtproto.realtime"]
  tg-worker:
    command: ["python", "-m", "app.worker.task_worker"]
  tg-parser-api:
    # internal :8000, healthcheck /health
```

`tg-listener` для режима backfill запускается разово отдельной командой (не как постоянный сервис): `docker compose run --rm tg-listener python -m app.mtproto.backfill --channel @my_channel --from-date 2026-01-01`.

Redis/Celery — не включены; при необходимости добавляется сервис `redis:7` и `tg-worker` переключается с polling на очередь без изменения таблицы `parser_tasks` (она может использоваться как журнал состояния параллельно с очередью).

---

## 13. `.env.example`

```env
# Telegram MTProto (my.telegram.org)
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE_NUMBER=            # нужен только при первичной авторизации (auth_cli.py)
TELEGRAM_2FA_PASSWORD=            # нужен только при первичной авторизации, если включена 2FA
TELEGRAM_SESSION_STRING=          # заполняется вручную после auth_cli.py, обязателен для listener/worker

# Целевой канал
TARGET_CHANNEL=                   # @username или numeric channel_id

# База данных
POSTGRES_USER=
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@tg-parser-postgres:5432/tg_parser

# Хранилище медиа
MEDIA_STORAGE_BACKEND=local        # local | s3
MEDIA_LOCAL_PATH=/data/media
S3_ENDPOINT_URL=
S3_BUCKET=
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_REGION=

# ИИ-модуль
AI_PROVIDER=ollama                 # ollama | (внешний провайдер — добавляется отдельным классом)
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.1
AI_MAX_RETRIES=3
AI_RETRY_BACKOFF_BASE_SECONDS=5

# Backend API
API_AUTH_TOKEN=

# Прочее
TZ=Europe/Moscow
LOG_LEVEL=INFO
```

Файл `.gitignore` проекта обязательно должен содержать:

```
.env
*.session
*.session-journal
```

---

## 14. Логирование и обработка ошибок

### 14.1 Структурные логи

Формат — JSON-строки (например, через `structlog` или `logging` + `python-json-logger`): `timestamp`, `level`, `event`, `post_id`/`task_id` где применимо, без свободного текста с потенциальными секретами.

### 14.2 Защита от утечки секретов в логах

Обязательный фильтр логирования, маскирующий значения из списка секретных переменных окружения (`TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`, `TELEGRAM_2FA_PASSWORD`, `S3_SECRET_KEY`, `API_AUTH_TOKEN`, `POSTGRES_PASSWORD`) — перед записью строки лога в неё подставляется `***`, если обнаружено точное совпадение со значением секрета.

### 14.3 Обработка ошибок Telegram (FloodWait)

```python
from pyrogram.errors import FloodWait

try:
    ...
except FloodWait as e:
    await asyncio.sleep(e.value)   # Pyrogram передаёт число секунд ожидания в исключении
    # повторить операцию после ожидания
```

Источник (для Telethon-аналога `FloodWaitError`): [Telethon RPC Errors](https://docs.telethon.dev/en/stable/concepts/errors.html).

### 14.4 Сетевые ошибки и недоступность ИИ

Единая retry-обёртка (например, через `tenacity`) с экспоненциальной задержкой для: сетевых ошибок MTProto-клиента, HTTP-ошибок при обращении к Ollama/внешнему API, ошибок скачивания медиа. Максимум попыток и базовая задержка — из `.env` (`AI_MAX_RETRIES`, `AI_RETRY_BACKOFF_BASE_SECONDS`); после исчерпания попыток — `status='failed'`, `error_text` заполнен, исходный пост остаётся в БД без изменений.

---

## 15. Миграции и инструкция запуска

```bash
# 1. Клонирование и настройка окружения
cp .env.example .env
# заполнить TELEGRAM_API_ID, TELEGRAM_API_HASH, TARGET_CHANNEL, POSTGRES_*, AI_*

# 2. Первичная авторизация (разово, интерактивно, НЕ в Docker с detached-режимом)
# compose требует непустой TELEGRAM_SESSION_STRING ещё до auth_cli:
# в .env поставить TELEGRAM_SESSION_STRING=placeholder, затем:
docker compose -f docker-compose.tg-parser.yml run --rm -it --no-deps \
  tg-listener python -m app.mtproto.auth_cli
# скопировать выведенный session_string в .env и Coolify → TELEGRAM_SESSION_STRING

# 3. Применение миграций
docker compose run --rm tg-parser-api alembic upgrade head

# 4. Первичная загрузка истории (опционально)
docker compose run --rm tg-listener python -m app.mtproto.backfill \
  --channel "${TARGET_CHANNEL}" --from-date 2026-01-01

# 5. Запуск постоянных сервисов
docker compose up -d tg-listener tg-worker tg-parser-api ollama

# 6. Проверка
curl http://localhost:8081/health
```

Пул моделей Ollama нужно подготовить отдельно: `docker compose exec ollama ollama pull llama3.1` (или выбранная модель из `OLLAMA_MODEL`).

---

## 16. Данные, которые понадобятся от вас

1. `api_id` и `api_hash` личного аккаунта — выдаются на [my.telegram.org/apps](https://my.telegram.org/apps) после входа тем же номером телефона, что состоит в канале.
2. Номер телефона аккаунта (только для одноразового шага авторизации) и, если включена двухфакторная аутентификация, пароль (тоже только для этого шага).
3. `@username` личного канала (`purpose='monitoring'`) или его `channel_id` — точный идентификатор канала для чтения.
3a. Список `@username`/`channel_id` каналов поставщиков (`purpose='supplier_price_source'`), если бот закупок будет собирать цены из каналов (`TECHNICAL_DOCUMENTATION.md`, 9.5.1) — с привязкой каждого канала к конкретному поставщику из таблицы `suppliers` бота закупок.
4. Подтверждение, что личный аккаунт уже состоит в каждом из перечисленных каналов (личном и поставщиков) и не ограничен в чтении истории (для приватных каналов история видна только с момента, когда аккаунт стал участником, если иное не оговорено настройками канала). Для каналов поставщиков, в которых аккаунт пока не состоит, его нужно вступить/быть добавленным туда до запуска backfill/realtime.
5. Период/начальная точка для первичной загрузки истории: конкретная дата или `message_id`, откуда начинать backfill (отдельно для личного канала и отдельно для каждого канала поставщика, если нужна история цен).
5a. Для каждого канала поставщика — подтверждение, что поставщик публикует цены текстом (не сканом/фото) — иначе автоматический разбор не сработает (раздел 1).
6. Решение по хранилищу медиа: локальный диск сервера или S3-совместимое хранилище (если S3 — эндпоинт, бакет, ключи доступа).
7. Хост и модель Ollama (если локальный сервер уже есть — адрес и уже установленная модель; если нет — какую модель разворачивать) либо, если решено сразу использовать внешний API вместо Ollama — провайдер и ключ.
8. Значение `API_AUTH_TOKEN` для защиты backend API (или предпочтение перейти на JWT с несколькими пользователями).
9. Часовой пояс сервера/каналов для корректной интерпретации `post_date` в отчётах и фильтрах API.
10. Список категорий для поля `category` в результатах ИИ, если нужна фиксированная таксономия, а не свободная классификация моделью.

---

## 17. Явно вне скоупа этого сервиса

- Запись в канал, реакции, комментарии — сервис только читает.
- Каналы, где аккаунт не состоит, или получение доступа обходом ограничений Telegram.
- Гарантия чтения истории канала до момента присоединения аккаунта, если Telegram/администратор канала это ограничивает.
- Замена MTProto-клиента на ИИ-модуль в любой роли, связанной с доступом к Telegram.
