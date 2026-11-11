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

## Review fix round 1

- WAITING_MATERIAL occurrences now have a durable retry generation and outbox
  re-arm path. Beat revisits waiting rows and never leaves material waits
  orphaned.
- Existing build result/readback workers call a `record_submission_result`
  hook, mapping reconciliation evidence into COMPLETE/PARTIAL/FAILED/UNKNOWN.
  UNKNOWN cannot transition to DISPATCHED without an explicit retry guard.
- Schedule claims use a stable tenant/optimizer/row/strategy/time advisory lock
  in addition to request idempotency, protecting duplicate requests.
- Beat persists future, inactive-optimizer, and provider-readiness skip reasons
  in occurrence evidence rather than silently dropping rows.
- Added regression tests for retry rearming, UNKNOWN transition protection,
  readback evidence import, and material readiness classification.

Fix round verification:

- `uv run ruff check --fix backend/app/modules/ai_workbench/build_handoff.py backend/app/modules/ai_workbench/build_tasks.py backend/app/modules/builds/tasks.py backend/app/core/errors.py backend/tests/modules/ai_workbench/test_build_handoff.py` — passed.
- Focused handoff tests — `10 passed`.

## Review fix round 2

- Moved the workbench result hook after `finalize_submission` and made it read
  the aggregate `Submission.status`. Intermediate child-step readbacks now
  leave a multi-step occurrence DISPATCHED until the existing finalizer reports
  COMPLETED, PARTIAL, FAILED, or NEEDS_REVIEW.
- Result transitions are monotonic: a completed occurrence cannot regress to a
  partial/unknown child result, while a later aggregate completion may close an
  UNKNOWN occurrence without resubmitting it.
- Added a focused multi-step regression test covering the nonterminal aggregate
  guard.

Round 2 verification:

- `uv run ruff check --fix backend/tests/modules/ai_workbench/test_build_handoff.py` — passed.
- Focused handoff tests — `11 passed`.

## Review fix round 3

- Aggregate `Submission.NEEDS_REVIEW` now maps to occurrence `UNKNOWN`,
  preserving the explicit unresolved-readback state and its evidence instead of
  incorrectly reporting a partial build.
- Added a regression test for the NEEDS_REVIEW-to-UNKNOWN transition.

Round 3 verification:

- Focused handoff tests — `12 passed`.

## Review fix round 4

- `NEEDS_REVIEW` reconciliation now checks the existing submission's active
  units and queued/running execution steps. An attention result with a live
  sibling remains DISPATCHED until the aggregate submission is settled.
- Added a regression covering an UNKNOWN child alongside active sibling work.

Round 4 verification:

- Ruff passed.
- Focused handoff tests — `13 passed`.

## Review fix round 5

- Active execution-step gating now applies the same
  `NOT obsolete_readback_sql(...)` predicate as `finalize_submission`, so an
  obsolete queued/running readback cannot hold an occurrence in DISPATCHED
  after the aggregate submission settles.
- Added a regression that verifies the obsolete-readback SQL predicate and
  allows the occurrence to transition to UNKNOWN.

Round 5 verification:

- Ruff passed.
- Focused handoff tests — `14 passed`.
