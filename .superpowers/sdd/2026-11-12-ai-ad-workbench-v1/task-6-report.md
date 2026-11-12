# Task 6 report

Implemented the TK-ADA AI optimizer workbench shell and reused configuration entry points.

## Delivered

- Moved “AI 优化师” out of the 广告投放 group into its own sidebar group after the normal tenant groups and immediately before “打开体验版”.
- Added a tenant/optimizer scoped two-column `AIWorkbenchShell` with overview, schedule, execution, review and pending views.
- Added optimizer selection with URL scope (`tenantId`, `optimizer_id`, view, record/version IDs and return context); switching optimizer clears record/version context and remounts schedule state.
- Added overview/config panels showing inherited BC, advertiser accounts, one strategy binding, goals, AUTO/MANUAL mode, and schedule controls.
- Reused existing strategy and automation-rule entry points with optimizer, version, bound-scope and return-context query parameters; no duplicate strategy/rule editor was introduced.
- Added review/pending proposal actions using the Task 5 accept/reject API and execution evidence copy that keeps material matching out of the schedule grid.
- Added focused frontend route/menu/link tests and sidebar experience-entry test IDs.

## Verification

- `git diff --check` — passed.
- Frontend build, Biome and Playwright commands were attempted but could not run in this environment because Bun/dependencies are unavailable; npm/npx dependency bootstrap did not complete. Existing deterministic worksheet tests remain unchanged and the new pure route/menu assertions are included in `frontend/tests/ai-workbench.spec.ts`.

## Commit

`feat: add AI optimizer workbench UI`

## Fix round 1

- Added tenant/optimizer scoped occurrence read API and connected execution view to display status, submission, preview and readback evidence.
- Optimizer switch now clears `return_to` in addition to record/version IDs; helper and endpoint tests cover this isolation.
- Existing strategy and rule routes now consume optimizer/record/version/return context, enforce read-only bound context, filter bound rules, and preserve return context through strategy save navigation.
- Schedule frequency/time controls are explicitly disabled because the current daily-plan contract does not persist those fields; the UI directs users to the persisted daily-plan schedule instead of implying a local save.
- Added static route/menu/scope regression assertions.

Verification: `python3 -m compileall -q backend/app/modules/ai_workbench/api.py` and `git diff --check` passed. Frontend Bun/Playwright build remains unavailable because this environment has no Bun or installed frontend dependencies.
