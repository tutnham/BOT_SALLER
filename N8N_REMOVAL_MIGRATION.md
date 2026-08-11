# Миграция: удаление n8n из проекта zakupki-bot

Версия: 1.0 · Дата: 10 августа 2026
Назначение: пошаговая инструкция для кодера (Cursor). Выполнять строго по шагам сверху вниз. После каждого шага с пометкой **ПРОВЕРКА** — выполнить проверку и только потом идти дальше.

Изменяемые документы: `TECHNICAL_DOCUMENTATION.md` (бот закупок). НЕ изменяется: `TG_CHANNEL_PARSER_DOCUMENTATION.md` — сервис-парсер к n8n отношения не имеет и остаётся как есть.

---

## 1. Что меняется (суть миграции)

### До

```
Telegram ──webhook──> n8n (Telegram Trigger) ──HTTP──> backend /telegram/webhook
n8n (4 × Schedule Trigger) ──HTTP──> backend /jobs/*
n8n ──алерт при не-200──> Bot API sendMessage (ADMIN_ALERT_CHAT_ID)
```

### После

```
Telegram ──webhook (secret_token)──> backend /telegram/webhook
APScheduler (внутри backend-процесса, 4 джобы) ──прямой вызов──> service-функции
backend ──алерт при исключении──> Bot API sendMessage (ADMIN_ALERT_CHAT_ID)
```

Ключевой факт, упрощающий миграцию: **вся бизнес-логика и все эндпоинты уже существуют в backend** (ТД §7). n8n был пассивным прокси и будильником. Никакая бизнес-логика, DDL, LLM-контракты, шаблоны и алгоритмы 9.1–9.6 **не меняются**.

Экономия ресурсов: минус контейнер n8n (~300–500 МБ RAM), минус один HTTP-хоп на каждый update, минус точка отказа «n8n упал — бот мёртв».

---

## 2. Полный маппинг нод n8n → код

Источник: `n8n_workflow_zakupki_bot.json`. Всего 15 нод.

| # | Нода n8n | Параметры из JSON | Куда переезжает |
|---|---|---|---|
| 1 | `Telegram Trigger` | updates: `message`, `edited_message` | Telegram `setWebhook` напрямую на backend (шаг 7), `allowed_updates=["message","edited_message"]` |
| 2 | `Forward To Backend` | POST `/telegram/webhook`, header `X-Webhook-Secret`, timeout 15s | Удаляется — Telegram шлёт update напрямую в тот же эндпоинт. Секрет переносится в стандартный механизм Telegram `secret_token` (шаг 4.2) |
| 3 | `IF: Backend Error` (statusCode ≠ 200) | — | Обёртка `handle_update_safe()` в роуте webhook: try/except → алерт админу, наружу всегда 200 (шаг 4.2) |
| 4 | `Alert Admin (Telegram API)` | sendMessage в `ADMIN_ALERT_CHAT_ID`, текст со statusCode и update_id | Функция `alert_admin()` в backend (шаг 4.1) |
| 5 | `No Operation` | — | Удаляется (ветка «успех» в n8n ничего не делала) |
| 6 | `Schedule: Morning Price 08:00` | cron `0 8 * * *`, TZ Europe/Moscow | APScheduler джоба `morning_price` (шаг 5) |
| 7 | `POST /jobs/morning-price` | timeout 60s, `X-Webhook-Secret` | Прямой вызов `price_service.build_morning_price()` с `asyncio.wait_for(..., 60)` |
| 8 | `IF: Morning Price Error` + `Alert Admin: Morning Price Failed` | алерт при не-200 | `alert_on_failure=True` у джобы `morning_price` — **единственная джоба с алертом, как и в n8n** |
| 9 | `No Operation (Morning Price OK)` | — | Удаляется |
| 10 | `Schedule: Recheck Poller (15 min)` | cron `*/15 * * * *` | APScheduler джоба `recheck_due` |
| 11 | `POST /jobs/recheck-due` | timeout 30s, без алерта при ошибке (`neverError`, нет IF-ноды) | Прямой вызов `recheck_service.send_due_rechecks()` с `wait_for(..., 30)`, `alert_on_failure=False` (только лог) |
| 12 | `Schedule: Daily Report 21:00` | cron `0 21 * * *` | APScheduler джоба `daily_report_day` |
| 13 | `POST /jobs/daily-report (day)` | body `{"period":"day"}`, timeout 30s, без алерта | `report_service.build_report(period="day")`, `wait_for(..., 30)`, без алерта |
| 14 | `Schedule: Weekly Report Mon 09:00` | cron `0 9 * * 1` | APScheduler джоба `weekly_report` |
| 15 | `POST /jobs/daily-report (week)` | body `{"period":"week"}`, timeout 30s, без алерта | `report_service.build_report(period="week")`, `wait_for(..., 30)`, без алерта |

