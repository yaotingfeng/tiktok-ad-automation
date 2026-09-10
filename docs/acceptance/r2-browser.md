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

## Bounded 20,000-file selection evidence

### Existing evidence and its limits

`frontend/tests/materials.spec.ts`, test **大批导入一次选择 20000 文件仅渲染 100 行并保留同名文件**, already asserts 20,000 accepted selections, an enabled start button, exactly 100 rendered list items on each of two pages, preserved duplicate filenames and zero application POSTs. Its earlier approximately 2.1-second test duration includes navigation, synthetic fixture creation and assertions; it was **not** a measurement of first UI feedback.

`frontend/tests/upload-foundation.spec.ts`, test **20,000 file metadata stays chunked and seek pages stay scoped**, calls `metadataScaleScenario()` in `tests/harness/upload-foundation.ts`. That helper measures `registerFiles()` after the fixture's File objects already exist: 100 registration chunks of at most 200, local IndexedDB writes and a scoped seek page. The latest recorded foundation run was **7.789s** (an earlier run was 6.364s). These are synthetic browser metadata-registration measurements, not selection-feedback latency or real upload throughput. The prior tests/reports contain no browser JS-heap measurement.

### One measured selection

A one-off probe used the production build at `442c0ba`, the real isolated FastAPI/JWT entry point above and Chromium's CDP. It opened the upload sheet, then selected **20,000 synthetic File objects of five bytes each** (100,000 payload bytes), retaining the existing duplicate-name pattern. It did **not** click Start: real database evidence remained zero ingest sessions/materials/validator jobs, and the HTTPS storage gateway received zero PUTs. Only one run reached the measured 20k selection; an initial probe exited before constructing files because it incorrectly expected the intentionally hidden native file input to be visible.

Recorded at 2026-09-10 02:16:36 UTC, on **Apple M4, 10 logical CPUs, Darwin 25.3.0 arm64, Chromium 151.0.7922.34**, 1440×900 viewport, no CPU throttling. This is a single warm-page observation, not P50/P95 or a lower-end-machine guarantee.

| Measurement | Result |
| --- | ---: |
| Construct 20k synthetic File/DataTransfer entries | 1,568.1 ms |
| Assign the prepared FileList to the input | 0 ms at the observed timer resolution |
| Dispatch `change` → enabled 20k start button and 100-row DOM | 16.4 ms |
| Dispatch `change` → two animation frames after the ready DOM | 25.0 ms |
| Rendered file rows, page 1 and page 2 | 100 / 100 |
| Rows intersecting the viewport after ancestor clipping | 7 |
| Connected elements inside the sheet / whole page | 526 / 855 |
| CDP DOM nodes before / after | 1,612 / 3,335 |

A MutationObserver stopped the readiness clock only when the 20k button was enabled and all 100 list rows existed. Two subsequent animation frames are reported separately; this is a paint-opportunity proxy, not a compositor presentation timestamp. IntersectionObserver counted actual viewport-intersecting rows. `querySelectorAll('*')` counted connected elements; CDP DOM counters additionally include text/comment and retained/detached nodes, so the two counts are not interchangeable.

The prepared-FileList → UI response meets the plan's 1-second target in this observation. **The synthetic construction + delivery sequence takes approximately 1.593s and must not be reported as under one second.** Creating File objects in JavaScript is not the native OS file picker's enumeration/read-permission latency; that path was not timed.

### Browser JS heap samples

The probe used CDP `Runtime.getHeapUsage().usedSize`, with explicit `HeapProfiler.collectGarbage` only at the labelled points. No production instrumentation or behavior changed.

| Sampling point | JS heap used, bytes | MiB |
| --- | ---: | ---: |
| Empty sheet, after forced GC | 7,621,100 | 7.268 |
| After constructing the synthetic FileList | 8,127,756 | 7.751 |
| After rendering the 20k selection | 11,931,768 | 11.379 |
| Selection retained, after forced GC | 10,222,384 | 9.749 |

The retained post-GC increase was **2,601,284 bytes (2.481 MiB)**. The largest observed JS-heap sample was 11.379 MiB; **this is not a measured peak**. Sampling can miss allocation/GC spikes and does not account for total native renderer memory, Blob storage, GPU memory, browser process RSS, OS cache or backend Worker RSS. CDP's separate embedder/backing-store fields are retained in the raw artifact but are not relabelled as JS heap or added into a claimed process-memory total.

The one-off probe and raw output are retained locally in the isolated worktree under `.runtime/r2-selection/selection.spec.ts` and `.runtime/r2-selection-metrics.json`. The probe passed in 2.6s (10.4s including setup/build/migrations). Its owned PostgreSQL database was automatically removed; no uploaded file, real storage object or deployment credential was used. Only this documentation supplement is committed.

### Task4 measurements still outstanding

- Native OS selection/drag-and-drop enumeration and first feedback for real 10k/20k files; the synthetic construction and prepared-FileList handoff are separately reported above. A separate 10k UI timing was not collected here.
- Repeated-run latency distributions and behavior on slower or constrained-memory machines; this single desktop Chromium observation is not a general 1-second SLA.
- Actual peak JS heap and full browser/renderer memory during selection, 4×2 active 16 MiB part buffers, hashing and network transfer. Worker RSS belongs to the backend capacity report and cannot be inferred from these browser samples.
- A full 20k browser→FastAPI upload/reload/reselection/receipt-recovery run and long-lived multi-import IndexedDB/storage-pressure behavior. Existing coverage combines 20k synthetic metadata handling, separate backend scale tests, bounded browser recovery fixtures and the three-file real-API scenario above; those are not equivalent to a 20k end-to-end video run.

This supplement establishes bounded DOM rendering and a measured desktop selection handoff. It does not establish video-network throughput, daily ingestion capacity or a true peak-memory acceptance result.
