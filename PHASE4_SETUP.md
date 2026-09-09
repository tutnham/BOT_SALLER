# Phase 4 — что добавить в проект

После Phase 4 утренний прайс работает только если заполнены переменные окружения и данные в БД. Код уже есть — нужен конфиг и операционные данные.

---

## 1. Добавить в `.env` (корень репозитория)

Скопируй из `.env.example` или допиши вручную:

```env
# Куда слать черновик прайса на approve/reject (CSV, предпочтительно)
PRICE_APPROVAL_CHAT_IDS=
# Legacy: один chat_id, если CHAT_IDS пустой
PRICE_APPROVAL_CHAT_ID=

# Куда публиковать прайс после /approve_price (несколько chat_id через запятую)
PRICE_PUBLISH_CHAT_IDS=

# Наценка по умолчанию (₽), если у правила нет markup_fixed
DEFAULT_MARKUP=500

# Таймаут HTTP к сервису-парсеру (секунды)
PARSER_TIMEOUT_SECONDS=15

# URL и токен API парсера каналов (Docker-сеть Coolify или UUID-hostname — см. СЕРВИСЫ.md)
PARSER_API_URL=http://<parser-api-uuid-or-tg-parser-api>:8000
PARSER_API_TOKEN=

# LLM (backend Coolify). Не LLM_PROVIDER=deepseek — см. СЕРВИСЫ.md § LLM
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_KEY=
```

Порядок чатов approve: `PRICE_APPROVAL_CHAT_IDS` → иначе `PRICE_APPROVAL_CHAT_ID` → иначе `ADMIN_ALERT_CHAT_ID`. Дополнительно draft уходит в ЛС всех `owners` с `dm_ok=true`.

---

## 2. Что значат поля

| Переменная | Зачем |
|---|---|
| `PRICE_APPROVAL_CHAT_IDS` | CSV chat_id, куда бот шлёт черновик (`/approve_price`, `/reject_price`) |
| `PRICE_APPROVAL_CHAT_ID` | Legacy: один chat_id, если CSV пустой |
| `PRICE_PUBLISH_CHAT_IDS` | Список chat_id (CSV), куда публикуется утверждённый прайс. Пример: `-100111,-100222` |
| `DEFAULT_MARKUP` | Базовая наценка в рублях, если в `markup_rules` нет `markup_fixed` |
| `PARSER_TIMEOUT_SECONDS` | Сколько ждать ответ парсера каналов |
| `PARSER_API_URL` | Базовый URL сервиса `tg-channel-parser` (**порт 8000**, не 8100). В Coolify с Predefined Network — UUID-hostname контейнера |
| `PARSER_API_TOKEN` | Bearer-токен для `GET /posts` (тот же, что `API_AUTH_TOKEN` у парсера) |
| `LLM_PROVIDER` | Только `openai` / `openai_compatible` / `openrouter` / `ollama`. Не `deepseek` |
| `LLM_BASE_URL` | Для DeepSeek: `https://api.deepseek.com` (код дописывает `/chat/completions`) |
| `LLM_MODEL` | Например `deepseek-chat` |
| `LLM_API_KEY` | Ключ провайдера; только Coolify, не git |

Как узнать `chat_id`: добавь бота в нужный чат/канал и посмотри update (или используй бота вроде `@userinfobot` / логи webhook).

---

## 3. Данные в БД (обязательно)

Без этих строк пайплайн отработает «пусто» или без наценки.

### 3.1. Правила наценки — таблица `markup_rules`

Минимум одно активное правило с категорией `*` (fallback). Актуальные фиксированные наценки:

```sql
INSERT INTO markup_rules (category, markup_fixed, markup_min, markup_max, active)
VALUES
  ('iphone_17_pro', 800, NULL, NULL, true),
  ('iphone', 500, NULL, NULL, true),
  ('airpods', 500, NULL, NULL, true),
  ('apple_watch', 500, NULL, NULL, true),
  ('ipad', 500, NULL, NULL, true),
  ('macbook', 800, NULL, NULL, true),
  ('samsung_s26_ultra', 800, NULL, NULL, true),
  ('samsung', 500, NULL, NULL, true),
  ('playstation', 800, NULL, NULL, true),
  ('dyson', 800, NULL, NULL, true),
  ('*', 500, NULL, NULL, true);
```

Логика:
- есть `markup_fixed` → берём его;
- иначе → `DEFAULT_MARKUP`, зажатый в `[markup_min, markup_max]`;
- нет правила категории и нет `*` → SKU в draft не попадает.

### 3.2. Поставщики с каналом цен

Для тех, у кого прайс в Telegram-канале:

```sql
UPDATE suppliers
SET
  price_channel_id = <channel_id>,
  price_channel_username = '@channel_username'
WHERE id = <supplier_id>;
```

`price_channel_id` должен совпадать с каналом, уже зарегистрированным в **tg-channel-parser** с `purpose='supplier_price_source'`.

