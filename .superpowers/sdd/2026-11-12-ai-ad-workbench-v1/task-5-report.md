# Task 5 report

Implemented tenant and optimizer scoped DSH review tasks, frozen evidence and versioned knowledge references, short lived worker authorizations, cancellation/restart fences, and structured sanitized task results.

Added strategy and automation rule proposals with immutable before/after snapshots, authorization scope checks, AUTO in-scope publication, MANUAL confirmation, stale-base fencing, shared strategy/rule rejection, and idempotent accept/reject decisions. Publication remains local to the database and uses the existing strategy/rule version services; no provider or advertising API writes are performed.

Added API routes for review creation, proposal listing, acceptance and rejection, the 0123 database migration, and focused regression tests for publication modes, fences, scope checks and evidence sanitization.

Validation:

- `.venv/bin/ruff check ...` — passed.
- `.venv/bin/python -m compileall -q backend/app backend/tests/modules/ai_workbench` — passed.
- Focused pure contract checks for the new tests — passed.
- Full pytest requires the repository's dedicated PostgreSQL test database and Redis service; unavailable in this environment.

## Reviewer fix round 1

- Normalized nested strategy config and rule definition scope before AUTO authorization, including budget/spend deltas and nested account/BC scope.
- Fenced DSH results against authorization expiry at finish time.
- Frozen report, business and operation snapshot IDs, revisions and digests in review evidence.
- Required proposal evidence references to be present in the frozen review task.
- Added review request digests and decision request IDs/digests for conflicting retry detection.
- Broadened secret and reasoning trace sanitization.

## Reviewer fix round 2

- Reporting review inputs now freeze immutable `QuerySnapshot` IDs plus row,
  name-scope and inline `ReportEvidenceVersion` references with digests; no
  mutable report-day rows are read as review evidence.
- Proposal creation rejects a review task whose optimizer version is stale and
  accepts evidence IDs from every frozen category (plans, builds, rules,
  reporting, business and operation logs).
- Added composite tenant/optimizer model foreign keys and migration-matching
  proposal checks/decision reason length, defensive `DomainError` handling for
  malformed nested rule scopes, and JSON-safe secret-sanitized evidence values.

Validation:

- `.venv/bin/ruff check backend/app/modules/ai_workbench backend/tests/modules/ai_workbench` — passed.
- `.venv/bin/python -m compileall -q backend/app/modules/ai_workbench backend/tests/modules/ai_workbench` — passed.
- Pure sanitizer, evidence-ID, malformed-scope and metadata contract checks — passed.
- Full pytest still requires the repository PostgreSQL test database and Redis service.

## Reviewer fix round 3

- Rule AUTO authorization now normalizes only bounded legacy account payloads,
  requires explicit `ALL`/`BC`/`ACCOUNTS` semantics, rejects broad `ALL`,
  requires the corresponding IDs, and limits BC/account scopes to the optimizer
  scope.
- Reporting evidence refs are tenant and advertiser filtered; missing or
  cross-scope `ReportEvidenceVersion` rows fail the review instead of being
  silently omitted. Operation exports and coverage are BC scoped as well.
- Public evidence rejects non-finite floats, and terminal DSH tasks preserve
  COMPLETE/CANCELLED state across cancel/restart calls while stale fences remain
  enforced.

Validation:

- `.venv/bin/ruff check backend/app/modules/ai_workbench backend/tests/modules/ai_workbench` — passed.
- `.venv/bin/python -m compileall -q backend/app/modules/ai_workbench backend/tests/modules/ai_workbench` — passed.
- 11 pure AI workbench regression checks (including terminal-state and scope checks) — passed.
