# P02 Task 4 — resumable account discovery

Delivered Task 4 only on `task/p02-discovery`. Task 3 prerequisite `c8b4d42` was cherry-picked as `e101f46`; the directory schema was handed to the root early as `596c1ae`. Schema revision is `02c_directory`, parent `02b_connections`; this history was not changed after handoff.

## Implementation

- Six plan models: `DiscoveryRun`, `DiscoverySeen`, `TenantBC`, `AdvertiserAccount`, `BCAccountAccess`, `ExternalAssetOwner`. Tenant composite foreign keys cover BC/account/connection/run/candidate relationships. PostgreSQL enforces one active generation per connection and external asset owner uniqueness. Conflicting accounts remain in both tenants' directories; the second tenant is blocked.
- `save_directory_page` saves normalized rows and page evidence atomically, rejects gaps/after-end pages, and deduplicates completed pages. `finalize_directory` requires all BC pages and every BC's contiguous account pages with end evidence. Only completion retires unseen access for this connection and promotes candidate credentials after checking both the run and attempt base version.
- `accounts.discover` advances a persisted work record through AUTHORIZED → BCS → ASSETS → DETAILS. One official SDK call is made per dispatch. The genuine authorized-advertiser endpoint is unpaginated. BC/asset responses missing a supported `list/page_info` end structure fail closed. Missing advertiser metadata stays visible and blocked; advertiser details are requested only for the authorized intersection.
- Each call consumes a fresh shared App/endpoint/tenant/account admission lease through Task 3's wrapper. Admission denial and Redis failure preserve the same page, keep `sent_count` unchanged, and enqueue a due outbox record. No worker sleeps or SDK retries are used.
- Run claims and a future recovery outbox record are committed before the network call. No database transaction or row lock spans SDK execution. A completed revision cannot be re-applied, and a crashed claim can be recovered after expiry.
- Candidate SDK use decrypts the tenant-validated attempt in its own request client. It never replaces the live connection token before successful finalization. Persisted errors contain stable codes only.
- Celery registration imports the resource handler. Production requires a prefork child and an effective hard deadline at most 45 seconds (soft 40 seconds), rejecting eager/direct/unbounded execution and longer request overrides. Admission leases must exceed 50 seconds. A watchdog recovers the page after 60 seconds.

## Verification

Initial `test_discovery.py` failed on the missing discovery module before implementation. All development API calls used the installed official SDK with a fake urllib3 transport; no live TikTok request or advertising write was made.

Commands from `backend`:

```text
TEST_REDIS_URL=redis://127.0.0.1:16379/13 uv run pytest --import-mode=importlib tests/modules/accounts tests/modules/tenants tests/jobs -q
189 passed (41 new Task 4 tests)
uv run ruff check app/modules/accounts app/integrations/tiktok/accounts.py app/jobs/celery_app.py tests/modules/accounts/test_discovery.py tests/modules/accounts/test_discovery_worker.py tests/modules/accounts/test_account_adapters.py
All checks passed
uv run ty check app/modules/accounts app/integrations/tiktok/accounts.py
All checks passed
uv run alembic downgrade 02b_connections
uv run alembic upgrade head
uv run alembic check
Roundtrip passed; no new upgrade operations
uv run python -m compileall -q app/modules/accounts app/integrations/tiktok/accounts.py
Passed
```

The combined suite needs `--import-mode=importlib` because the existing account and jobs suites both contain `test_admission.py`; no project-wide pytest configuration was changed in this task.

Behavioral coverage includes real PostgreSQL claim races, two concurrent workers sending once, tenant foreign key failures, single-generation enforcement, candidate isolation/promotion, old-version rejection, two BC pages and two account pages per BC, second-page failure/resumption with old access retained, duplicate page/delivery, missing end evidence, metadata blocking, revoked membership, cross-tenant payload rejection, each endpoint's admission denial/resumption, Redis unavailable, expired claim recovery, and a second PostgreSQL connection acquiring run/connection locks during the SDK transport callback.

## Explicit verification boundary

The four generated method signatures and request serialization were checked against installed SDK revision `f809c396520df2d7b201a9ccc5378d822b728ed3`. Tests exercise its actual synchronous `{data, request_id}` envelope. Response normalizers are fail-closed fixture contracts; P07 still must validate approved-app response examples and capabilities. No role/capability field from a fake is treated as permission evidence: discovery always leaves `can_upload=False`, `can_build=False`, and `permission_state=UNKNOWN` when metadata is present, or `METADATA_INCOMPLETE` when it is absent.

The Mac tests verify the configured deadline and rejection guards; they do not claim a Linux prefork hard-kill integration test. Root accepted Linux worker enforcement testing as a P07 integration boundary. Failed SDK reads remain on the same durable page with run `ERROR`; retrying its candidate attempt via `start_discovery` resumes the same run. Unseen relationships and old credentials are retained during errors.
