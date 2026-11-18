#!/usr/bin/env bash
# Подключить Coolify backend (и при необходимости worker) к сети zakupki-internal.
# Coolify v4 не применяет --network в General → Custom Docker Options (см. СЕРВИСЫ.md).
# Запуск на VPS после Redeploy/Restart bot-saller (и worker, если есть).
set -euo pipefail

NETWORK="${NETWORK:-zakupki-internal}"
BACKEND_APP_UUID="${BACKEND_APP_UUID:-pj7j3j6gr0wgv1qwusce1ozi}"
EXTRA_APP_UUIDS="${EXTRA_APP_UUIDS:-}"

_connect_by_prefix() {
  local prefix="$1"
  local name
  name=$(docker ps --format '{{.Names}}' | grep "^${prefix}-" | head -1 || true)
  if [[ -z "$name" ]]; then
    echo "skip: no running container for prefix ${prefix}"
    return 0
  fi
  if docker network connect "$NETWORK" "$name" 2>/dev/null; then
    echo "connected: $name -> $NETWORK"
  else
    echo "already on network or failed: $name ($NETWORK)"
  fi
}

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "error: network $NETWORK missing; run: docker network create $NETWORK" >&2
  exit 1
fi

_connect_by_prefix "$BACKEND_APP_UUID"
IFS=',' read -ra extras <<< "$EXTRA_APP_UUIDS"
for uuid in "${extras[@]}"; do
  [[ -n "$uuid" ]] || continue
  _connect_by_prefix "$uuid"
done