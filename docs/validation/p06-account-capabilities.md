# Link-free BC capability bootstrap

This prepares current-token account capabilities independently of provider applications, promotion links, draft account acceptance, and Scene reads. No ad or credential write is made to TikTok.

## HTTP and internal contracts

`POST /api/tenants/{tenant_id}/bcs/{bc_id}/capability-refresh` accepts only `{request_id: UUID, connection_id: UUID}`. It requires the current actor's tenant build permission and at least one current read-eligible account grant on that exact BC/connection. UNKNOWN capability is eligible for inspection. The public service `start_capability_refresh(session, *, context, bc_id, connection_id, request_id) -> UUID` flushes job/request/outbox changes; the route commits. Replaying the same request returns the original job; changing connection with that request returns `request_id_conflict`, including concurrent requests. A new request can reuse an active or still-fresh complete job with the same credential and directory basis. The frontend can discover relevant connection IDs and issue these commands without presenting credential configuration to the operator.

`GET /api/tenants/{tenant_id}/bcs/{bc_id}/capability-refresh/{job_id}` requires tenant read permission. It performs no refresh and returns `Cache-Control: no-store`. POST returns the same public DTO:

```text
job_id, bc_id, connection_id
status: PENDING | COMPLETE | BLOCKED | STALE | FAILED
phase: READ | PUBLISH | DONE
remote_read_count: int
remote_total_count: int | null
published_account_count: int
permissions_known: bool
error_code: string | null
created_at, completed_at: datetime | null
```

`remote_read_count` counts distinct remote BC assets received; `published_account_count` counts existing local grants checked/published, including denials. Neither count claims build eligibility. No raw scope receipt, token, credential ciphertext, claim nonce or dispatch payload is public. A lost POST response can safely replay its original request UUID; do not generate a new key merely because the response was lost.

`get_capability_evidence(session, *, context, bc_id, advertiser_id, connection_id) -> CapabilityEvidence | None` is a pure local read for Scene/preview. It requires current read eligibility, full COMPLETE publication, matching credential/directory basis and unexpired evidence. Its frozen DTO contains `job_id`, `evidence_ids=(job_id,)`, `page_number`, `observed_at`, `expires_at`, `scope_verified`, `can_build`, `can_upload`, `source_revision`, `basis_digest`. Page evidence has the composite identity `(job_id, page_number)`; a missing account role has no matching page and cannot establish capability. `observed_at` is the actual role page time (first page for a complete absence result), not job completion time. The reader never decrypts, locks rows, calls SDK/Redis, flushes or enqueues. It rechecks current grant flags; a stored positive job cannot undo a current denial.

## Complete evidence and publication

Each `accounts.refresh_capabilities` resources task performs one fixed official `BCApi.bc_asset_get` GET, no filtering argument, `asset_type=ADVERTISER`, `page_size=50`. The documented default user is the auth-code issuer. Each page requires correct numeric metadata, exact page size/counts, and known advertiser roles. Remote IDs are unique within a page and across the whole job via scoped evidence rows and a primary key. No role is inferred from account visibility. Remote assets absent from the local directory may be retained as role evidence but never create a local grant.

Only after every distinct page is present does the job enter PUBLISH. Each local transaction joins at most 100 existing connection-local grants to the complete role evidence, rechecks current account/grant operational facts, publishes capabilities and updates progress/outbox together. All existing local rows are checked, so a missing remote role becomes UNKNOWN and false. No grant is published while remote pagination is incomplete. A publish page rollback also rolls back its progress and permissions. The evidence reader remains unavailable until every publish page has completed.

Encrypted OAuth scopes are strictly decoded; omitted/malformed receipts retain UNKNOWN capability and leave the connection ACTIVE/readable. Verified ADMIN/OPERATOR plus parent scope 2 permits build; plus 6, 61 or 611 permits video upload. ANALYST has known negative permissions. Narrow ordinary ad scopes do not stand in for an unverified Smart+ scope mapping. Sources: [BC user asset roles](https://business-api.tiktok.com/portal/docs?id=1739432717798401), [permission hierarchy](https://business-api.tiktok.com/portal/docs?id=1753986142651394); complete source hashes are recorded in `docs/contracts/tiktok-minis-source-hashes.json`. The SDK remains pinned at `f809c396520df2d7b201a9ccc5378d822b728ed3`.

## Concurrency, recovery and age

A server-side PostgreSQL ordered aggregate hashes the local directory inputs into one small digest; Python never loads the entire directory to compute the basis. The basis includes grant membership/authorization/activity/discovery generation and account metadata/ownership/status; publishing capability flags does not invalidate its own work. Tenant/BC/connection/version plus that basis deduplicates active jobs. An independent request table preserves all replay identities.

Workers lock connection then job, durably claim the page, and close the database Session before SDK transport. An actual admission lease covers each GET. The worker must run in a Celery prefork process with effective hard limit at most 45 seconds; socket timeouts alone are insufficient. Receipt publication rechecks actor, current credential/directory authority, revision, nonce and server deadline. Stale receipts cannot write evidence. Current tenant roles are always loaded from the database.

`accounts.repair_capabilities` scans at most 100 due jobs. Expired claims and lost published messages reuse the exact current dispatch ID, business key, payload and revision. A merely unpublished message retains its broker backoff. Invalid dispatch identity stops the job. Permission/connection denials become explicit terminal BLOCKED results; malformed role evidence becomes FAILED; STALE and terminal failures require an explicit new request once the cause is resolved. Admission/transient local failures use bounded exponential backoff in the same business job. No repair operation calls TikTok.

Set `BC_CAPABILITY_MAX_AGE_SECONDS=14400` in deployment configuration. Valid range is 60..86400 seconds. This is an engineering cache and complete-chain age policy, **not a TikTok quota**. Adjust it for BC size and actual queue scheduling: one page per task means 10,000 accounts require about 200 remote deliveries. Origin is the first remote page's `observed_at`; later pages and completion never extend it. A stale chain stops explicitly instead of restarting forever. Changing this setting invalidates the previous directory basis. Scene's separate ten-minute cache does not impose a ten-minute deadline on this BC chain.

## Integration ownership

Migration `0005e_account_capabilities` follows `0005d_build_previews` and adds only capability job/request/page/asset tables. Root integrates the Alembic model import, capability router, Celery module import and control-queue Beat registration every 60 seconds. The real consumer module is `app.modules.accounts.capability_tasks`; this delivery does not edit shared Scene, SDK compiler, draft or preview files. Root consumes the pure evidence reader and schedules capability commands before strict build account acceptance. Local Scene/status GETs never schedule capability work.

Verification uses isolated real PostgreSQL/Redis and the pinned SDK's offline transport. Tests cover 205 rows/five reads/three publish pages, multiple connections, role/scope and legacy tokens, concurrent request conflicts, duplicate pages, actor/directory/credential/nonce/deadline fences, no row locks during I/O, exact dispatch recovery, publish rollback, local READ ONLY evidence, API viewer reads, and effective worker limits. No real TikTok, S3, provider or OAuth business API was called. Worker registration and transport tests are not a claim that a live deployment has been started.
