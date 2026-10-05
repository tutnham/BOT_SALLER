# Ретеншн и аудит

## Что удаляется

Задача `retention` в 03:30 по `TZ`. Пока `RETENTION_DRY_RUN=true`, она только считает строки и пишет это в `admin_audit_log`.

Когда `RETENTION_DRY_RUN=false`, один запуск удаляет не больше `RETENTION_BATCH_SIZE` строк каждого вида:

- `webhook_inbox` со статусом `done` и `processed_at` старше `RETENTION_INBOX_DAYS`
- `telegram_outbox` со статусом `sent` и `sent_at` старше `RETENTION_OUTBOX_DAYS`
- `parse_cache` старше `RETENTION_PARSE_CACHE_DAYS`

`pending`, `processing`, `dead`, `uncertain` и заявки, сделки, котировки, сообщения не удаляются. Финансовая история остаётся.

## Аудит

`admin_audit_log` не ссылается на бизнес-таблицы, поэтому удаление заявки его не сотрёт. Туда пишутся одобрение и отклонение прайса, повтор и закрытие ошибки очереди, запуск ретеншна.
