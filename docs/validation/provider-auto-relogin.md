# Provider automatic session renewal

## Behavior

Tenant administrators still enter their own Wangyan email/password or Jiashu username/password and perform the initial **Save and verify**. Credentials and candidate sessions use the existing tenant-bound Fernet envelope. No CLI account file, browser cookie jar or global provider account is consulted.

During link preparation, HTTP 401 and Jiashu's documented `10001` mark only the exact expired session. The current business call is not replayed by the session factory. The original durable item continues automatically: login, application discovery and each Jiashu application-options request each consume their own task delivery. Once the session and application evidence are complete, the original business stage continues. Merely browsing connection metadata does not log in.

Automatic refresh uses the current business actor's permission, not administrator impersonation. Administrators manage credentials; operators can continue their authorized link preparation. Every refresh claim and finalization reloads that original actor, connection status, credential version and application verification generation. Concurrent items share one connection-level 60-second lease; PostgreSQL transactions finish before HTTP. Existing provider Celery tasks enforce Linux prefork / 45-second hard deadlines. The refresh service is called only through that bounded production path, performs at most one external request per advance, and uses the original item outbox/watchdog. It does not introduce a second scheduler.

## Recovery rules

- A session-only change preserves the application's existing verification generation and all link/effect identities. Removed applications, changed channel prefixes or changed Minis facts advance the generation so previous business plans cannot silently use different authority/configuration.
- A stale 401 from an old ciphertext cannot invalidate a newer completed session. Disabled connections, changed passwords and revoked actors cannot publish a stale candidate session.
- Successful refresh stages schedule the original item five seconds later. Network errors use durable connection backoff (10/20/40 seconds), not worker sleep. Each phase has at most three attempts; login is also limited to three claims in ten minutes, including reclaim after worker death. Exhaustion persists a ten-minute cooldown, then automatically resumes the original phase. The original item is scheduled directly at that due time, avoiding worker sleeps or repeated five-second polling during cooldown. `unavailable_since` records the uninterrupted outage start and `cooldown_rounds` counts exhausted bursts; operations should investigate continuous unavailability beyond one hour. These fields provide an inspection/alerting basis, not a newly deployed external alert service.
- A candidate session expiring during application/options discovery restarts authentication within the same three-login window. A worker soft deadline retains the current phase with transient backoff; hard worker death leaves the bounded lease recoverable by the existing watchdog.
- Empty application discovery and malformed response shapes stop safely rather than being retried indefinitely as network errors. HTTP 403 does not trigger another login. Failed login credentials stop with `provider_auth_failed`; the administrator updates them in the connection editor. Initial wrong credentials are not labeled as an automatically recoverable session expiry.
- Existing unknown external writes stay unknown. Refresh never repeats a create/generate/save call. A saved-link response lost before receipt can recover by reading that link after login; an unknown Jiashu generation without authoritative evidence remains unknown even after successful login. Successful effects retain their original IDs.
- Migration resumes legacy read/auth-blocked items through their existing exact-revision watchdog. Legacy failed writes still carrying an active effect are deliberately not converted into unsent work; they require reconciliation. A new credential version likewise does not silently resume an old frozen intent.

Unknown Wangyan business error codes are still treated as rejection, not guessed to mean expired credentials. Live backend-specific expiry responses beyond the reviewed HTTP/code contract require their own verified mapping. No actual provider credentials or live calls were used to validate this change.

## Schema and verification

`0016_provider_session_refresh` adds a tenant/connection-bound refresh record with a candidate ciphertext, phase, lease, attempts, due time and discovered application facts. The composite foreign key cascades on connection deletion. No token, password, candidate ciphertext or new refresh field appears in the public connection DTO. The integrated migration follows `0015_username_auth`, forming a single migration head.

Real PostgreSQL tests: `backend/tests/modules/providers/test_auto_relogin.py` covers both providers, same-connection contention and cross-tenant rejection, lock-free HTTP, stale/disabled/password/actor fencing, lost-dispatch repair, bounded backoff, original application generation, changed application configuration, unknown save recovery and non-replay of unknown generation. The initial expiry expectation, continuation-cadence expectation, initial-password classification, transient-outage cooldown, candidate-session expiry and worker soft-deadline recovery were observed failing before implementation, then passed.

Validation on this branch:

- Entire provider suite: **259 passed**, including **26 new tests**; safe fake HTTP only.
- Provider browser suite: **21 passed**, including automatic-renewal presentation; API transport fixtures only.
- Ruff, strict Mypy for the new refresh/service/model/adapter files, and Ty for provider modules pass.
- Fresh migrations were exercised by isolated provider fixtures; explicit 0016 downgrade/upgrade and Alembic model comparison pass.
- TypeScript and Vite production build pass.

Use an explicit private `*_test` DATABASE_URL and independent nonzero TEST_REDIS_URL. Run from `backend`: `PYTHONPATH=. uv run pytest -q tests/modules/providers`. Do not use a business database for these tests. Live credentials, actual reauthentication and live provider write results remain a separate deployment acceptance step.
