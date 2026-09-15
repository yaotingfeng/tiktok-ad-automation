# Pagination Total Counts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display an exact filtered data total in every TK-ADA list that has pagination.

**Architecture:** Extend the shared cursor page contract with an exact total computed before the cursor predicate, then pass that value through the generated client to the shared pager. Adapt the few local or specialized pagers to the same copy using their existing exact counts.

**Tech Stack:** FastAPI, Pydantic, SQLModel/SQLAlchemy, React 19, TanStack Query, Playwright, Bun/Vite.

**Spec:** `docs/superpowers/specs/2026-09-15-pagination-total-counts-design.md`

## Global Constraints

- `total` is the exact count after tenant, BC, permission, search, and filter scoping, before cursor and limit.
- Preserve seek-pagination ordering and cursor behavior.
- Use the shared `Pager` for common UI and the approved neutral shadcn visual system.
- Do not make live TikTok, provider, authorization, or advertising writes during tests.
- Deployment requires the target environment's complete backup and restore-verification gate.

---

### Task 1: Shared page response contract

**Files:**
- Modify: `backend/app/core/pagination.py`
- Test: `backend/tests/core/test_pagination.py`

**Interfaces:**
- Produces: `Page[T](items: list[T], next_cursor: str | None, total: int)`.

- [ ] Add a schema test that constructs a page with `total=3`, serializes it, and rejects a negative or missing total.
- [ ] Run `uv run pytest tests/core/test_pagination.py -q` and confirm the new assertions fail for the missing field/validation.
- [ ] Add the required non-negative integer field to `Page[T]`.
- [ ] Re-run the focused test and confirm it passes.

### Task 2: Exact totals for backend catalogs

**Files:**
- Modify: `backend/app/modules/tenants/service.py`
- Modify: `backend/app/modules/accounts/router.py`
- Modify: `backend/app/modules/materials/{catalog,repository,router,ingest_service}.py`
- Modify: `backend/app/modules/providers/{catalog,repository,router}.py`
- Modify: `backend/app/modules/strategies/service.py`
- Modify: `backend/app/modules/builds/{catalog,draft_catalog,preview_catalog,previews,submission_catalog,submissions}.py`
- Test: existing module tests under `backend/tests/modules/`

**Interfaces:**
- Consumes: required `Page.total` from Task 1.
- Produces: every system-owned `Page` response with an exact total independent of cursor and limit.

- [ ] Add focused assertions to representative module tests that filtered first and second pages return the same exact total.
- [ ] Run those test nodes and confirm failures are caused by absent totals.
- [ ] Add count statements that reuse the pre-cursor predicates and populate every `Page` constructor.
- [ ] Run all affected backend module suites and fix only contract/count regressions.

### Task 3: Generated client and shared pager

**Files:**
- Modify: `frontend/src/client/types.gen.ts` via the repository client generator
- Modify: `frontend/src/features/tenants/shared.tsx`
- Test: `frontend/tests/tenants-accounts.spec.ts`

**Interfaces:**
- Consumes: `Page_*.total: number` from the OpenAPI schema.
- Produces: `<Pager total={number | undefined}>` rendering `共 N 条 · 第 X 页`.

- [ ] Extend a representative Playwright test to expect `共 55 条 · 第 1 页` and the same total on page two.
- [ ] Run the focused test and confirm it fails because the total is absent.
- [ ] Regenerate the client, add the `total` prop, and render the unified copy without showing a false zero during errors.
- [ ] Pass `data?.total` from every shared-page caller.
- [ ] Re-run the focused test and TypeScript build.

### Task 4: Specialized pagination displays

**Files:**
- Modify: `frontend/src/features/accounts/McpAuthorizationSheet.tsx`
- Modify: `frontend/src/features/builds/MiniTargetPicker.tsx`
- Modify: `frontend/src/features/builds/DramaMaterialSheet.tsx`
- Modify: `frontend/src/features/materials/BatchUploadSheet.tsx`
- Test: relevant Playwright suites in `frontend/tests/`

**Interfaces:**
- Consumes: existing MCP `total`, Minis `total`, drama material collection count, and local `files.size`.
- Produces: the same `共 N 条 · 第 X 页` wording at each specialized pager.

- [ ] Add UI assertions for each specialized paginator's exact total.
- [ ] Run the focused tests and confirm the new assertions fail.
- [ ] Add total text using the existing authoritative count for each component.
- [ ] Re-run the focused suites and confirm they pass.

### Task 5: Full verification, documentation, commit, push, and deploy

**Files:**
- Modify: `docs/implementation-progress.md`
- Create: environment-specific validation record under `docs/validation/`

**Interfaces:**
- Consumes: completed Tasks 1–4 and the selected environment runbook.
- Produces: a focused Git commit, pushed branch, recoverable deployment, and validation evidence.

- [ ] Run affected backend suites, full frontend build, and pagination-focused Playwright suites.
- [ ] Check `git diff --check`, repository root, status, and staged diff; update progress documentation with exact commands/results.
- [ ] Commit only this feature and push the current branch without force.
- [ ] Resolve the requested server from repository environment context; read its dedicated runbook completely.
- [ ] Freeze writes, drain work, pause backup scheduling, create all required backups, and verify restores before changing the running version.
- [ ] Copy/deploy the exact pushed SHA, run migrations if any, restart the prescribed services, restore scheduling, and verify version, health, configuration parity, and pagination totals.