Важно для точности поведения: в n8n алерт админу был **только** у webhook-цепочки и у morning-price. У recheck-due и обоих отчётов ошибок-алертов не было. Сохраняем это поведение 1:1 (ошибки пишутся в лог).

---

## 3. Что удаляется из проекта

| Артефакт | Действие |
|---|---|
| Сервис `n8n` в `docker-compose.yml` | Удалить целиком (шаг 6.1) |
| Каталог `n8n/` в репозитории | НЕ удалять — оставить `n8n/zakupki_bot_workflow.json` как архив для отката (шаг 10) |
| `N8N_WEBHOOK_SECRET` | Переименовать в `WEBHOOK_SECRET` — продолжает защищать ручные вызовы `POST /jobs/*` |
| `N8N_BASIC_AUTH_USER`, `N8N_BASIC_AUTH_PASSWORD` | Удалить из `.env` и `.env.example` |
| ТД §2: блок «n8n (оркестрация)» в диаграмме и таблице ролей | Обновить (шаг 8) |
| ТД §3: строка «Оркестрация — n8n» | Заменить на APScheduler (шаг 8) |
| ТД §13 п.3–4 | Обновить формулировки про webhook и секрет (шаг 8) |

Что НЕ удаляется: HTTP-эндпоинты `POST /jobs/morning-price`, `/jobs/recheck-due`, `/jobs/daily-report` остаются в backend под защитой `X-Webhook-Secret` — для ручного запуска и отладки. Cron их больше не дёргает по HTTP, джобы вызывают service-функции напрямую.

---

## 4. Шаг 1–4: изменения в коде backend

### Шаг 1. Зависимости

В `backend/requirements.txt` добавить:

```
apscheduler>=3.10,<4.0
```

(Ставим v3, не v4 — v4 на момент документа в pre-release с другим API.)

### Шаг 2. Конфиг (`backend/app/config.py`)

Добавить в `Settings`:

```python
# Раньше: N8N_WEBHOOK_SECRET — переименован
WEBHOOK_SECRET: str                      # защита ручных POST /jobs/*

# Новое: секрет, который Telegram присылает в заголовке X-Telegram-Bot-Api-Secret-Token
TELEGRAM_WEBHOOK_SECRET_TOKEN: str

# Новое: публичный HTTPS-URL backend, нужен для setWebhook
PUBLIC_BACKEND_URL: str                  # напр. https://api.zakupki.example.com

TZ: str = "Europe/Moscow"
```

Удалить из `Settings`: `N8N_BASIC_AUTH_USER`, `N8N_BASIC_AUTH_PASSWORD` (если были), `N8N_WEBHOOK_SECRET` (заменён на `WEBHOOK_SECRET`).

**ПРОВЕРКА:** `python -c "from app.config import settings"` проходит без ошибок при заполненном `.env`.

### Шаг 3. Генерация новых секретов (выполняет человек, кодер фиксирует в `.env`)

```bash
# TELEGRAM_WEBHOOK_SECRET_TOKEN — требования Telegram: 1-256 символов, A-Z a-z 0-9 _ -
openssl rand -hex 32

# WEBHOOK_SECRET — можно оставить старое значение N8N_WEBHOOK_SECRET, просто переименовать
```

### Шаг 4.1. Функция алерта админу (`backend/app/telegram/alerts.py`, новый файл)

Переносит ноду «Alert Admin (Telegram API)». Прямой вызов Bot API через тот же httpx-клиент, что и `telegram/client.py`:

```python
import logging
from app.config import settings
from app.telegram.client import send_message_safe

logger = logging.getLogger(__name__)


async def alert_admin(text: str) -> None:
    """Алерт админу при сбоях. Никогда не пробрасывает исключение наружу —
    алерт не должен ронять вызывающий код."""
    try:
        await send_message_safe(settings.ADMIN_ALERT_CHAT_ID, text)
    except Exception:
        logger.exception("Не удалось отправить алерт админу: %s", text)
```

