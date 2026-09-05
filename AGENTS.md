# AGENTS.md — Development Blueprint & Instructions for AI Agents

This document defines execution rules, code style guidelines, task workflows, and implementation phases for AI agents (Cursor, Claude, Codex) working on this codebase.

---

## 1. Primary Directives & Design Philosophy

When writing or modifying code in this codebase, you must follow these principles:

1. **Deterministic Business Logic:** Never allow LLM outputs to directly execute actions or write unvalidated data into business tables (`quotes`, `deals`, `requests`). LLM output must pass strict Pydantic schema validation first (Source [6]).
2. **Safety First in Supplier Messaging:** Never write arbitrary LLM-generated text directly to suppliers. Always process outgoing supplier requests through pre-defined Jinja/string templates (`TEMPLATES` in `templates/messages_ru.py`) (Source [6]).
3. **Idempotency Mandate:** Every endpoint handling webhooks or cron tasks must check `update_log` or `parser_tasks` status before proceeding (Source [4], Source [6]).
4. **Explicit Fallbacks:** If an LLM provider fails, fails validation, or returns low confidence, gracefully fall back to raw human-readable outputs or regex parsers without crashing the HTTP server (Source [6]).

---



## 2. Directory Structure Conventions

Always place code in its designated location based on its responsibility:

. ├── backend/ # Zakupki-Bot Main Application │ ├── app/ │ │ ├── db/ # SQLAlchemy Models & Alembic Migrations │ │ ├── handlers/ # Employee, Supplier & Owner Request Handlers │ │ ├── services/ # Request, Quote, Bargain, Recheck & Price Services │ │ ├── llm/ # Structured LLM Clients, Prompts & Schemas │ │ ├── parsers/ # Regex & Parsing logic │ │ ├── templates/ # Ru-language message templates │ │ └── telegram/ # Webhook Router & Bot API wrapper ├── tg-channel-parser/ # Microservice: Telegram Channel MTProto Parser │ ├── app/ │ │ ├── mtproto/ # Pyrogram auth, backfill & realtime listener │ │ ├── worker/ # Task processing loop (media download & AI) │ │ ├── ai/ # AI Provider protocols & Ollama implementations │ │ └── api/ # FastAPI routes for post query & reprocess

--- ## 3. Implementation Roadmap (By Phases) Follow this order strictly when building features or expanding the codebase (Source [6]): ### Phase 0: Foundation & Infrastructure - [ ] Set up Docker Compose setup `backend`, `postgres`, `APScheduler`, `tg-parser-postgres`, `tg-listener`, `tg-worker`, `tg-parser-api`, `ollama`) (Source [4], Source [12]). - [ ] Implement initial DB migrations for both services `zakupki` and `tg_parser` databases) (Source [4], Source [6]). - [ ] Create `/health` endpoints and verify `X-Webhook-Secret` validation middleware (Source [6], Source [7]). ### Phase 1: Core Webhook & Basic Communication Pipeline - [ ] Implement `POST /telegram/webhook` dispatcher with whitelist check `owners`, `employees`, `suppliers`) (Source [6], Source [7.1]). - [ ] Implement `/ask` request handler and template renderer to send messages to active suppliers (Source [6], Source [9.1]). - [ ] Implement supplier reply handler (recording `messages_in` and routing raw message back to employee group) (Source [6], Source [9.2]). ### Phase 2: State Machine & Manual Overrides - [ ] Build quote registration `quotes`), manual price setting `/setprice`), agreement `/deal`), status check `/status`), and cancellation `/cancel`) (Source [6], Source [11]). - [ ] Ensure full audit logging across `messages_out`, `messages_in`, and `update_log` (Source [6]). ### Phase 3: LLM Parsing Integration & Analytics - [ ] Build structured LLM extraction for supplier responses with sha256 caching in `parse_cache` (Source [6], Source [8.3, 8.5]). - [ ] Implement low-confidence handling `confidence < 0.75`) (Source [6], Source [8.3]). - [ ] Build `/report` service and owner daily/weekly analytics generation (Source [6], Source [9.6]). ### Phase 4: Morning Price Generation & Parser Microservice Integration - [ ] Implement `tg-channel-parser` MTProto listener `realtime.py`) and task worker `task_worker.py`) (Source [4], Source [3]). - [ ] Implement `GET /posts` API endpoint in parser microservice (Source [4], Source [11]). - [ ] Build `/jobs/morning-price` cron logic in `backend` to fetch posts from `PARSER_API_URL`, process raw text via `llm.parse_price_list`, calculate markups using `markup_rules`, and generate drafts `price_list_drafts`) (Source [6], Source [9.5, 9.5.1]). ### Phase 5: Recheck & Bargaining Automated Workflows - [ ] Build `/bargain` handler sending non-aggressive negotiation requests (Source [6], Source [9.3]). - [ ] Build `/recheck` command handler and setup `POST /jobs/recheck-due` cron runner (Source [6], Source [7.3, 9.4]). --- ## 4. Source Documentation Mapping When referencing technical specs or validating code behavior, refer to the following documents: - **Bot logic, database schemas, LLM rules, and main commands:** See `Техническая документация  бот для закупок.md` (Source [6]). - **Channel parsing, Pyrogram listener, task queue, and MTProto auth:** See `TG_CHANNEL_PARSER_DOCUMENTATION.md` (Source [4]).

## 5. Additional Hard Requirements

1. **Money Type Safety:** Monetary values must use `Decimal` end-to-end. Do not parse or store money via `float`.
2. **Repository Scope:** `backend/` and `tg-channel-parser/` live in this repository. Deploy parser as a separate Coolify Compose resource; never merge databases or MTProto code into backend.