# Selector Readiness and Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make identity and Minis catalogs visible as soon as their own cached reads are complete, and add cached directory search to both selectors.

**Architecture:** Replace the shared whole-scene completion gate with resource-specific readiness checks that preserve route, scope and expiry validation. Add optional server-side search over persisted scene facts/pages, then expose it through the existing selector dialog interaction without making TikTok calls.

**Tech Stack:** FastAPI, SQLModel/PostgreSQL JSONB, Pydantic, React, TanStack Query, shadcn/ui, Bun/Vite.

**Spec:** `docs/superpowers/specs/2026-09-21-selector-readiness-search-design.md`

## Global Constraints

- Preserve tenant, BC, connection, route, credential and account-scope isolation.
- Search must read only persisted scene facts/pages and must not call TikTok.
- CTA, VBO and regions remain required for preview/build validation.
- Draft writes remain disabled until the draft status is `READY`.
- No real advertising, authorization or provider writes during development or deployment.

## Review Focus

- A partial multi-page Minis catalog must remain pending rather than expose incomplete search results.
- A completed resource from an expired or differently routed job must not be reused.
- Search terms containing `%`, `_` or mixed case must be treated as literal user text and remain case-insensitive.
- Empty search must preserve the existing page ordering, total and selection behavior.
- A selected identity or Minis value must still be validated against the exact catalog job used by the GET response.

---

### Task 1: Resource-specific catalog readiness

**Files:**
- Modify: `backend/app/modules/builds/mini_selection.py`
- Modify: `backend/app/modules/builds/identity_selection.py`
- Test: `backend/tests/modules/builds/scene_jobs/test_draft_bootstrap.py`

**Interfaces:**
- Consumes: persisted `SceneJob` and `SceneJobPage` rows from the existing scene worker.
- Produces: `account_catalog(..., resource: Literal["identity", "minis"]) -> SceneJob | None` and resource-ready selector responses.

- [ ] **Step 1: Write failing readiness tests**

Add tests that persist a current PENDING job after identity completion and after complete Minis pagination, assert the matching selector is available, and assert a partial Minis pagination remains pending.

- [ ] **Step 2: Run readiness tests and verify failure**

Run: `uv run pytest backend/tests/modules/builds/scene_jobs/test_draft_bootstrap.py -k 'catalog_available_before_scene_complete or partial_minis_catalog' -v`

Expected: FAIL because `account_catalog` currently requires `SceneJob.status == "COMPLETE"`.

- [ ] **Step 3: Implement resource readiness**

Select a current scoped job without requiring whole-scene completion, then require the requested resource's persisted completion evidence. Preserve expiry, frozen-route and `_account_scope` basis checks.

- [ ] **Step 4: Run focused readiness tests**

Run the command from Step 2 and expect PASS.

### Task 2: Cached catalog search API

**Files:**
- Modify: `backend/app/modules/builds/api.py`
- Modify: `backend/app/modules/builds/mini_selection.py`
- Modify: `backend/app/modules/builds/identity_selection.py`
- Test: `backend/tests/modules/builds/scene_jobs/test_draft_bootstrap.py`

**Interfaces:**
- Consumes: optional trimmed `query: str` from both selector GET routes.
- Produces: filtered `DraftMinis` and `DraftIdentities` responses whose totals and pagination describe the filtered result set.

- [ ] **Step 1: Write failing search tests**

Add mixed-case name/username/ID tests, special-character literal tests, no-match tests and a Minis cross-page search test. Assert the transport double receives no additional TikTok call.

- [ ] **Step 2: Run search tests and verify failure**

Run: `uv run pytest backend/tests/modules/builds/scene_jobs/test_draft_bootstrap.py -k 'selector_search' -v`

Expected: FAIL because selector routes do not accept or apply `query`.

- [ ] **Step 3: Implement bounded cached search**

Normalize the optional query once, filter identity facts and active MINI_SERIES options from all persisted Minis pages, then paginate the filtered Minis values at 50 items per page. Keep empty-query behavior unchanged.

- [ ] **Step 4: Regenerate the OpenAPI client and run backend tests**

Run the repository client-generation command, then rerun the tests from Step 2 and the full build-module suite.

### Task 3: Selector search UI and perceived loading

**Files:**
- Modify: `frontend/src/features/builds/useDraftMinis.ts`
- Modify: `frontend/src/features/builds/useDraftIdentities.ts`
- Modify: `frontend/src/features/builds/MiniTargetPicker.tsx`
- Modify: `frontend/src/features/builds/IdentityTargetPicker.tsx`
- Test: existing frontend unit/component tests near `frontend/src/features/builds/`

**Interfaces:**
- Consumes: `query` on generated `BuildsService.minisOptions` and `identityOptions` methods.
- Produces: BC-style explicit search forms, filtered empty states, reset pagination on search, and retained catalog data while polling.

- [ ] **Step 1: Write failing component tests**

Assert each dialog renders a labeled search field, submits a trimmed query, resets Minis to page 1, and shows a filtered empty state without hiding already loaded catalog data during refetch.

- [ ] **Step 2: Run component tests and verify failure**

Run the focused frontend test command from `frontend/package.json`; expect failure because the fields do not exist.

- [ ] **Step 3: Implement the search forms**

Use shadcn `Field`, `FieldLabel`, `Input` and `Button`; store input separately from submitted search; include search in TanStack query keys and requests; retain previous data during refetch.

- [ ] **Step 4: Run frontend tests, typecheck and production build**

Run focused tests, lint/typecheck, and `bun run build`; expect all to pass.

### Task 4: Regression, documentation, commit and staging deployment

**Files:**
- Modify: `docs/implementation-progress.md`
- Create: `docs/validation/2026-09-21-selector-readiness-search-staging.md`

**Interfaces:**
- Consumes: verified backend/frontend implementation and the staging deployment runbook.
- Produces: one focused local commit and a fully backed-up, health-checked Singapore staging release.

- [ ] **Step 1: Run regression and inspect the diff**

Run all relevant backend build tests, frontend tests/build, generated-client check, `git diff --check`, and inspect `git status -sb` plus the scoped diff.

- [ ] **Step 2: Update progress and validation records**

Record cause, behavior, commands, results, staging evidence and the absence of TikTok writes.

- [ ] **Step 3: Commit the focused change**

Verify the repository root, stage only named files, inspect the staged diff and commit with a scoped subject.

- [ ] **Step 4: Back up and deploy staging**

Follow `docs/runbooks/deployment.md`: verify target/configuration, create and validate database/Redis/project/config backups, drain workers, deploy the immutable release, run migrations if any, restart all units and restore timers.

- [ ] **Step 5: Verify staging**

Check health, service restart counts, worker pings, migration head, selector API behavior against cached data, frontend assets and logs. Do not perform TikTok or advertising writes.
