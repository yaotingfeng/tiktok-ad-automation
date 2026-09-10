# R2 browser-to-API acceptance

This is a bounded Task8 test of the actual upload UI, FastAPI handlers, password/JWT authentication, PostgreSQL transactions and durable validation handoff. It closes the gap between browser tests that mock application HTTP and backend tests that do not exercise the browser.

## Run

Prerequisites: installed frontend dependencies/Chromium, the repository's Python dependencies at `.venv/bin/python` (a symlink to `backend/.venv` is sufficient), `ffmpeg`, and a local PostgreSQL role allowed to create disposable databases. `DATABASE_URL` must already name a **test** database on `127.0.0.1`, `localhost` or `::1`. Load its credentials through your normal private environment; do not commit them.

From `frontend/`:

```sh
bun x playwright test --config=playwright.r2-acceptance.config.ts
```

The config builds the real frontend, starts `tests.acceptance.r2_browser_server` with `PYTHONPATH=.`, and uses one Chromium worker. It does not reuse an existing server. Ports are fixed and isolated: application **5293**, synthetic HTTPS storage **5294**. Port 8011 and the existing acceptance services are untouched. The config explicitly sets `VITE_API_URL` empty so the build addresses this same-origin application. Application settings are imported from a private temporary working directory with no parent `.env`, so deployment credentials are not read.

The server validates the supplied test/loopback database URL **before connecting**, creates a new `r2_browser_<random>_test` database, records ownership under `.runtime/r2-browser-acceptance/<database>/ownership.json`, and applies actual Alembic migrations only to that new database. It never migrates or resets the database named in the supplied URL. On graceful exit it closes its services/pool and drops only the database it just created. SIGTERM is converted to a Python unwind after Uvicorn's shutdown so cleanup is not bypassed by Uvicorn's signal re-raise. Abrupt SIGKILL may leave a database; only an ownership-marked database may be cleaned manually. The marker contains no DSN or credential.

## Exercised behavior

- A fresh synthetic tenant, numeric 20-digit BC, operator and unrelated tenant are seeded. No material, received object, available asset or completed job is inserted as a fixture.
- Chromium enters the real username and password login form. The production login handler issues the JWT; application requests use that token. No auth dependency is overridden and no token is injected into browser storage.
- The user selects three actual MP4 files and clicks **开始上传 3 个文件**. `ffmpeg` generates the small clip; the third file appends a valid ISO BMFF `free` box to exceed 16 MiB and exercise two actual part transfers.
- Actual parent/chunk/resume/sign/part-receipt/complete APIs execute. Browser IndexedDB, File slicing, SHA256 proofs and direct fetch are real. The test never uses `page.route`, `route.fulfill`, fake business responses or direct workflow state transitions.
- A loopback HTTPS storage gateway receives the actual PUT bodies into memory, verifies each issued URL's object/part, expected Content-Length, origin and expiry, and returns actual body-derived ETags. The storage adapter uses boto's real SigV4 presigner with exclusively synthetic credentials. Browser PUTs contain no application Authorization, cookies or Referer. CORS exposes ETag. A fresh local certificate exists only in a temporary directory; this isolated Chromium context accepts it.
- Production part-receipt handling queries the storage adapter's actual received part ETags/sizes. Production completion enumerates parts, completes the synthetic object, performs HEAD validation, updates real object/budget/session records and inserts the real `materials.validate_original` outbox item.
- The test reads the real summary and bounded file page through authenticated HTTP and independently checks PostgreSQL/storage evidence. Object SHA256 values must equal those of the browser's original files. Reloading the page must not add a completion or PUT. A request scoped to the unrelated tenant must be denied.

Test controls live only in this test entry point, under `/__r2_acceptance__/`: seed synthetic identity, provide the generated clip, health and safe aggregate evidence. Production routers and shared acceptance scenarios are unchanged.

## Scope limits

The storage client/gateway is a **transport double**, not Cloudflare R2. It does not establish Cloudflare's signature enforcement, service CORS, TLS, regional behavior, deletion semantics, throughput or deployed credentials. Actual SigV4 generation here is local; the synthetic gateway recognizes issued capabilities rather than implementing a production S3 authentication server.

The worker is deliberately not started. This test ends at **stored original + unpublished validation outbox**, with platform-available and cleaned counts both zero. It does not execute ffprobe validation, Redis/Celery publication, TikTok upload/readback, target relay/share, cover generation or cleanup. Those are covered by the separate backend pipeline/worker tests and deployment gates. The free-box file exercises the browser's part boundary; it is not a bandwidth or large-file capacity benchmark. The test's displayed counters cannot be interpreted as TikTok ingestion success.

## Recorded result

Implementation baseline: committed `dc3f5c7` plus browser adapter `8c69539` (local cherry-pick `943f297`). Generated client matches the actual full-app OpenAPI at `301a46f`; no public DTO was recreated by hand.

On 2026-09-10, the real browser scenario passed in **5.5s**; total run including frontend build, server setup and migrations was **13.2s**. Evidence: one ingest session, three materials/objects in `stored`, four successful HTTPS PUTs, **16,782,295** received bytes, four completed permission receipts, three persisted pending validator dispatches, zero available account assets and zero credential-header violations. All completed-object hashes matched the original browser inputs. The run-owned database was automatically dropped and its ownership marker records `dropped: true`.

The first run reached real login but failed a test locator that assumed a BC dropdown. With a single BC the product correctly renders fixed BC text; the assertion was corrected without changing production UI. That run also exposed Uvicorn's SIGTERM re-raise bypassing outer cleanup; the test harness now unwinds safely, and its first ownership-marked database was explicitly removed. No unrelated database or service was altered.

The final repeat after explicit same-origin configuration and private-settings isolation also passed: **5.6s** for the scenario, **15.5s** total. Its independent database was automatically dropped as well.

Targeted Ruff, Ty (with the isolated backend import path), frontend TypeScript/Biome and the real production build pass. Shared CI registration is intentionally left to root integration; this config is a separate short lane and does not rerun the advertising acceptance suite.
