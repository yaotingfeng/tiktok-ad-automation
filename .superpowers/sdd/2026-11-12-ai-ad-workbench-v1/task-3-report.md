# Task 3 report

Implemented scheduled AI optimizer build handoff into TK-ADA's existing local
draft, preparation, preview, submission and reconciliation services.

## Delivered

- Added `BuildOccurrence`, a tenant/optimizer/plan-row scoped immutable handoff
  ledger with frozen BC, advertiser, provider, application and strategy-version
  snapshots and explicit pending/material/dispatch/partial/unknown/complete/
  failed states.
- Added `BuildHandoff.create_occurrence`, which resolves the optimizer's active
  binding at execution time, rejects caller-supplied cross-optimizer or stale
  strategy assertions, preserves provider links and material-order intent, and
  deduplicates by request and scheduled occurrence.
- Added local dispatch through existing `create_draft`, `prepare_draft`,
  `generate_preview` and `submit_preview` services. Plan rows never select a
  material; matching remains in the existing preparation pipeline. No provider
  or TikTok call is made by the handoff service itself.
- Added explicit reconciliation for readback evidence. UNKNOWN is terminal for
  automatic dispatch and cannot be re-submitted by Beat or a duplicate task.
- Added Celery/Beat tasks, outbox registration, and the manual dry-run API at
  `POST /tenants/{tenant_id}/ai-optimizers/{optimizer_id}/plans/{plan_id}/run`.
- Added Alembic migration `0121_ai_build_occurrences`.

## Verification

- `uv run ruff check --fix` on all changed Python files — passed.
- `uv run python -m compileall -q` on changed application, migration and tests — passed.
- `SECRET_KEY=x PROJECT_NAME=x DATABASE_URL=postgresql+psycopg://u:p@localhost/tt_ada_test FIRST_SUPERUSER=admin FIRST_SUPERUSER_PASSWORD=abcdefgh uv run pytest --noconftest backend/tests/modules/ai_workbench/test_build_handoff.py backend/tests/modules/builds/test_ai_workbench_handoff.py -q` — 6 passed.

The repository's full database-backed suite still requires its dedicated
PostgreSQL/Redis test environment; these focused tests do not call external ad
or provider services.
