#!/usr/bin/env bash
# Manual morning-price verification after channel binding (requires backend WEBHOOK_SECRET).
set -euo pipefail

: "${PUBLIC_BACKEND_URL:?set PUBLIC_BACKEND_URL}"
: "${WEBHOOK_SECRET:?set WEBHOOK_SECRET}"

curl -sf -X POST \
  -H "X-Webhook-Secret: ${WEBHOOK_SECRET}" \
  "${PUBLIC_BACKEND_URL}/jobs/morning-price"

echo "morning-price job accepted"
