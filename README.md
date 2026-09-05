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

## Production (кратко)

- Backend: Coolify app `bot-saller`, Supabase Session pooler IPv4
- Parser: отдельный Coolify Compose stack, **без публичного API**
- Связь: `PARSER_API_URL` + `PARSER_API_TOKEN` (порт **8000**)

## Owner `/menu` (ЛС бота)

Владелец: `/menu` → inline-админка:

- **Поставщики** — список, добавление, RFQ, чаты
- **Сотрудники** — добавить менеджера (имя + Telegram ID из @userinfobot или forward), вкл/выкл
- **Клиентские беседы** — привязанные группы для `/ask`
- **Новые чаты** — классификация после добавления бота в группу
- **Каналы прайсов** — интеграция с tg-channel-parser

Подробнее: `Техническая документация  бот для закупок.md` (§11.1).