Поставщики **без** канала шлют прайс в ЛС боту сообщением, начинающимся с:

- `прайс`
- `#прайс`
- `/price`

---

## 4. Сервис-парсер (отдельный)

Backend **не** читает каналы сам. Нужен развёрнутый `tg-channel-parser` (см. **`СЕРВИСЫ.md`** — Coolify Compose, env, auth, сеть):

1. Stack поднят, `GET /health` parser → `db: ok`
2. `PARSER_API_TOKEN` (backend) = `API_AUTH_TOKEN` (parser)
3. Каналы через `/menu` или `POST /channels` с `purpose='supplier_price_source'`
4. MTProto-аккаунт состоит в каналах; `TELEGRAM_SESSION_STRING` в env parser

Пока parser недоступен — manual DM (`прайс`) работает; channel_post → `degraded`, job HTTP 200.

---

## 10. Coolify: parser stack (кратко)

1. New **Service Stack** → repo `tutnham/BOT_SALLER`, base `tg-channel-parser/`, compose `docker-compose.tg-parser.yml`
2. Env — см. `СЕРВИСЫ.md`; **без domains**
3. `auth_cli` → `TELEGRAM_SESSION_STRING` → redeploy
4. **Connect to Predefined Network** на parser + backend
5. UUID-hostname → backend `PARSER_API_URL=http://<uuid>:8000`
6. Smoke: `tg-channel-parser/scripts/smoke-from-backend.sh`

---

## 5. Webhook и планировщик

Telegram webhook теперь ведёт прямо в backend: `POST /telegram/webhook` защищён `X-Telegram-Bot-Api-Secret-Token`, а ручные вызовы `/jobs/*` продолжают использовать `X-Webhook-Secret`.

Проверь:
- `PUBLIC_BACKEND_URL` указывает на публичный HTTPS backend;
- `WEBHOOK_SECRET` заполнен;
- `TELEGRAM_WEBHOOK_SECRET_TOKEN` заполнен;
- cron задачи запускаются внутри backend через APScheduler.

---

## 6. Быстрый чеклист

- [ ] В `.env` заполнены `PRICE_APPROVAL_CHAT_IDS` (или legacy `PRICE_APPROVAL_CHAT_ID`) и `PRICE_PUBLISH_CHAT_IDS`
- [ ] LLM: `openai_compatible` + `LLM_BASE_URL` провайдера + Restart backend после правки Coolify
- [ ] Задан `PARSER_API_TOKEN` (если нужны каналы)
- [ ] В БД есть активные `markup_rules` (хотя бы `*`)
- [ ] У нужных поставщиков заполнен `price_channel_id` **или** они шлют `прайс` в ЛС
- [ ] Парсер развёрнут и подписан на каналы (для channel_post)
- [ ] APScheduler job на morning-price активна
- [ ] Бот добавлен в чаты approve и publish

---

## 8. Security/Deploy доп. требования

- Backend и PostgreSQL должны быть доступны только локально (`127.0.0.1`) и публиковаться наружу через reverse proxy.
- Применить конфиг `deploy/nginx.conf.example` (rate limits для `/telegram/webhook` и `/jobs/*`, `client_max_body_size 256k`).
- Использовать только непустые секреты в `.env` (`POSTGRES_PASSWORD`, `WEBHOOK_SECRET`, `TELEGRAM_WEBHOOK_SECRET_TOKEN`).
- Перед запуском в проде применить миграции: `alembic upgrade head`.

---

## 9. Текущий статус parser-сервиса

Код **`tg-channel-parser/`** в репозитории. Prod на Coolify (smoke 2026-09-06): stack Running, migrate `0002` после `606d9ab`+`83d4bca`, сеть `coolify`, smoke из bot-saller успешен, каналов `[]`.

Публичная проверка 2026-09-09: backend `/health` `db: ok`. LLM-ключи в Coolify **не** равны рабочему парсингу, пока `LLM_PROVIDER` не `openai_compatible` и контейнер не перезапущен. Осталось для morning-price: канал active, `price_channel_id`, `markup_rules`, чаты approve/publish. Подробно — `СЕРВИСЫ.md` § «Готовность».

MVP: `supplier_price_source`, worker = resolve + download_media. Ollama/AI — вне текущего prod scope.

`auth_cli` на VPS: `TELEGRAM_SESSION_STRING=placeholder` + `docker compose … run --rm -it --no-deps tg-listener python -m app.mtproto.auth_cli`. Hostname API после Redeploy обновить в `PARSER_API_URL`.

---

## 7. Как пользоваться после настройки

1. В 11:00 МСК APScheduler запускает `morning_price` (после окна публикации в каналах 08:00–11:00).
2. Черновик приходит в `PRICE_APPROVAL_CHAT_IDS` и в ЛС владельцев с `dm_ok`.
3. Сотрудник пишет:
   - `/approve_price {id}` — публикация в `PRICE_PUBLISH_CHAT_IDS`
   - `/reject_price {id}` — отклонение
