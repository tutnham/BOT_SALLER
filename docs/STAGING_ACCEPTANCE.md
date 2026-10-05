# Staging acceptance

Живой прогон делает оператор. Не использовать production-чаты, production-поставщиков, production-базы и production-секреты.

## Что уже гоняется в CI

- `backend/tests/acceptance/` — Postgres, фейковые Telegram, LLM и parser.
- `tg-channel-parser/tests/acceptance/` — API parser, когда задан `PARSER_TEST_DATABASE_URL`.

Userbot, реальный Telegram и публикация в чат в CI не ходят.

## Команды

Backend, из `backend/`:

```bash
uv run pytest tests/acceptance -q
```

Parser, из `tg-channel-parser/`, с тестовой БД:

```bash
PARSER_TEST_DATABASE_URL=postgresql+asyncpg://tg_parser:tg_parser@localhost:5432/tg_parser_test \
  uv run pytest tests/acceptance -q
```

## Что обязан сохранить оператор

- SHA коммита.
- `alembic current` для `zakupki` и `tg_parser`.
- Идентификатор прогона.
- `request_id`, `draft_id`, id канала parser.
- Ожидаемый и фактический статус.
- Логи без токенов, session string и текстов сообщений клиентов.
- Точные команды.
- Конфиг окружения без секретов.

## Сценарии, которые CI не закрывает

Нужны тестовый бот, тестовый аккаунт Business и тестовый канал:

1. `/ask` в тестовой группе и ответ поставщика в Telegram.
2. Пост в канале, который читает userbot, затем morning-price с `degraded=0`.
3. Одобрение черновика публикует прайс один раз.
4. Повтор того же job не создаёт второй черновик.
5. Остановка worker через `SIGTERM` и повторный claim после истечения lease.

Ожидание morning-price на здоровом прогоне: `degraded=0`. Если parser или LLM недоступны, HTTP-процесс остаётся живым, а результат помечается degraded.
