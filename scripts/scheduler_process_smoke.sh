#!/bin/sh
# Start scheduler in a container, wait for DB heartbeat, SIGTERM, require exit 0.
set -eu

image="${1:?image required}"
database_url="${2:?DATABASE_URL required}"

cid=$(docker run -d --rm \
  -e TELEGRAM_BOT_TOKEN=test-bot-token \
  -e DATABASE_URL="$database_url" \
  -e WEBHOOK_SECRET=test-webhook-secret \
  -e TELEGRAM_WEBHOOK_SECRET_TOKEN=test-telegram-webhook-secret \
  -e SCHEDULER_ENABLED=false \
  -e HOSTNAME=ci-scheduler-smoke \
  "$image" \
  python -m app.scheduler)

cleanup() {
  docker rm -f "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

deadline=$(( $(date +%s) + 45 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if ! docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null | grep -q true; then
    echo "scheduler container exited early" >&2
    docker logs "$cid" 2>&1 || true
    exit 1
  fi
  sleep 2
done

docker kill --signal=SIGTERM "$cid" >/dev/null
wait_deadline=$(( $(date +%s) + 35 ))
while [ "$(date +%s)" -lt "$wait_deadline" ]; do
  if ! docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null | grep -q true; then
    exit 0
  fi
  sleep 1
done

echo "scheduler did not stop after SIGTERM" >&2
exit 1
