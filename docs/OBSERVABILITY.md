# Наблюдаемость

Пороги ниже — стартовые значения для вашего мониторинга. Это не SLO и не включённые алерты.

## Пробы

| Путь | Смысл |
| --- | --- |
| `GET /live` | Процесс жив, база не проверяется. Этим путём смотрит Docker healthcheck. |
| `GET /health` | Старый ответ `{status, db}`. Всегда HTTP 200, чтобы не сломать уже настроенные пробы. |
| `GET /ready` | HTTP 200 только если база отвечает, ревизия Alembic совпадает с кодом, и (когда включено) свежие heartbeat worker и scheduler. Иначе HTTP 503. |
| `GET /health/details` | Те же данные плюс мёртвая очередь и последние cron. Заголовок `X-Internal-Token` равен `HEALTH_DETAILS_TOKEN` или `WEBHOOK_SECRET`. |
| `GET /metrics` | Текст Prometheus. Заголовок `X-Metrics-Token` = `METRICS_TOKEN`. Без токена HTTP 401. |

Счётчики и гистограммы webhook/HTTP/LLM/cron — **process-local** (не суммируются между `backend`, worker и scheduler). Очереди, heartbeat age и morning-price last success берутся из Postgres при каждом scrape.

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

- `backend` — HTTP (`uvicorn`). В `docker-compose.yml` по умолчанию `SCHEDULER_ENABLED=false`, `SCHEDULER_EXPECTED=true`: cron на web не запускается, `/ready` ждёт heartbeat отдельного scheduler.
- `backend-worker` — `python -m app.workers.main`, `SCHEDULER_ENABLED=false`.
- `backend-scheduler` — `python -m app.scheduler`; пишет `process_heartbeats` с `process_type=scheduler` каждые ~15 с. Health orchestrator — через `/ready` на web (свежесть heartbeat в Postgres), не через HTTP у scheduler.

Локальная разработка в одном процессе: `SCHEDULER_ENABLED=true`, `SCHEDULER_EXPECTED=false` — тогда web сам пишет scheduler heartbeat и поднимает APScheduler в lifespan.

Split deploy: никогда не включайте cron и на web, и в `backend-scheduler` одновременно. Advisory lock не заменяет правильную топологию.
