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

`/ready` не зависит от Jev: при недоступности TypeSafe бот остаётся ready и уходит в детерминированный путь, существующий LLM или разбор сотрудником.

`/health/details` добавляет блок `jev`: enabled, shadow_mode, configured, last success/failure, error code, rolling latency, failure count. Платный запрос в Jev на health не выполняется.

Метрики Jev (bounded labels result, error_code, shadow_mode, model, action): `jev_requests_total`, `jev_errors_total`, `jev_timeouts_total`, `jev_rate_limits_total`, `jev_invalid_responses_total`, `jev_latency_seconds`, `jev_decisions_total`, `jev_shadow_disagreements_total`, `jev_auto_bind_candidates_total`, `jev_review_total`, `jev_cache_hits_total`.

В лейблах нет supplier id, chat id и текста сообщения.

`/ready` требует свежий heartbeat `worker`, когда `WEBHOOK_ASYNC_ENABLED=true` или `AUTO_PUBLISH_FIRST_VALID_PRICE=true`. При `CLIENT_PRICE_INLINE_DELIVERY=true` этот heartbeat пишет сам web. Heartbeat scheduler требуется, когда `SCHEDULER_ENABLED=true` или `SCHEDULER_EXPECTED=true`. Свежесть — `HEARTBEAT_STALE_SECONDS` (по умолчанию 90).

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
