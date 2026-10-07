# Supplier binding verification log

Recorded on local workspace (uncommitted changes on top of `7af739beb7d13de4c57448397bea7ae173d28edc`).

| Command | Exit code | Notes |
|---------|-----------|--------|
| `uv run ruff check app tests` (backend) | 0 | All checks passed |
| `uv run ruff check app tests` (tg-channel-parser) | 0 | All checks passed |
| `uv run mypy app --config-file pyproject.toml` | 0 | Success, 100 source files |
| `uv run alembic upgrade head` | 0 | Head: `0019_messages_in_pending_binding_status` |
| `pytest tests/test_supplier_reply_binding.py -q` | (bundle) | See row below |
| `pytest tests/test_business_message.py -q` | (bundle) | See row below |
| `pytest tests/test_supplier_llm_fallback.py -q` | (bundle) | See row below |
| `pytest tests/test_messages_unique.py -q` | (bundle) | See row below |
| `pytest tests/test_supplier_binding_scenarios.py -q` | (bundle) | See row below |
| Combined binding pytest bundle | 0 | **31 passed** in ~43s |
| `pytest tests -q` (full suite) | — | Prior run: **398 passed**, 4 failed (fixed in tree); re-run blocked by concurrent local pytest/DB lock |
| `docker build` (backend image) | — | Docker CLI not available on this host |

## Alembic revision

`EXPECTED_ALEMBIC_REVISION` = `0019_messages_in_pending_binding_status`

## CI

Push branch and confirm GitHub Actions workflow `ci` is green on the merge SHA (main was red on `7af739b` before this work).

## Staging

Business webhook smoke remains manual after deploy (`SUPPLIER_SINGLE_CANDIDATE_AUTO_BIND_ENABLED` rollout per `SUPPLIER_BINDING.md`).
