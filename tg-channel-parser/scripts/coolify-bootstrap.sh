#!/usr/bin/env bash
# Coolify bootstrap for tg-channel-parser (run on VPS via SSH; secrets only in Coolify env).
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.tg-parser.yml}"
STACK_DIR="${STACK_DIR:-/data/coolify/services/<parser-service-id>}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-bot-saller-<uuid>}"

echo "1) Create Coolify Service Stack from tg-channel-parser/docker-compose.tg-parser.yml"
echo "2) Set env in Coolify UI: POSTGRES_*, DATABASE_URL, API_AUTH_TOKEN, TELEGRAM_*"
echo "3) Run interactive auth once (local or VPS):"
echo "   docker compose -f ${COMPOSE_FILE} run --rm -it tg-listener python -m app.mtproto.auth_cli"
echo "4) Put TELEGRAM_SESSION_STRING into Coolify env and redeploy"
echo "5) Enable Connect to Predefined Network on parser stack and backend application"
echo "6) Resolve parser API UUID hostname from Coolify runtime (do not guess tg-parser-api)"
echo "7) From backend container smoke:"
echo "   curl -sf http://<parser-api-uuid>:8000/health"
echo "   curl -sf -H \"Authorization: Bearer \$PARSER_API_TOKEN\" http://<parser-api-uuid>:8000/channels"
echo "8) Set backend PARSER_API_URL/PARSER_API_TOKEN and restart backend"

if [[ "${1:-}" == "--smoke-backend" ]]; then
  : "${PARSER_API_URL:?set PARSER_API_URL}"
  : "${PARSER_API_TOKEN:?set PARSER_API_TOKEN}"
  curl -sf "${PARSER_API_URL}/health"
  curl -sf -H "Authorization: Bearer ${PARSER_API_TOKEN}" "${PARSER_API_URL}/channels"
  echo "backend smoke ok"
fi
