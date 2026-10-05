#!/usr/bin/env sh
set -eu

: "${TG_PARSER_DATABASE_URL:?source url}"
: "${RESTORE_DATABASE_URL:?temp restore url}"
: "${BACKUP_ARTIFACT:?path to .dump.age}"
: "${AGE_IDENTITY_FILE:?age identity}"

start=$(date -u +%s)
status="${BACKUP_DIR:-/tmp}/last_restore_drill.json"

age -d -i "$AGE_IDENTITY_FILE" -o /tmp/restore.dump "$BACKUP_ARTIFACT"
pg_restore --clean --if-exists --no-owner -d "$RESTORE_DATABASE_URL" /tmp/restore.dump
rm -f /tmp/restore.dump

cd tg-channel-parser
DATABASE_URL="$RESTORE_DATABASE_URL" uv run alembic current

end=$(date -u +%s)
printf '{"ok":true,"duration_seconds":%s}\n' "$((end - start))" > "$status"
