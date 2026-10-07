# Supplier inbound binding (deterministic-first)

## Decision order

1. **Telegram reply** to a sent `messages_out` row on the same route (`chat_id`, `business_connection_id`).
2. **Explicit request id** in text (`#123 117900`, `заявка 123 — 117900`, `на #123 цена 117900`) when `#N` is in the eligible candidate set.
3. **Single eligible candidate** + parsed price → `single_candidate` when `SUPPLIER_SINGLE_CANDIDATE_AUTO_BIND_ENABLED=true` (shadow-log when false).
4. **Deterministic text / attrs** match with score and margin thresholds.
5. **LLM classify** only when product identity is present; uses `SUPPLIER_BIND_LLM_THRESHOLD` (not quote threshold).
6. **Multiple candidates + bare price** → `pending_binding`, `supplier_bind_sessions`, supplier prompt + operator inbox (no client group purchase price).
7. **No candidates** → `unbound`, no quote.

## Route identity

- Bot route: `(chat_id, tg_message_id)` with `business_connection_id IS NULL`.
- Business route: `(business_connection_id, chat_id, tg_message_id)`.

## Operator recovery

- Owner `bind_pick` / manual bind resolves pending session when `request_id` is in `candidate_request_ids`.
- Supplier can answer with `#N`, inline button (`sup:bind_pick`), or yes/no fallback on the active session.

## Rollback boundary

- Disable `SUPPLIER_SINGLE_CANDIDATE_AUTO_BIND_ENABLED` and `SUPPLIER_BIND_BUTTONS_ENABLED` without DB rollback.
- New `bind_method` values: `single_candidate`, `explicit_request_id`, `callback`, `pending_binding` status on `messages_in`.

## Configuration

See `backend/app/config.py` and environment variables:

- `SUPPLIER_REPLY_MAX_AGE_HOURS` (default 48)
- `SUPPLIER_BIND_LLM_THRESHOLD` (default 0.85)
- `SUPPLIER_BIND_SESSION_TTL_HOURS` (default 4)
- `SUPPLIER_CORRECTION_WINDOW_MINUTES` (optional)
- `SUPPLIER_SINGLE_CANDIDATE_AUTO_BIND_ENABLED` (default false)
- `SUPPLIER_BIND_BUTTONS_ENABLED` (default false)
