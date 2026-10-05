# API Account Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an explicit API authorization “同步账户” action that re-reads the full visible BC/account directory with the existing active API credential and publishes the complete snapshot.

**Architecture:** Add a manage-only API endpoint that creates a connection-scoped discovery run without an OAuth candidate. Extend the official API discovery gateway to read from active connection credentials, while preserving candidate OAuth behavior. Add a generated-client endpoint and a connection-details action; API sync is whole-connection, MCP remains per-BC.

**Tech Stack:** FastAPI, SQLModel/PostgreSQL, Celery dispatch, official TikTok Python SDK, React/TanStack Query, OpenAPI-generated TypeScript client.

**Spec:** Existing API directory discovery contract in `backend/app/modules/accounts/tasks.py` and `backend/app/modules/accounts/api_directory.py`.

## Global Constraints

- API calls use the official Python SDK and must remain read-only directory calls.
- The complete staged directory is published atomically; partial pages never update live access.
- Sync is tenant-scoped, connection-scoped, manage-protected, and cannot run for disabled/non-API connections.
- Existing OAuth candidate discovery behavior must remain unchanged.
- No live TikTok or advertising write calls are used by tests.

## Review Focus

- Active API connection with no OAuth candidate must use encrypted connection credentials and publish successfully.
- A second sync while one is running must return the existing run rather than create duplicate active runs.
- Disabled, MCP, cross-tenant, and unauthenticated connections must be rejected.
- Frontend must show API sync progress and invalidate BC/account projections after completion.
- Existing OAuth candidate discovery must continue requiring candidate credentials.

### Task 1: API sync run creation and worker path

**Files:**
- Modify: `backend/app/modules/accounts/tasks.py`
- Modify: `backend/app/integrations/tiktok/official/bootstrap.py`
- Modify: `backend/app/modules/accounts/router.py`
- Test: `backend/tests/modules/accounts/test_api_directory.py` or existing accounts route tests

- [ ] Add a failing test for creating an active API connection sync run, duplicate-run reuse, and rejection of non-API/disabled connections.
- [ ] Run the focused test and verify it fails because no endpoint/service exists.
- [ ] Implement `start_connection_discovery(session, context, connection_id)` and queue the existing `accounts.discover` worker with `candidate_attempt_id=NULL`; keep run actor, credential revision, stage, and revision consistent with OAuth discovery.
- [ ] Extend the official API gateway factory with an active-connection mode that decrypts `TikTokConnection.credential_ciphertext`, validates `validate_run`, and uses normal API admission for directory reads; retain candidate mode unchanged.
- [ ] Allow the worker to process active API runs while continuing to reject malformed API runs and all MCP runs.
- [ ] Add `POST /tenants/{tenant_id}/tiktok/connections/{connection_id}/sync`, returning `McpSyncResult` with the discovery run ID.
- [ ] Run focused backend tests and Ruff.

### Task 2: Client and connection details UI

**Files:**
- Modify: `backend/openapi.json` or generated source if required
- Modify: `frontend/src/client/sdk.gen.ts`
- Modify: `frontend/src/client/types.gen.ts`
- Modify: `frontend/src/features/accounts/ConnectionsPage.tsx`
- Test: `frontend/tests/accounts.spec.ts` or existing connection page tests

- [ ] Add a failing UI test for an API connection showing “同步账户”, submitting the API sync endpoint, and displaying queued/running state.
- [ ] Regenerate the OpenAPI client after the backend route exists.
- [ ] Add the API mutation in the connection details sheet; label it “同步全部账户” to distinguish it from MCP’s per-BC sync.
- [ ] Disable the action for disabled, discovering, or in-flight connections; invalidate connection, BC, account, and default-authorization queries after the mutation starts/completes.
- [ ] Keep MCP’s existing per-BC controls unchanged.
- [ ] Run focused frontend tests, TypeScript, Biome, and build.

### Task 3: Documentation and verification

**Files:**
- Modify: `docs/implementation-progress.md`
- Modify: `docs/validation/2026-10-05-api-account-sync.md`

- [ ] Record the endpoint, whole-directory semantics, and validation limitations.
- [ ] Run `git diff --check`, focused backend/frontend checks, and the available deterministic regression suite.
- [ ] Commit with `accounts: add API account sync`.