### Шаг 4.2. Роут webhook (`backend/app/telegram/webhook_router.py`)

Сейчас эндпоинт проверяет `X-Webhook-Secret` (его ставил n8n) и обрабатывает update синхронно. Меняем на схему: Telegram ставит `X-Telegram-Bot-Api-Secret-Token`, обработка уходит в фон, наружу всегда 200, исключения → алерт админу (перенос нод «IF: Backend Error» + «Alert Admin»):

```python
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import settings
from app.telegram.alerts import alert_admin

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    # 1. Верификация: заголовок выставляет Telegram, значение мы задали в setWebhook (шаг 7)
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if token != settings.TELEGRAM_WEBHOOK_SECRET_TOKEN:
        raise HTTPException(status_code=401)

    update = await request.json()

    # 2. Обработка в фоне — отвечаем Telegram 200 сразу (замена neverError-цепочки n8n).
    #    Идемпотентность по update_log внутри dispatch_update защищает от ретраев Telegram.
    background_tasks.add_task(handle_update_safe, update)
    return {"ok": True}


async def handle_update_safe(update: dict) -> None:
    """Перенос нод 'IF: Backend Error' + 'Alert Admin (Telegram API)' из n8n."""
    try:
        await dispatch_update(update)  # существующая логика из ТД §7.1, без изменений
    except Exception as exc:
        logger.exception("Ошибка обработки update %s", update.get("update_id"))
        await alert_admin(
            "⚠️ Backend webhook error\n"
            f"Error: {type(exc).__name__}\n"
            f"Update ID: {update.get('update_id', 'n/a')}"
        )
```

`dispatch_update()` — существующая функция диспетчеризации из ТД §7.1 (идемпотентность, whitelist, ветки сотрудник/поставщик/владелец). Её код не меняется.

**ПРОВЕКА:** ручной запрос с неверным токеном → 401; с верным → 200 и обработка:

```bash
curl -i -X POST "$PUBLIC_BACKEND_URL/telegram/webhook" \
  -H "X-Telegram-Bot-Api-Secret-Token: wrong" -d '{}'                       # ожидаем 401

curl -i -X POST "$PUBLIC_BACKEND_URL/telegram/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: $TELEGRAM_WEBHOOK_SECRET_TOKEN" \
  -d '{"update_id": 999999999, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "test"}}'
# ожидаем 200 {"ok": true}; в логах — ветка "ignored" (id не в whitelist)
```

---

## 5. Шаг 5: планировщик (`backend/app/scheduler.py`, новый файл)

Полная замена 4 Schedule Trigger + 4 HTTP Request + IF/Alert нод. Cron-выражения и таймауты взяты 1:1 из JSON workflow (маппинг — таблица §2):

```python
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.services import price_service, recheck_service, report_service
from app.telegram.alerts import alert_admin

logger = logging.getLogger(__name__)


async def _run_guarded(job_name: str, fn, timeout: float, alert_on_failure: bool) -> None:
    """Общий прогон джобы: таймаут = бывший timeout HTTP-ноды n8n,
    алерт = только там, где в n8n стояла цепочка IF→Alert Admin."""
    logger.info("Джоб %s запущен", job_name)
    try:
        await asyncio.wait_for(fn(), timeout=timeout)
        logger.info("Джоб %s завершён успешно", job_name)
    except Exception as exc:
        logger.exception("Джоб %s упал: %s", job_name, exc)
        if alert_on_failure:
            await alert_admin(
                f"⚠️ Джоб {job_name} завершился ошибкой\n"
                f"Error: {type(exc).__name__}: {exc}\n"
                "Проверить логи backend и доступность сервиса-парсера каналов (PARSER_API_URL)."
            )


async def job_morning_price() -> None:
    # БЫЛО: Schedule "0 8 * * *" → POST /jobs/morning-price (timeout 60s) → IF≠200 → Alert Admin
    await _run_guarded("morning-price", price_service.build_morning_price,
                       timeout=60, alert_on_failure=True)


async def job_recheck_due() -> None:
    # БЫЛО: Schedule "*/15 * * * *" → POST /jobs/recheck-due (timeout 30s), без алерта
    await _run_guarded("recheck-due", recheck_service.send_due_rechecks,
                       timeout=30, alert_on_failure=False)


async def job_daily_report_day() -> None:
    # БЫЛО: Schedule "0 21 * * *" → POST /jobs/daily-report {"period":"day"} (timeout 30s), без алерта
    await _run_guarded("daily-report-day",
                       lambda: report_service.build_report(period="day"),
                       timeout=30, alert_on_failure=False)


async def job_weekly_report() -> None:
    # БЫЛО: Schedule "0 9 * * 1" → POST /jobs/daily-report {"period":"week"} (timeout 30s), без алерта
    await _run_guarded("weekly-report",
                       lambda: report_service.build_report(period="week"),
                       timeout=30, alert_on_failure=False)


def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.TZ)
    defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}

    scheduler.add_job(job_morning_price,
                      CronTrigger.from_crontab("0 8 * * *", timezone=settings.TZ),
                      id="morning_price", **defaults)
    scheduler.add_job(job_recheck_due,
                      CronTrigger.from_crontab("*/15 * * * *", timezone=settings.TZ),
                      id="recheck_due", **defaults)
    scheduler.add_job(job_daily_report_day,
                      CronTrigger.from_crontab("0 21 * * *", timezone=settings.TZ),
                      id="daily_report_day", **defaults)
    scheduler.add_job(job_weekly_report,
                      CronTrigger.from_crontab("0 9 * * 1", timezone=settings.TZ),
                      id="weekly_report", **defaults)
    return scheduler
```

