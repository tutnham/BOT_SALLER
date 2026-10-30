# BOT_SALLER

Боты-агенты для сбора, обработки и выдачи информации по закупкам (Zakupki-Bot + tg-channel-parser).

## Репозиторий

| Путь | Сервис |
|---|---|
| `backend/` | Telegram Bot API, webhook, morning-price, Supabase |
| `tg-channel-parser/` | MTProto parser, internal API, Postgres `tg_parser` |

## Документация

- **`СЕРВИСЫ.md`** — production Coolify/VPS, env, deploy parser
- **`PHASE4_SETUP.md`** — morning-price, parser env, чеклист
- **`TG_CHANNEL_PARSER_DOCUMENTATION.md`** — спека parser
- **`Техническая документация  бот для закупок.md`** — спека backend
- **`CLAUDE.md`** / **`AGENTS.md`** — правила для агентов
- **`docs/archive/`** — история (n8n migration, workflow JSON)

## Production (кратко)

- Backend: Coolify app `bot-saller`, Supabase Session pooler IPv4
- Parser: отдельный Coolify Compose stack, **без публичного API**
- Связь: `PARSER_API_URL` + `PARSER_API_TOKEN` (порт **8000**)
- Схема backend: Alembic head `0028_price_publication`. Parser: head `0005_runtime_heartbeat`, процесс `tg-runtime` (listener + worker + heartbeat) и внутренний `tg-parser-api:8000`.
- Webhook идемпотентен: `update_log` пишется до обработки. Повтор Telegram не дублирует действие.
- Быстрые проверки без Postgres: из `backend/` — `uv run pytest tests/test_config.py tests/test_migration_head_contract.py tests/test_jev_policy.py tests/test_jev_classify_unit.py tests/test_jev_client.py tests/test_jev_shadow_eval.py -q`. Полный `pytest` требует `DATABASE_URL` и Postgres. Parser: `uv run pytest -q` из `tg-channel-parser/`.

## Owner `/menu` (ЛС бота)

Владелец: `/menu` → inline-админка:

- **Поставщики** — список, добавление, RFQ, чаты
- **Сотрудники** — добавить менеджера (имя + Telegram ID из @userinfobot или forward), вкл/выкл
- **Клиентские беседы** — привязанные группы для `/ask`
- **Закупки** — отчёт фактических сделок (`/purchase_report today|week|YYYY-MM-DD|batch N`)
- **Новые чаты** — классификация после добавления бота в группу
- **Каналы прайсов** — интеграция с tg-channel-parser

Jev (TypeSafe) — отдельный классификатор ответов поставщика, не замена `LLM_PROVIDER`. По умолчанию выключен. Включение и откат: `docs/JEV_RUNBOOK.md`. Kill switch: `JEV_ENABLED=false` и перезапуск.

Клиентская цена: первая валидная цена текущего дня уходит в чат-источник запроса ровно один раз, если включён `AUTO_PUBLISH_FIRST_VALID_PRICE`. По умолчанию выключено. Сообщение ставится в `telegram_outbox`. При `CLIENT_PRICE_INLINE_DELIVERY=true` (значение по умолчанию) процесс web сам забирает очередь и пишет heartbeat `worker`. Отдельный `backend-worker` (`python -m app.workers.main`) нужен, когда inline-доставка выключена или включён `WEBHOOK_ASYNC_ENABLED`. Поведение и откат: `docs/PRICE_PUBLICATION_RUNBOOK.md`.

Многострочный `/ask` (2+ непустых строк) создаёт пакет `request_batches` с превью. Однострочный `/ask` переиспользует сегодняшнюю цену, если она есть, иначе сразу шлёт RFQ. Система выбирает минимальную допустимую котировку для клиентской цены; закупку сотрудник подтверждает отдельно.

Подробнее: `Техническая документация  бот для закупок.md` (§11.1).

## Owner purge (ЛС бота)

| Команда | Назначение |
|---|---|
| `/purge_request {id}` | Hard-delete заявки и детей (подтверждение кнопкой) |
| `/purge_old {days} [--confirm]` | Batch-очистка cancelled/closed (dry-run по умолчанию) |

Сотрудник `/cancel` только меняет статус; полное удаление — только владелец. Подробнее: §11.2 в технической документации.
