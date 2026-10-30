# Публикация клиентской цены

## Поведение

- Есть валидная цена текущего московского дня → клиенту уходит наименьшая, RFQ не отправляется.
- Нет → RFQ; первый валидный ответ публикуется сразу; поздние копятся внутри. Более низкая поздняя цена даёт сотруднику уведомление с кнопками. Замена цены — только после повторного подтверждения.
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
| `DAILY_PRICE_REUSE_ENABLED` | `true` | Переиспользование сегодняшней цены |

При выключенном флаге каждая котировка уходит в клиентскую группу как раньше.

## Деплой

1. Бэкап БД.
2. Деплой с `AUTO_PUBLISH_FIRST_VALID_PRICE=false`.
3. `alembic upgrade head` (ревизия `0028_price_publication`).
4. `python -m scripts.ensure_owners --telegram-id <id>`.
5. Владелец пишет боту `/start`, проверка `/menu`.
6. Проверка: вчерашняя цена не предлагается, повторная публикация не создаёт второе сообщение.
7. **Поднять `backend-worker`** (отдельный Coolify-сервис, команда `python -m app.workers.main`, те же `DATABASE_URL` и `TELEGRAM_BOT_TOKEN`). Без него шаг 8 приведёт к «молчаливой» публикации.
8. Включить `AUTO_PUBLISH_FIRST_VALID_PRICE=true` (Coolify env → Redeploy web и worker).

## Проверка после включения

1. Тестовый запрос из группы → первый ответ поставщика → в группе ровно одно сообщение с клиентской ценой; вторая цена сообщение не дублирует.
2. SQL-контроль застрявших публикаций:

```sql
SELECT id, request_id, chat_id, status, attempts, last_error, created_at
FROM telegram_outbox
WHERE kind = 'client_price' AND status IN ('pending','processing','dead')
ORDER BY created_at;
```

Пустая выборка — норма. Строки в `pending` дольше пары минут — воркер не работает.
3. `GET /ready` → 200; при мёртвом воркере → 503 `reasons:["worker"]`.
4. `GET /health/details` → `pending_client_prices`, `oldest_client_price_age_seconds`; метрика `zakupki_outbox_pending` (пороги — `docs/OBSERVABILITY.md`).

## Откат

`AUTO_PUBLISH_FIRST_VALID_PRICE=false` останавливает автопубликацию. Сбор цен и карточки продолжают работать. `alembic downgrade 0027` убирает новые колонки, строки `request_price_selections` сохраняются.
