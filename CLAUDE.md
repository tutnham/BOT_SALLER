# Project Overview & Guidelines: Zakupki-Bot Ecosystem

This repository houses a dual-service architecture designed to automate hardware purchasing, supplier price tracking, and analytics in Moscow.

## Architecture Architecture Overview

1. **Zakupki-Bot (Main Application)**
   - **Role:** Webhook-driven bot core, purchase workflow management, negotiation state machine, supplier messaging templates, analytics generation.
   - **Stack:** Python 3.11+, FastAPI, PostgreSQL, SQLAlchemy 2.x, Alembic, APScheduler (in-process cron).
   - **Primary Spec:** `Техническая документация  бот для закупок.md` (Source [6])

2. **tg-channel-parser (Standalone Userbot Microservice)**
   - **Role:** MTProto userbot parsing Telegram channels (for daily supplier prices or monitoring). Serves internal FastAPI to Zakupki-Bot; MVP scope is `supplier_price_source` + media download.
   - **Stack:** Python 3.11+, Pyrogram (MTProto), PostgreSQL (`tg_parser` database), separate Coolify Compose stack.
   - **Primary Spec:** `TG_CHANNEL_PARSER_DOCUMENTATION.md` (Source [4])
   - **Ops:** `СЕРВИСЫ.md`, `PHASE4_SETUP.md`

---

## Critical Rules & Non-Negotiables

1. **Strict Service Isolation:**
   - **Zakupki-Bot** and **tg-channel-parser** MUST NOT share PostgreSQL databases or ORM models.
   - **Zakupki-Bot** communicates with the Parser exclusively over HTTP API via `PARSER_API_URL` and `PARSER_API_TOKEN` (Source [4], Source [6]).
   - **No Userbot Code in Main Bot:** The main bot operates solely on Telegram Bot API (direct webhook) (Source [6]). MTProto/Pyrogram code lives strictly inside `tg-channel-parser` (Source [4]).

2. **Strict LLM Constraints:**
   - **No Financial Calculations or Decision Making:** LLMs are restricted strictly to structured text extraction (JSON mode with Pydantic validation) and HTML formatting (Source [6]).
   - **No Competitor Price Leakage:** Supplier bargaining templates must NEVER include or reveal competitor prices (Source [6]).
   - **Confidence Threshold:** Any LLM response with `confidence < CONFIDENCE_THRESHOLD` (default 0.75) must fall back to raw human text without automated quote creation (Source [6]).

3. **Database & Idempotency:**
   - All DB Schema changes MUST be driven by Alembic migrations (separate migration chains for `zakupki` and `tg_parser`) (Source [4], Source [6]).
   - All incoming webhooks must verify `update_log.tg_update_id` for idempotency (Source [6]).
   - All database tables must strictly maintain unique indexes on Telegram IDs and post message IDs (Source [4], Source [6]).

4. **Security & Secrets:**
   - `TELEGRAM_SESSION_STRING`, `api_id`, `api_hash`, DB passwords, and API tokens must exist ONLY in `.env` files (Source [4], Source [6]).
   - Ensure `.env` and `*.session` remain strictly ignored in `.gitignore` (Source [4], Source [13]).

---

## Reference Map

- **System System Architecture & Bot Logic:** Refer to `Техническая документация  бот для закупок.md` (Source [6])
- **Channel Parser & MTProto Spec:** Refer to `TG_CHANNEL_PARSER_DOCUMENTATION.md` (Source [4])
- **Archived workflow JSON:** `n8n_workflow_zakupki_bot.json` is kept only as rollback/archive reference (Source [5])
- **Agent Guidelines & Phased Roadmap:** Refer to `AGENTS.md`

---

## Current Repository Scope

- This repository contains **`backend/`** (Zakupki-Bot) and **`tg-channel-parser/`** (MTProto microservice).
- Production: backend on Coolify + Supabase; parser on Coolify as private Compose stack (see `СЕРВИСЫ.md`).