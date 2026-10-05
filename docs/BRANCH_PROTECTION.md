# Защита ветки main

Команду выполняет владелец репозитория. Агент её не запускает.

Обязательные проверки (имена job в `.github/workflows/ci.yml`):

- `docs`
- `backend`
- `tg-channel-parser`
- `images`
- `compose`
- `secrets`

`PREV_REVISION` в job `backend` и `tg-channel-parser` сейчас равен родителю `head` в git. После `alembic current` на production подставить фактическую ревизию.

```bash
gh api --method PUT repos/tutnham/BOT_SALLER/branches/main/protection \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["docs", "backend", "tg-channel-parser", "images", "compose", "secrets"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

Прямой push в `main` после этого закрыт. Merge при красной проверке невозможен. Ветка должна быть актуальна относительно `main` (`strict: true`).
