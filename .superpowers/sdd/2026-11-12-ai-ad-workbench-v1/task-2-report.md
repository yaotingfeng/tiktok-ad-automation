# Task 2 report

Implemented daily drama plans for the AI workbench.

## Delivered

- Added tenant/optimizer scoped `ai_daily_plan` and `ai_daily_plan_row` models with a revision counter and composite foreign keys.
- Added `POST/PATCH/GET /tenants/{tenant_id}/ai-optimizers/{optimizer_id}/daily-plans`. PATCH replaces one day only when the submitted revision matches; stale writes return a domain conflict.
- Added Excel-compatible TSV parsing with quoted cells, duplicate-title validation per business day, strict one-optimizer validation, and text-preserving external drama IDs (including leading zeroes).
- Plan rows intentionally contain no material selection fields. `order_materials` is persisted as intent and material matching remains an execution-time concern.
- Added Alembic revision `0120_ai_workbench_daily_plans`.
- Added `DramaPlanGrid` and `AIWorkbenchPage`, ten blank rows, TSV paste, Tab/Enter navigation, undo, date switching, inherited optimizer context, and a visible execution-time material matching note. Added the tenant AI workbench route and side rail entry.

## Verification

- `uv run ruff check ...` (focused backend files): passed.
- `uv run python -m compileall ...`: passed.
- `uv run pytest --confcutdir=backend/tests/modules/ai_workbench backend/tests/modules/ai_workbench/test_daily_plans.py -q`: 4 passed.
- `npm run build` in `frontend/`: passed (including demo build).
- Full backend tests were not run because this worktree has no configured dedicated PostgreSQL test database; the repository test harness refuses non-dedicated or unavailable database settings.

## Commit

Baseline implementation commit: `6aa3f074 feat: add AI optimizer daily drama plans`

## Fix round 1

- Daily plan reads and writes now require an active optimizer with exactly one active strategy binding; inactive or orphaned optimizer contexts are rejected before row writes.
- Provider connection/application pairs are checked against the tenant's provider application registry and are also protected by a composite foreign key.
- The AI workbench is tenant-only, selects an enabled optimizer from the tenant API, loads the selected day's plan, and carries the selected optimizer into save requests. Switching tenant, optimizer, or date resets the rows and revision before loading fresh data.
- Normal cell edits now use the same history path as TSV paste, and focused backend/API plus frontend worksheet checks cover scope, order intent, leading-zero IDs, and row expansion.

Verification for this fix round:

- `uv run ruff check app/modules/ai_workbench/plans.py app/modules/ai_workbench/plans_service.py app/core/errors.py tests/modules/ai_workbench/test_daily_plans.py` (passed).
- `npm run build` in `frontend/` (passed for production and demo bundles).
- `PLAYWRIGHT_BASE_URL=http://127.0.0.1:9 npx playwright test tests/ai-workbench.spec.ts --project=chromium --no-deps` (2 passed).
- Full database-backed API tests remain environment-dependent because this worktree has no configured dedicated PostgreSQL test database; contract tests pass under the isolated test invocation.

