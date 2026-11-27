# Публикация клиентской цены

## Поведение

- Каждый запрос уходит поставщикам. Сегодняшний кэш `daily_sku_prices` цену в чат заказчика не подставляет.
- Первый подходящий ответ по этой заявке публикуется сразу: закупочная цена плюс наценка из `markup_rules`. Поздние копятся внутри. Более низкая поздняя цена даёт сотруднику уведомление с кнопками. Замена цены у заказчика — только после повторного подтверждения.
- Полночь не отзывает уже отправленную цену. Исторические котировки не удаляются.
- Пакетная заявка (многострочный `/ask`, `batch_id`): при `AUTO_PUBLISH_FIRST_VALID_PRICE=true` первая валидная цена публикуется так же, как по одиночной. При выключенном флаге поштучные цены в чат не уходят — клиенту отправляется только сводный прайс кнопкой «Опубликовать» (`publish_batch_to_client`).

## Топология процессов (обязательно)

Клиентская цена ставится в `telegram_outbox` (`kind='client_price'`, `status='pending'`). Отправляет её процесс с арендой строки: отдельный `backend-worker` и, при `CLIENT_PRICE_INLINE_DELIVERY=true` (по умолчанию), тот же цикл внутри web. Две доставки одной строки не будет: захват через `FOR UPDATE SKIP LOCKED`. Флаг `OUTBOX_DELIVERY_ENABLED` на клиентскую цену не влияет.

| Процесс | Команда | Назначение |
| --- | --- | --- |
| web | `uvicorn app.main:app` | webhook, jobs, API |
| **backend-worker** | `python -m app.workers.main` | доставка `telegram_outbox` (включая клиентские цены), обработка inbox |
| scheduler | `python -m app.scheduler` | cron-задачи |

При `AUTO_PUBLISH_FIRST_VALID_PRICE=true` и `CLIENT_PRICE_INLINE_DELIVERY=false` без живого воркера цена помечается опубликованной в БД (`request_price_selections`, `price_state=published`), но **в Telegram не уходит**. С включённой inline-доставкой web сам забирает очередь и пишет heartbeat `worker`, поэтому `/ready` не требует отдельного процесса. `/health/details` показывает `pending_client_prices` и `oldest_client_price_age_seconds`.

## Флаги

| Переменная | По умолчанию | Эффект |
| --- | --- | --- |
| `AUTO_PUBLISH_FIRST_VALID_PRICE` | `false` | Kill switch автопубликации |
| `CLIENT_PRICE_INLINE_DELIVERY` | `true` | Web сам забирает `telegram_outbox`, если отдельный worker остановлен |
| `CLIENT_PRICE_PUBLICATION_POLICY` | `first_valid_today` | Политика выбора |
| `CONTINUE_COLLECTING_AFTER_PUBLICATION` | `true` | Сбор цен после публикации |
| `NOTIFY_EMPLOYEE_ON_BETTER_LATE_PRICE` | `true` | Уведомление о более низкой поздней цене |
| `DAILY_PRICE_REUSE_ENABLED` | `true` | Обновляет дневную проекцию SKU; в чат заказчика не подставляется |
| `MANUAL_PRICE_USE_MARKUP` | `true` | Ручная кнопка «▶ Запустить прайс»: минимум между каналами + наценка, как в кроне |
| `CHANNEL_SKU_PRICE_PROJECTION_ENABLED` | `false` | Наполнять проекцию `channel_sku_prices` (цены каналов в канонических ключах заявок) |
| `CHANNEL_PRICE_OFFER_ENABLED` | `false` | Карточка «цена из прайс-канала» сотруднику по входящей заявке |
| `CHANNEL_PRICE_MAX_AGE_MINUTES` | `1440` | Возраст снимка канала, после которого оффер не предлагается и не подтверждается |
| `PRICE_PUBLISH_VIA_OUTBOX` | `false` | Утверждённый прайс публикуется через `telegram_outbox` вместо прямой отправки |

## Утверждённый прайс: черновик → канал публикации

Цели публикации — таблица `price_publish_channels` (экран меню «Публикация
прайса»: добавить / выкл. / удалить). `PRICE_PUBLISH_CHAT_IDS` остаётся
fallback: при первом обращении строки из env переносятся в таблицу
(`seed_publish_channels_from_env`), поэтому выключенный в боте канал не
включится снова после рестарта.

- Владелец жмёт «✅ Утвердить» → `approve_price_draft` собирает
  `price_list_published_html` (дата по бизнес-дню `TZ`, группы по категориям
  дерева наценок, суммы в формате `47 000 ₽`, текст канала экранирован).
- `PRICE_PUBLISH_VIA_OUTBOX=false` (по умолчанию): прямая отправка, как раньше;
  частичный сбой даёт `approved_partial`.
- `PRICE_PUBLISH_VIA_OUTBOX=true`: на каждую активную цель ставится одна строка
  `telegram_outbox` (`kind='price_publish'`, `parse_mode='HTML'`,
  `dedupe_key='price_publish:{draft_id}:{chat_id}'`), итог — `approved_queued`.
  Сбой уходит в ретрай и DLQ вместо тихой потери (история OPS-008).
- Ни в `price_list_drafts.items`, ни в публикуемое сообщение не попадают закуп,
  наценка и имя поставщика — происхождение цены владелец видит отдельным
  сообщением «Происхождение цен» (`review_sources`).
- Если активных каналов нет, прайс не публикуется молча: в очереди нет строк,
  операторам уходит предупреждение, а экран «Публикация прайса» показывает,
  что добавить.

