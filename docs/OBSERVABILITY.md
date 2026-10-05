# Наблюдаемость

Пороги ниже — стартовые значения для вашего мониторинга. Это не SLO и не включённые алерты.

## Пробы

| Путь | Смысл |
| --- | --- |
| `GET /live` | Процесс жив, база не проверяется. Этим путём смотрит Docker healthcheck. |
| `GET /health` | Старый ответ `{status, db}`. Всегда HTTP 200, чтобы не сломать уже настроенные пробы. |
| `GET /ready` | HTTP 200 только если база отвечает, ревизия Alembic совпадает с кодом, и (когда включено) свежие heartbeat worker и scheduler. Иначе HTTP 503. |
| `GET /health/details` | Те же данные плюс мёртвая очередь и последние cron. Заголовок `X-Internal-Token` равен `HEALTH_DETAILS_TOKEN` или `WEBHOOK_SECRET`. |
| `GET /metrics` | Текст Prometheus без идентификаторов заявок, текста сообщений и секретов. |

`/ready` требует heartbeat worker, когда `WEBHOOK_ASYNC_ENABLED=true`. Heartbeat scheduler требуется, когда `SCHEDULER_ENABLED=true` или `SCHEDULER_EXPECTED=true`. Свежесть — `HEARTBEAT_STALE_SECONDS` (по умолчанию 90).

## Метрики

`zakupki_webhook_total`, `zakupki_webhook_errors`, `zakupki_inbox_pending`, `zakupki_inbox_dead`, `zakupki_outbox_pending`, `zakupki_outbox_uncertain`, `zakupki_inbox_oldest_pending_seconds`, `zakupki_worker_heartbeat_age_seconds`, `zakupki_scheduler_heartbeat_age_seconds`.

Значение `-1` у возраста heartbeat значит, что процесс ещё не отметился.

Стартовые пороги, которые оператор заводит сам:

- `zakupki_inbox_dead` > 0 дольше 15 минут
- `zakupki_outbox_uncertain` > 0 дольше 15 минут
- `zakupki_inbox_oldest_pending_seconds` > 300
- возраст heartbeat worker или scheduler > 120, если этот процесс должен работать

## Меню

Владелец: Меню → Ошибки системы. Там видны необработанные `dead` входа и `dead`/`uncertain` выхода: номер, статус, попытки, заявка, поставщик, укороченная ошибка без секретов. Повтор исходящего спрашивает подтверждение, потому что сообщение уйдёт снова. `uncertain` для `ask` и `bargain` без подтверждения в очередь не возвращается. «Закрыть без повтора» пишет `resolved_at` и строку в `admin_audit_log`.

## Процессы

- `backend` — HTTP. Cron внутри него работает, пока `SCHEDULER_ENABLED=true`.
- `backend-worker` — `python -m app.workers.main`
- `backend-scheduler` — `python -m app.scheduler`

Если поднят отдельный scheduler, на web поставьте `SCHEDULER_ENABLED=false` и `SCHEDULER_EXPECTED=true`. Иначе утренний прайс может запуститься в двух процессах; второй пропустит работу из-за advisory lock, но так оставлять не нужно.