Примечания для кодера (не пропускать):

- `max_instances=1` — следующий запуск не стартует, пока идёт предыдущий (защита от наложения recheck-поллера, если один прогон завис).
- `coalesce=True` + `misfire_grace_time=300` — если контейнер был выключен в момент срабатывания, после старта джоба выполнится один раз (а не серией пропущенных запусков), и только если опоздание ≤ 5 минут. Утренний прайс 08:00 при даунтайме НЕ догоняется автоматически — запускается вручную через `POST /jobs/morning-price` (поведение, эквивалентное n8n).
- Jobstore — дефолтный in-memory, НЕ подключать SQLAlchemyJobStore: расписания статичны и пересоздаются при каждом старте, персистентность не нужна и потребовала бы лишней миграции Alembic.
- Имена вызываемых функций (`build_morning_price`, `send_due_rechecks`, `build_report`) — те, что уже вызывают HTTP-эндпоинты `/jobs/*` по ТД §7.2–7.4. Если в реализации они названы иначе — поправить импорты, логику функций не трогать.

### Подключение в `backend/app/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.scheduler import setup_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = setup_scheduler()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
# ... регистрация роутов — без изменений
```

**ПРОВЕРКА (без ожидания реального cron):** временно добавить в `setup_scheduler()` тестовую джобу `CronTrigger(second=...)`/`interval` на 1 минуту, вызывающую `job_recheck_due`, поднять backend локально и увидеть в логах «Джоб recheck-due запущен/завершён». Затем тестовую джобу удалить. Либо дёрнуть функции напрямую: `python -c "import asyncio; from app.scheduler import job_recheck_due; asyncio.run(job_recheck_due())"`.

---

## 6. Шаг 6: инфраструктура

### 6.1. `docker-compose.yml` — удалить сервис n8n

Удалить целиком блок:

```yaml
  n8n:
    image: ...        # удалить весь сервис, включая volumes, env, ports
```

В сервисе `backend`:

- удалить `n8n` из `depends_on` (если был);
- из `environment` удалить `N8N_WEBHOOK_SECRET`, `N8N_BASIC_AUTH_USER`, `N8N_BASIC_AUTH_PASSWORD`;
- добавить в `environment`: `WEBHOOK_SECRET`, `TELEGRAM_WEBHOOK_SECRET_TOKEN`, `PUBLIC_BACKEND_URL`, `TZ=Europe/Moscow`;
- убедиться, что `BACKEND_URL` больше нигде не нужен внутри backend (он был адресом самого backend для n8n) — удалить, если не используется кодом.

Также удалить named volume n8n (`n8n_data` или аналог) из секции `volumes:` — предварительно убедиться, что workflow уже сохранён в `n8n/zakupki_bot_workflow.json` в репозитории (он там и есть).

### 6.2. `.env.example` — привести к виду:

```env
# Telegram
TELEGRAM_BOT_TOKEN=
ADMIN_ALERT_CHAT_ID=
ANALYTICS_CHAT_ID=
TELEGRAM_WEBHOOK_SECRET_TOKEN=   # openssl rand -hex 32; те же символы в setWebhook secret_token