Проверка застрявшей публикации:

```sql
SELECT id, chat_id, status, attempts, last_error, created_at
FROM telegram_outbox
WHERE kind = 'price_publish' AND status IN ('pending','processing','dead')
ORDER BY created_at;
```

## Оффер цены из прайс-канала по заявке

`CHANNEL_SKU_PRICE_PROJECTION_ENABLED` + `CHANNEL_PRICE_OFFER_ENABLED`:

- по созданию заявки (`/ask`, пакетный `/ask`) бот ищет свежую цену позиции в
  `channel_sku_prices` и ставит сотруднику ephemeral-карточку «цена из
  прайс-канала» с кнопками «Отправить клиенту» / «Отмена»;
- до нажатия кнопки в клиентский чат не уходит ничего: пишется только строка
  `request_price_selections` со `status='proposed'`, `price_state` не
  меняется (`tests/test_channel_price_no_auto_publish.py`);
- по «Отправить клиенту» цена проверяется повторно (свежесть, та же сумма) и только
  потом ставится в очередь клиентского сообщения
  (`kind='client_price'`, `dedupe_key='request:{id}:first-client-price'`); устаревшая
  или заменённая цена даёт `stale` / `already` вместо отправки.

При выключенном флаге (`AUTO_PUBLISH_FIRST_VALID_PRICE=false`):

- В группу **публично** уходит только **первая** котировка с ценой по заявке (шаблон без поставщика).
- Каждая **следующая** котировка, исправление и смена цены — только сотрудникам и владельцам через **Ephemeral Messages** в том же чате (закуп и поставщик не видны остальным участникам). Подробно: `docs/STAFF_EPHEMERAL_PRICES.md`.

## Деплой

1. Бэкап БД.
2. Деплой с `AUTO_PUBLISH_FIRST_VALID_PRICE=false`.
3. `alembic upgrade head`. Текущий head backend — `0035_price_publish_channels`. Последние четыре (`0032_parsed_sku_key_slugs`, `0033_price_draft_review_sources`, `0034_channel_sku_prices`, `0035_price_publish_channels`) добавляют колонки и таблицы, поведение по умолчанию не меняют.
4. `python -m scripts.ensure_owners --telegram-id <id>`.
5. Владелец пишет боту `/start`, проверка `/menu`.
6. Проверка: вчерашняя цена не предлагается, повторная публикация не создаёт второе сообщение.
7. Доставка. При `CLIENT_PRICE_INLINE_DELIVERY=true` (по умолчанию) отдельный worker не обязателен: web забирает `telegram_outbox` сам. Поднимать `backend-worker` (`python -m app.workers.main`, те же `DATABASE_URL` и `TELEGRAM_BOT_TOKEN`) нужно, если inline-доставка выключена или включён любой из `WEBHOOK_ASYNC_ENABLED`, `AUTO_PUBLISH_FIRST_VALID_PRICE`, `PRICE_PUBLISH_VIA_OUTBOX`, `CHANNEL_PRICE_OFFER_ENABLED`. Без живого дренажа `/ready` отдаёт 503 `worker`, а `approve_price_draft` при outbox и выключенном inline возвращает `delivery_unavailable` и не ставит черновик в `approved`.
8. Включить `AUTO_PUBLISH_FIRST_VALID_PRICE=true` (Coolify env → Redeploy web и worker).

## Проверка после включения

1. Тестовый запрос из группы → первый ответ поставщика → в группе ровно одно **публичное** сообщение с клиентской ценой. Второй поставщик → публичного дубликата нет; у сотрудника/владельца ephemeral с закупом (см. `docs/STAFF_EPHEMERAL_PRICES.md`). При включённой автопубликации первая карточка — из outbox, правило для второй цены то же.
2. SQL-контроль застрявших публикаций:

```sql
SELECT id, request_id, chat_id, status, attempts, last_error, created_at
FROM telegram_outbox
WHERE kind = 'client_price' AND status IN ('pending','processing','dead')
ORDER BY created_at;
```

Пустая выборка — норма. Строки в `pending` дольше пары минут — воркер не работает.
3. `GET /ready` → 200; при мёртвом воркере → 503 `reasons:["worker"]`.
4. `GET /health/details` → `pending_client_prices`, `oldest_client_price_age_seconds`, `pending_price_publishes`, `oldest_price_publish_age_seconds`, `price_publish_via_outbox`; метрика `zakupki_outbox_pending` (пороги — `docs/OBSERVABILITY.md`).

## Откат

Без даунграда схемы: `PRICE_PUBLISH_VIA_OUTBOX=false`, `CHANNEL_PRICE_OFFER_ENABLED=false`, `CHANNEL_SKU_PRICE_PROJECTION_ENABLED=false`, `MANUAL_PRICE_USE_MARKUP=false` возвращают поведение до этой работы (публикация прямым `send_message` по env, карточек «цена из прайс-канала» нет, ручная кнопка — как раньше).

`AUTO_PUBLISH_FIRST_VALID_PRICE=false` останавливает автопубликацию. Сбор цен и карточки продолжают работать. Откат только серии прайс-каналов с head `0035_price_publish_channels`: из `backend/` выполнить `alembic downgrade 0031_parsed_sku_key_canonical`. Откат до `0027_supplier_ai_decisions` дополнительно снимает `0035`…`0028`, включая `price_publish_channels`, `channel_sku_prices`, `review_sources` и `raw_prices.post_date`. Номер версии внутри одной заявки сериализуется `pg_advisory_xact_lock(CAST(hashtext(key) AS bigint))`.
