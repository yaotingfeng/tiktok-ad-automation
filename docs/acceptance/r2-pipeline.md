# R2 pipeline acceptance — 2026-09-10

This is offline engineering evidence on dedicated PostgreSQL and Redis. No real
R2, TikTok, provider or advertising operations were performed. Ingest and cleanup
switches are enabled only inside the isolated test fixture; deployment defaults
remain disabled.

## Reproduce

From the backend, with the repository's dedicated `_test` PostgreSQL database and
nonzero isolated Redis database configured:

```sh
python -m pytest tests/acceptance/test_r2_pipeline.py \
  tests/modules/materials/test_cleanup_abandoned.py \
  tests/modules/materials/test_validation_dispatch.py \
  tests/modules/materials/test_remote_only_distribution.py::test_frozen_material_steps_use_verified_targets_after_original_cleanup \
  -q --tb=short
```

Local verification used the private `.runtime/run-with-env.py` launcher and the
project Python 3.14 virtual environment. Never copy operational credentials into
this harness. It creates its own tenant, operator, connection and grants; teardown
removes only that tenant and its exact remaining global-budget contribution.
Non-local DNS is rejected. R2 factories and the pinned official SDK's urllib3
transport are replaced with deterministic doubles. Unsupported SDK requests
exhaust the explicit response queue and fail the test.

## Actual boundaries exercised

| Stage | Production entry point and evidence |
| --- | --- |
| Registration | FastAPI router + signed JWT: POST session, bounded 200-file chunks, exact replay, seal. Material IDs and object generations are created by the API. |
| Receiving | POST resume, part-urls and complete. Real PG object/budget/permission/outbox transactions; synthetic multipart parts and bytes at the R2 transport boundary. No final stored/verified/ready rows are seeded. |
| Validation | Exact persisted validation dispatch payload passed to `validate_original`; real stream, MD5/SHA-256 and actual ffprobe on a generated 160×240 MP4. Strong digest and the exact source outbox commit together. |
| Source | `run_source_upload`: durable source selection, URL-signing/OriginalUse and pinned SDK multipart `UPLOAD_BY_URL`, then actual SDK INFO serialization. Actual returned source VID is retained; no MID is required. |
| Cleanup | Source strong INFO schedules cleanup in its success transaction. `run_cleanup` sends exact DeleteObject and verifies HEAD 404, then releases actual object/tenant/global/session accounting. |
| Target | After cleanup and clearing S3 credentials, real readiness and `ensure_target_asset` choose authorized URL relay. Actual SDK source INFO → target URL POST → exact target INFO; no original-byte read. |
| Cover/resources | Real cover job, SDK video INFO, URL image upload and strong image INFO. `ad_assets` compiles the actual target VID and image ID from the resulting mapping. |
| Frozen execution | Separate existing regression invokes real preview/submission expansion and MATERIAL execution against frozen IDs after originals become unavailable. Its fixture supplies verified target mappings; it is supplementary resource evidence, not part of the above upload lineage. |

Six complete MP4 flows cover normal success, lost CreateMultipartUpload receipt,
lost CompleteMultipartUpload receipt, lost DeleteObject receipt, unknown source
POST result and unknown target POST result. Unknown SDK results recover by the
same persisted account/name/digest search followed by strong INFO; no second video
POST occurs. Each flow asserts exactly one multipart create, one multipart
complete, one delete, one original GET and two video URL POSTs (source + target).
The source UNKNOWN case retains its OriginalUse and has no cleanup job before
strong INFO. Video/cover aspect ratios are verified. Filenames and content digests
survive cleanup; operation evidence and outbox payloads contain no transient URLs.

The test invokes production worker bodies synchronously. It does not establish
Linux prefork hard-kill behavior, broker delivery, browser upload scheduling or
DNS-inclusive deadlines. Only the lost-delete test advances its retry timestamp
to exercise the due retry without sleeping; no successful result is synthesized
in the database.

## 1,000-file mixed control failures

All 1,000 distinct files are registered through JWT routes in five 200-item
chunks; every chunk is replayed exactly. Every file receives one tested condition:

| Condition | Files | Outcome |
| --- | ---: | --- |
| Capacity below one file | 200 | Waiting; no storage request or permission issued. |
| Stale operation revision | 200 | HTTP 409; no storage request. |
| Cancel before upload | 200 | Cancelled locally; no storage request. |
| Lost create receipt | 200 | Exact upload recovered by listing, cancelled, exact Abort/ListParts/HEAD confirmed by cleanup worker. |
| Lost complete receipt | 200 | Exact receipt recovered by HEAD, cancelled, Delete/HEAD confirmed by cleanup worker. |

Final counts: accepted 1,000, uploaded 200, cleaned 400, source-ready 0; reserved
and stored bytes both zero. Transport counts are create 400, complete 200, abort
200, delete 200, SDK calls zero. Ten seek pages retain exactly the original 1,000
IDs without omission or duplication. Summary + one file page use at most 20 SQL
statements and no COUNT/SUM aggregate. The independently run scenario passed in
23.41 seconds on the local machine; this is synthetic control-plane elapsed time,
not platform throughput. It does not claim 1,000 ffprobe/SDK uploads, actual HTTP
429/expired-signature enforcement, process restarts or a measured worker RSS peak.
Those wider operational measurements remain separate acceptance requirements.

## Review and verification

The first actual API → validator test exposed a stage-handoff bug: a completed
multipart operation retained its claim's future `next_attempt_at`, so the initial
validator delivery silently returned. Root fix `2df5e85` resets this timestamp only
when creating a new validation stage; existing dispatch retry backoff remains
unchanged. The full lineage test was red before this fix and passes with it.

Scoped review of `bc1c9ba` plus `8642e78` found no unresolved cancellation defect.
Six existing real-PG cases cover source UNKNOWN, expired unknown PUT use, lost
abort, late parts, exact abort and exact delete. An independent seventh probe
replaces the cleanup claim during Abort and confirms that the old response cannot
overwrite the new claim or release its five reserved bytes.

Combined focused verification: **24 passed in 29.07 seconds**. Ruff and diff
whitespace checks pass. A follow-up run of the six complete flows passed in 2.59 seconds and verifies
the added explicit one-GET and URL-persistence assertions. The Starlette/httpx deprecation warning is preexisting.

Live R2 multipart compatibility, exact vetted TikTok media hosts, byte-equivalent
source relay, real source → delete → target compatibility, Linux prefork recovery,
daily throughput and deployment memory remain pending an authorized environment.
This report does not authorize enabling automatic cleanup.
