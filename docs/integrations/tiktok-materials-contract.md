# Official TikTok material SDK contract

Pinned revision: `f809c396520df2d7b201a9ccc5378d822b728ed3`, `python_sdk`, import `business_api_client`. Evidence below is installed official source inspection and offline transport tests. No real TikTok upload, sharing, S3 write, account permission verification or live response acceptance was performed.

## Methods and wire shapes

The installed [FileApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/file_api.py) defines:

| Method | Endpoint suffix | Contract |
| --- | --- | --- |
| `ad_video_upload(access_token, **kwargs)` | `file/video/ad/upload/` | Multipart kwargs: actual `advertiser_id`, `upload_type=UPLOAD_BY_FILE`, local `video_file` path, internal UUID `file_name`, MD5 `video_signature`. JSON `body` is rejected by the generated method. Auto binding/fixing are disabled. |
| `ad_video_info(advertiser_id, video_ids, access_token, **kwargs)` | `file/video/ad/info/` | Account-scoped GET; SDK permits at most 60 IDs according to its documentation. This worker requests one VID. |
| `ad_video_search(advertiser_id, access_token, **kwargs)` | `file/video/ad/search/` | Account-scoped paginated GET; page size 100. `FilteringVideoAdSearch` supports `material_ids`, `video_ids`, `displayable`, dimensions and ratio, **not filename** in this revision. Unknown uploads therefore scan complete pages and correlate exact internal UUID name plus MD5 locally. |

[CreativeManagementApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/creative_management_api.py) exposes `creative_asset_share(access_token, body=AssetShareBody(...))`. The body contains source `advertiser_id`, `asset_type=VIDEO`, typed `material_ids` (MID), and `shared_advertiser_ids`. Existence of this generated method does not prove shared-asset permissions or support for a particular source/target pair. Task 3 implements source upload/readback; Task 4 owns actual sharing/fallback decisions.

The official synchronous [ApiClient](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api_client.py) checks nonzero codes and returns `{data, request_id}`, removing `code/message`. Upload `data` is normalized as exactly one row in an array; read `data` has `list`, search also has validated `page_info`. A supported generated model envelope must have integer `code=0`. Arbitrary dictionaries, missing fields, boolean success codes and untyped MID-only results fail closed.

