#!/bin/sh
# Import smoke and a check that dev tools are absent from a runtime image.
set -eu

image=$1
shift

if [ "$#" -lt 1 ]; then
  echo "usage: image_smoke.sh IMAGE MODULE [MODULE...]" >&2
  exit 2
fi

for module in "$@"; do
  docker run --rm \
    -e TELEGRAM_BOT_TOKEN=test-bot-token \
    -e DATABASE_URL=postgresql+asyncpg://zakupki:zakupki@localhost:5432/zakupki \
    -e WEBHOOK_SECRET=test-webhook-secret \
    -e TELEGRAM_WEBHOOK_SECRET_TOKEN=test-telegram-webhook-secret \
    -e API_AUTH_TOKEN=test-parser-api-token \
    -e TELEGRAM_API_ID=12345 \
    -e TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef \
    -e TELEGRAM_SESSION_STRING=test-session-string-not-real \
    "$image" python -c "import ${module}"
done

for banned in pytest ruff mypy pre_commit; do
  if docker run --rm "$image" python -c "import ${banned}"; then
    echo "${banned} is installed in ${image}" >&2
    exit 1
  fi
done
