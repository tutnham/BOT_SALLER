#!/usr/bin/env bash
# End-to-end smoke from backend container after predefined-network wiring.
set -euo pipefail

: "${PARSER_API_URL:?set PARSER_API_URL like http://<parser-api-uuid>:8000}"
: "${PARSER_API_TOKEN:?set PARSER_API_TOKEN}"

echo "health"
curl -sf "${PARSER_API_URL}/health" | tee /tmp/parser-health.json
python - <<'PY'
import json, sys
data = json.load(open("/tmp/parser-health.json"))
assert data.get("status") == "ok", data
assert data.get("db") == "ok", data
print("health ok")
PY

echo "auth negative"
code=$(curl -s -o /dev/null -w "%{http_code}" "${PARSER_API_URL}/channels")
test "$code" = "401" -o "$code" = "403"

echo "channels"
curl -sf -H "Authorization: Bearer ${PARSER_API_TOKEN}" "${PARSER_API_URL}/channels"

if [[ -n "${SMOKE_CHANNEL_ID:-}" ]]; then
  echo "posts"
  curl -sf -H "Authorization: Bearer ${PARSER_API_TOKEN}" \
    "${PARSER_API_URL}/posts?channel_id=${SMOKE_CHANNEL_ID}&content_type=text&limit=5"
fi

echo "smoke ok"