# Backend
PUBLIC_BACKEND_URL=              # https://<домен backend из Coolify>, без слэша в конце
WEBHOOK_SECRET=                  # защита ручных POST /jobs/*
CONFIDENCE_THRESHOLD=0.75
TZ=Europe/Moscow

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/zakupki

# LLM
LLM_API_KEY=
LLM_MODEL=
LLM_PROVIDER=

# Сервис парсинга каналов поставщиков (без изменений)
PARSER_API_URL=http://tg-parser-api:8100
PARSER_API_TOKEN=
PARSER_API_TIMEOUT_SECONDS=10
```

Блок «# n8n» с `N8N_BASIC_AUTH_*` — удалён. Блок парсера — без изменений.

### 6.3. Coolify

1. Открыть проект → удалить ресурс/сервис `n8n` (Stop → Delete). Перед удалением: убедиться, что деплой нового backend (шаги 1–5) уже собран и прошёл health-check — **порядок критичен, сначала новый backend жив, потом удаляем n8n, потом переключаем webhook (шаг 7)**.
2. В сервисе backend прописать новые env-переменные из 6.2.
3. Убедиться, что у backend есть публичный HTTPS-домен (Telegram требует HTTPS для webhook; HTTP и self-signed не принимает). Этот URL = `PUBLIC_BACKEND_URL`.

---

## 7. Шаг 7: переключение webhook Telegram

Выполняется ПОСЛЕ деплоя нового backend и удаления n8n. Команды выполняются с любой машины с доступом в интернет:

```bash
# 7.1. Снять старый webhook (ставил n8n Telegram Trigger), НЕ сбрасывая накопленные updates
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteWebhook"

# 7.2. Поставить новый webhook напрямую на backend
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  --data-urlencode "url=$PUBLIC_BACKEND_URL/telegram/webhook" \
  --data-urlencode "secret_token=$TELEGRAM_WEBHOOK_SECRET_TOKEN" \
  --data-urlencode 'allowed_updates=["message","edited_message"]'
# ожидаем: {"ok":true,"result":true,"description":"Webhook was set"}

# 7.3. Верификация
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

**ПРОВЕРКА:** в ответе `getWebhookInfo` поле `url` = `$PUBLIC_BACKEND_URL/telegram/webhook`, `allowed_updates` = `["message","edited_message"]`, `pending_update_count` не растёт без обработки, `last_error_date` отсутствует или старый. Параметр `allowed_updates` обязателен — он переносит фильтр ноды Telegram Trigger (`message`, `edited_message`); без него Telegram начнёт слать все типы updates.

**Сквозная ПРОВЕРКА:** написать боту в ЛС `/help` с аккаунта из whitelist → ответ приходит; в логах backend виден проход через `handle_update_safe`. Повторная доставка того же update (ретрай) не даёт дублей — `update_log` (ТД §15, тест 5) работает как раньше.

---

## 8. Шаг 8: правки в `TECHNICAL_DOCUMENTATION.md`

Кодер вносит в документ правки, чтобы спецификация не расходилась с кодом:

1. **§2 диаграмма**: удалить блок «n8n (оркестрация)»; стрелка от Telegram идёт напрямую в Backend с подписью «webhook + secret_token». В блок Backend добавить строку «APScheduler: 4 cron-джобы (бывшие n8n Schedule Trigger)».
2. **§2 таблица ролей**: строку «n8n» заменить на «APScheduler (внутри backend) — cron-расписания, алерты об ошибках джоб».
3. **§3 стек**: строку «Оркестрация — n8n (self-hosted…)» заменить на «Планировщик — APScheduler 3.x (AsyncIOScheduler, внутри процесса backend)». В строке «Контейнеризация» список сервисов: `backend, postgres` (n8n убрать).
4. **§4 структура репозитория**: удалить каталог `n8n/` из дерева (файл физически остаётся как архив, но не является частью системы); добавить `app/scheduler.py` и `app/telegram/alerts.py`.
5. **§7 вводный абзац**: «Все эндпоинты защищены заголовком `X-Webhook-Secret`» → заменить на: «`/telegram/webhook` вызывается Telegram и защищён `X-Telegram-Bot-Api-Secret-Token` (значение задаётся в setWebhook). Эндпоинты `/jobs/*` для ручного запуска защищены `X-Webhook-Secret` (`WEBHOOK_SECRET`); cron-вызовы выполняются внутрипроцессно через APScheduler и по HTTP не идут».
6. **§9.4**: фразу «(n8n cron каждые 15 минут → POST /jobs/recheck-due)» → «(APScheduler, джоба recheck_due, cron */15 * * * *)».
7. **§9.5**: «ВХОД: cron 08:00 МСК (n8n) → POST /jobs/morning-price» → «ВХОД: APScheduler, джоба morning_price, cron 0 8 * * *, Europe/Moscow».
8. **§12 `.env.example`**: заменить блоком из §6.2 настоящего документа.
9. **§13 п.3–4**: п.3 — «Единый webhook на токен бота — backend, `setWebhook` с `secret_token` и `allowed_updates=["message","edited_message"]`»; п.4 — «`X-Telegram-Bot-Api-Secret-Token` для `/telegram/webhook`, `X-Webhook-Secret` для ручных `/jobs/*`».
10. **§14 Фаза 0**: «Docker-compose (backend, postgres, n8n)» → «(backend, postgres)».
11. **§17 п.1**: пометить `n8n_workflow_zakupki_bot.json` как «архив для отката, не используется в рантайме».

`PROJECT_OVERVIEW.md` — обновить §1 (строка про webhook через n8n) и §2 п.3 аналогично.

---

## 9. Чек-лист приёмки миграции

Все пункты обязательны:

1. [ ] `docker-compose up` поднимает backend + postgres без n8n; `GET /health` → ok.
2. [ ] `getWebhookInfo` показывает новый URL, корректные `allowed_updates`, нет свежего `last_error_message`.
3. [ ] Webhook с неверным `X-Telegram-Bot-Api-Secret-Token` → 401; с верным → 200.
4. [ ] `/help` от сотрудника в группе и от владельца в ЛС работают (маршрутизация ТД §7.1 не сломана).
5. [ ] Искусственное исключение в хендлере (временно поднять в тесте) → алерт в `ADMIN_ALERT_CHAT_ID` с update_id, HTTP-ответ Telegram остаётся 200.
6. [ ] Ручные вызовы `POST /jobs/morning-price`, `/jobs/recheck-due`, `/jobs/daily-report` с `X-Webhook-Secret` работают; без секрета → 401.
7. [ ] В логах backend видны строки «Джоб recheck-due запущен» каждые 15 минут (подождать или временно ускорить расписание в тесте).
8. [ ] `docker stats`: контейнер n8n отсутствует; RSS backend вырос не более чем на ~20–30 МБ (APScheduler).
9. [ ] Сквозные тесты ТД §15, пункты 1–9, проходят без изменений (бизнес-логика не тронута).

---

## 10. Откат (rollback)

Если после миграции что-то пошло не так:

1. Вернуть сервис `n8n` в `docker-compose.yml` из git-истории, `docker-compose up -d n8n`.
2. Импортировать `n8n/zakupki_bot_workflow.json` в n8n, активировать workflow — Telegram Trigger сам перерегистрирует webhook на n8n.
3. Проверить `getWebhookInfo` — url должен смениться на n8n.
4. Код backend обратно совместим: эндпоинт `/telegram/webhook` после миграции принимает только `X-Telegram-Bot-Api-Secret-Token`; для периода отката временно вернуть приём `X-Webhook-Secret` (одна строка в проверке заголовка) или не переключать backend на новую версию до решения.

---

## 11. Явно вне скоупа этой миграции

- **LangGraph не вводится.** Джобы и webhook — линейные вызовы существующих service-функций; графовая оркестрация здесь ничего не даёт. LangGraph рассматривается отдельным решением, только если LLM-конвейер разбора прайсов (ТД §9.5, шаги 2–5) усложнится до мультистеп-логики с ветвлениями (несколько валидаций, tool-calling, ретраи по условию модели).
- Сервис-парсер каналов (`TG_CHANNEL_PARSER_DOCUMENTATION.md`) не меняется.
- Бизнес-алгоритмы 9.1–9.6, DDL, LLM-контракты, шаблоны, команды — не меняются.
