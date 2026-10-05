#!/usr/bin/env bash
# Smoke parser discovery from a container on zakupki-internal.
set -euo pipefail

: "${PARSER_API_URL:?set PARSER_API_URL=http://tg-parser-api:8000}"
: "${PARSER_API_TOKEN:?set PARSER_API_TOKEN}"

case "${PARSER_API_URL}" in
  http://tg-parser-api:8000|http://tg-parser-api:8000/) ;;
  *)
    echo "PARSER_API_URL must stay http://tg-parser-api:8000" >&2
    exit 1
    ;;
esac

python - <<'PY'
import socket
print(socket.getaddrinfo("tg-parser-api", 8000)[0][4])
PY

echo "health"
curl -sf "${PARSER_API_URL}/health" | tee /tmp/parser-health.json
python - <<'PY'
import json
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

echo "smoke ok"
