# Как включить канал Telegram Business (пошагово)

Код уже в репозитории. Миграция лежит здесь (скачивать отдельно не нужно):

`backend/app/migrations/versions/0008_business_connections.py`

Её применяет команда `alembic upgrade head` из контейнера backend. Новых секретов в Coolify / `.env` нет.

Ниже — всё, что делается руками: Git/Coolify, Supabase, Telegram.

---

## Что уже сделано в коде

- Бот умеет слать RFQ от имени Premium-аккаунта клиента (`sendMessage` + `business_connection_id`).
- Входящие `business_message` от известных поставщиков идут в тот же разбор котировок, что и обычные ответы.
- Группы, личка через `/start` и `dm_ok` не отключаются.

Пока не сделаны шаги ниже, канал в проде молчит.

---

## Шаг 1. Код на GitHub и деплой Coolify

Прод `bot-saller` в Coolify смотрит ветку **`main`**.

1. Убедитесь, что коммиты Business попали в `origin/main` (после push это делает разработчик/агент).
2. Coolify → приложение **`bot-saller`** (`pj7j3j6gr0wgv1qwusce1ozi`) → **Redeploy** (или дождитесь автодеплоя с `main`).
3. Дождитесь, пока контейнер станет Running.
4. Проверка: откройте в браузере

   `https://pj7j3j6gr0wgv1qwusce1ozi.80.90.179.114.sslip.io/health`

   Ожидание: `{"status":"ok","db":"ok"}`.

Если health красный — не идите дальше, смотрите логи Coolify.

---

## Шаг 2. Миграция в Supabase (таблица `business_connections`)

Файл миграции сам в Supabase не «кладётся». Его выполняет Alembic внутри контейнера, по `DATABASE_URL`.

1. В Supabase: **Project Settings → Database** — запомните, что приложение ходит в Postgres.
2. Сделайте backup / snapshot проекта (Dashboard → Database → Backups, или свой snapshot).
3. Проверьте `DATABASE_URL` в Coolify у `bot-saller`:
   - нужен **session** или **direct** (порт **5432**);
   - **нельзя** transaction pooler порт **6543** (Alembic ломает DDL).
   Если сейчас уже 5432 / session — ничего не меняйте.
4. Coolify → `bot-saller` → **Execute Command** (не новый сервис, не новый stack):

   ```text
   alembic upgrade head
   ```

   Рабочая папка в образе — `/app`. Команда должна напечатать что-то вроде:

   `Running upgrade 0007_... -> 0008_business_connections`

5. Проверка в Supabase → **Table Editor**: таблица `business_connections` есть.  
   Table Editor / PostgREST для этой таблицы не открывайте anon-ключами: backend ходит SQLAlchemy + service role, RLS без политик для PostgREST — так и задумано.

Если в логе `Can't locate revision` — в контейнере старый образ, сначала Redeploy (шаг 1), потом снова `alembic upgrade head`.

---

## Шаг 3. Telegram: Business Mode у бота

1. Откройте [@BotFather](https://t.me/BotFather).
2. `/mybots` → бот закупок.
3. Включите **Business Mode** (если пункта нет — обновите BotFather / проверьте, что это тот же бот, чей `TELEGRAM_BOT_TOKEN` в Coolify).

Без этого Telegram не шлёт `business_connection` и `business_message`.

---

## Шаг 4. Обновить webhook

Иначе Telegram продолжит слать только старые типы update.

Подставьте значения из Coolify (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET_TOKEN`). URL ниже — текущий прод backend:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  --data-urlencode "url=https://pj7j3j6gr0wgv1qwusce1ozi.80.90.179.114.sslip.io/telegram/webhook" \
  --data-urlencode "secret_token=<TELEGRAM_WEBHOOK_SECRET_TOKEN>" \
  --data-urlencode "allowed_updates=[\"message\",\"edited_message\",\"callback_query\",\"my_chat_member\",\"business_connection\",\"business_message\",\"edited_business_message\",\"deleted_business_messages\"]"
```

Проверка:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

Должно быть:

- `url` = `.../telegram/webhook`
- в `allowed_updates` есть `business_connection` и `business_message`
- нет свежего `last_error_message`

---

## Шаг 5. Подключить бота к Premium-аккаунту клиента

Premium нужен **только на аккаунте клиента**. Поставщикам Premium не нужен.

1. На телефоне клиента: **Настройки → Telegram Business → Chatbots**.
2. Добавьте того же бота, что в Coolify.
3. Область чатов (рекомендация по приватности):
   - **все личные чаты, кроме контактов**, или
   - **только выбранные чаты** (чаты с поставщиками).
4. Дайте боту право **отвечать** (can_reply).

После подключения бот получит update `business_connection`. Проверка: в Telegram напишите боту `/menu` с аккаунта **owner** → кнопка **Business**. Должны быть: аккаунт, `enabled=да`, `can_reply=да`.

---

## Шаг 6. Поставщики и первое сообщение

1. У поставщика в боте должен быть `telegram_id` (`/menu` → поставщик → привязка лички).
2. Поставщик пишет **в личку аккаунту клиента** (не обязательно жмёт `/start` у бота).
3. Первое такое сообщение само создаёт маршрут `business_dm`. Default-каналом его не сделает.
4. Окно ответа Telegram: roughly **24 часа** с последнего входящего от этого человека. Если человек давно не писал клиенту, `/ask` ему не доставится: в группу сотрудников придёт текст «не доставлено». Повторить — новым `/ask` или `/recheck`, когда переписка живая. Автоповтора нет.

Приоритет канала по умолчанию: сначала обычный default-чат (группа / личка бота). Чтобы слать сначала через Business, в таблице `app_settings` ключ:

`routing.business_dm_priority` = `business_dm_first`

Второй ключ (обычно не трогать): `routing.business_dm_autobind_default` = `true` только если первый `business_dm` должен стать default при отсутствии другого default.

---

## Как понять, что всё работает

1. `/menu` → Business: подключение включено, can_reply есть.
2. Поставщик написал клиенту в ЛС.
3. Сотрудник в клиентской группе: `/ask` (или reply + `/ask`).
4. Сообщение уходит **от имени клиента**, в тексте есть `#номер` заявки.
5. Поставщик отвечает reply на это сообщение.
6. В группе сотрудников появляется разобранная котировка.

Если пункт 4 не случился — смотрите сводку «не доставлено» и окно 24 часа.

---

## Чего делать не надо

- Не создавать новый Coolify-сервис и не трогать `tg-channel-parser`.
- Не включать RLS-политики PostgREST для anon на `business_connections`.
- Не класть токены в `docker-compose.yml`.
- Не гонять Alembic через порт **6543**.
