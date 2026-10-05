#!/usr/bin/env sh
set -eu

: "${TG_PARSER_DATABASE_URL:?TG_PARSER_DATABASE_URL required}"
: "${BACKUP_DIR:?BACKUP_DIR required}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT required}"
: "${S3_BUCKET:?S3_BUCKET required}"

umask 077
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
base="$BACKUP_DIR/tg_parser_${stamp}"
dump="${base}.dump"
enc="${base}.dump.age"
sha="${enc}.sha256"
status="${BACKUP_DIR}/last_backup.json"

pg_dump "$TG_PARSER_DATABASE_URL" -Fc -f "$dump"
age -r "$AGE_RECIPIENT" -o "$enc" "$dump"
rm -f "$dump"
sha256sum "$enc" > "$sha"
aws s3 cp "$enc" "s3://${S3_BUCKET}/tg_parser/$(basename "$enc")"
aws s3 cp "$sha" "s3://${S3_BUCKET}/tg_parser/$(basename "$sha")"

printf '{"ok":true,"completed_at":"%s","artifact":"%s"}\n' "$stamp" "$(basename "$enc")" > "$status"
find "$BACKUP_DIR" -name 'tg_parser_*.dump.age' -mtime +14 -delete