Official [video info documentation](https://business-api.tiktok.com/portal/docs?id=1740050161973250) describes `displayable`, `signature`, `video_id`, and `material_id` in the account's returned video list. The implementation requires exactly one row with `displayable is True`, matching original MD5 `signature`, and a nonblank VID before marking an account mapping `available`. MID remains separate. If account readback returns a different VID with matching content evidence, that actual returned VID is persisted. `available` represents verified library availability, not ad policy approval or guaranteed placement support.

Temporary preview/cover signed URLs, arbitrary platform messages, tokens and complete response bodies are not stored as operation evidence. Only validated identifiers and locally generated recovery/error fields are retained. SDK wire loggers are disabled by the shared credential scope; credentials are freshly resolved inside a short-lived `sdk_client`, removed on exit, and never carried in a queued message.

## Durable execution and bounded resources

Source upload task `materials.upload_original` has a 900-second prefork hard process deadline (890-second soft limit), 960-second database claim, and upload admission lease strictly greater than 905,000 ms. Read/reconciliation task `materials.verify_original` has a 45-second hard deadline (40-second soft limit), 60-second claim, and admission lease strictly greater than 50,000 ms. Production handlers reject eager/direct/solo/threads execution and unsafe hard-limit overrides before network work. SDK socket timeouts are upload `(10, 300)` and read `(5, 30)`; socket limits alone do not bound whole work units.

S3 downloads stream through the real Task 2 `open_original` adapter into a private temporary file; the adapter closes its DB session before I/O and cleans its stream/path on ordinary exit or cancellation. No TikTok admission lease is held while downloading. The upload/download plus SDK serialization, DNS and transfer remain within the same prefork process deadline. A hard-killed process cannot run Python cleanup; operating-system temporary-directory cleanup remains a deployment concern.

The pinned SDK itself reads a complete file in `files_parameters` and urllib3 constructs multipart data in memory. This integration does **not** claim end-to-end streaming or unlimited upload size. `MATERIAL_SDK_MAX_UPLOAD_BYTES` defaults to 256 MiB and blocks larger TikTok uploads before S3 retrieval while preserving the stored original. This is an engineering memory boundary, not a TikTok platform maximum. `MATERIAL_SDK_UPLOAD_MAX_INFLIGHT` defaults to 1; the upload endpoint's real shared Redis admission policy must not exceed it. Each worker process needs headroom for multiple copies plus SDK/runtime overhead; container memory limits, worker concurrency, large-file capacity tests and operational temp cleanup are P07 deployment verification. No change to the vendor SDK or alternate HTTP upload implementation is made.

Each request uses fresh shared Redis admission keyed by developer App ID + endpoint + tenant + actual advertiser. Denial reschedules with the reported delay without setting `sending`; release happens in `finally`. Explicit configuration is required; there is no production local limiter or fake admission.

## Operation and recovery rules

`MaterialAssetOperation` is the common file/account send authority. Its unique unresolved operation and token-fenced claim are shared with target distribution. `MaterialUploadAttempt` preserves the actual account/connection selected for each source attempt. No permanent material account or drama association is written. Upload receipt VID/MID remain in `upload_video_id`/`upload_mid` even if later readback returns different account VID/MID. A pre-existing verified mapping for the same account prevents a new source send.

The worker commits its operation claim plus a delayed recovery outbox record before S3 or TikTok I/O, rechecks account/tenant permissions and fresh credentials immediately before sending, commits `sending`, closes the DB session, and then invokes the generated SDK. Exactly one SDK request occurs per task. Upload receipt commits typed identifiers with `verifying`; a later read task creates the available mapping only after content and permission verification. Stale completion tokens cannot publish results.

Recovery records bind to the precise claim ID and become no-ops once that claim ends. Normal successor records carry a local operation revision that advances only when a worker actually claims work, so delayed watchdogs cannot invalidate unpublished successors or grow parallel recovery chains. Replayed upload messages never resend an unresolved operation or bypass it by changing advertiser.

Expired `sending`, timeouts, unexpected responses and uncertain write failures enter `result_unknown`. Known VID uses account info; no VID uses complete account search by exact internal UUID filename and content MD5, followed by an info call. Empty listings and temporary invisibility are not authoritative absence and never enable a second upload. Multiple matching VIDs remain ambiguous. Account revocation keeps history and prevents readiness; it does not authorize a new-account resend of an unknown result.

`request_source_retry(session, context, material_id)` provides a transactional, explicit retry entrypoint for an unsent `failed` operation (or an initial no-account block). It rechecks current upload authority, records a new actual account/connection attempt and returns the committed-outbox candidate ID; its caller commits. Unknown, verifying, sending and successful operations are rejected with `material_retry_not_allowed`. Task 2’s object retry endpoint must be explicitly wired to this platform retry path before its public queue offers platform retry.

`run_source_upload` intentionally consumes an engine rather than a caller-owned Session: transaction ownership must stay within the worker so network calls cannot accidentally retain caller locks. Task 2 enqueues only `{material_id}`; subsequent internal records include scoped operation/revision or recovery claim identifiers. `reserve_asset_operation` currently lives alongside this worker; Task 4 integration must reuse the same operation contract and extend the reservation action to `build` instead of inventing a second send claim.

## Offline validation

Behavioral tests use real local PostgreSQL and Redis, the pinned official SDK serialization/deserialization, `urllib3.PoolManager.request` as the TikTok transport double, and botocore `Stubber` for S3. They do not certify live advertiser permissions, actual video acceptance, cross-account sharing, or current tenant asset availability. Live validation needs a separately authorized concrete short video and source account.
