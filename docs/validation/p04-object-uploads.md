# P04 object upload contract

This implements P04 Task 2. Videos remain tenant/BC scoped files without drama ownership or user-selected advertiser accounts. Source account selection and actual TikTok upload are Task 3.

## HTTP contract

All routes use `/api/tenants/{tenant_id}/materials`. Authentication and current database membership are required; uploads use `upload`, directory/progress use `read`. BC ownership conflicts block writes. A BC may receive original files while it has no usable TikTok upload account; Task 3 records that platform block without discarding the original.

| Method/path | Request | Response |
| --- | --- | --- |
| POST `/upload-batches` | `request_id`, `bc_id`, `files:[{file_name,size,mime_type}]` | 201 `UploadBatchResult` |
| GET `/upload-batches/{batch_id}` | — | Current bounded batch/file progress |
| POST `/{material_id}/upload-parts/{part_number}/sign` | — | `{url,expires_in:900}`, Cache-Control no-store |
| POST `/{material_id}/complete` | `parts:[{part_number,etag}]` | `{task_id}` |
| POST `/{material_id}/retry` | — | Object `UploadFileResult`, only after a definite object failure |
| GET root path | `bc_id`, optional `query,status,created_from,created_to,cursor,limit` | `Page[MaterialPublic]` |
| GET `/{material_id}` | `bc_id` | `MaterialPublic` |
| GET `/{material_id}/assets` | `bc_id,cursor,limit` | `Page[AccountAsset]` |
| GET `/{material_id}/attempts` | `bc_id,cursor,limit` | `Page[UploadAttemptPublic]` |

Batch requests accept 1–200 files. This is a request-size bound, not a library capacity bound. File sizes are strict positive integers up to the application's conservative 5 TiB original-storage cap. Supported declared video MIME types are validated; this does not claim every file satisfies TikTok's separate constraints. A 4 GB object is supported. Default parts are 16 MiB; larger parts keep the count at most 10000. Completion requires exactly every expected part number once, with nonblank ETags, and sorts the manifest before sending. Multipart ETags are never treated as MD5.

Batch creation commits identities only. The first valid part-sign request lazily creates that file's private multipart session; it does not perform hundreds of S3 calls during batch creation. `receiving` therefore does not claim any received bytes. Browser progress is local until completion; `received_bytes` is null before a verified whole object and equals its actual checked size afterwards. The browser must retain the actual local file and part ETags to resume an unfinished transfer.

Public batch file fields: `material_id`, local `upload_id`, `file_name`, `byte_size`, `part_size`, `part_count`, `status`, `received_bytes`, `task_id`, `latest_advertiser_id`, `can_retry`, and safe `error_code`. There is no standalone bucket/key/remote-upload-ID/credential field. The presigned URL necessarily includes the S3 key, multipart identifier, access-key identifier and signature needed by the browser; it is a short-lived capability. It never includes the secret access key and must not be logged or stored as business metadata.

Directory pages default to 50, allow 1–100, and use filename COLLATE C plus UUID seek. Literal casefolded contains preserves `%`, `_`, and backslashes. Signed cursors bind tenant, BC and all filters; the query is represented by its digest so long Unicode searches remain pageable. Asset/attempt details are separately paged rather than expanding every account into directory rows. There is no original preview/download HTTP endpoint in this bounded Task 2 delivery.

## Transactions and recovery

`start_upload_batch(session,...)` is flush-only. Unique tenant/request ID plus the canonical request digest rejects changed payloads and returns the same file identities for a replay. Routes commit before network work.

`initialize_object_upload`, `sign_upload_part` and `complete_object_upload` use separate short database sessions. ObjectUpload has a server-UTC token and five-minute deadline, committed before network I/O. Successful write-back checks the token, deadline and current tenant/BC authority again. No database transaction or row lock spans S3 calls. Wrong or expired token responses cannot promote objects.

Creation uncertainty searches multipart sessions using the unique server key and recovers only one exact match. Missing or ambiguous evidence stays result_unknown; it does not repeat create. Completion commits its exact part manifest before sending. A timeout, lost response, NoSuchUpload, expired claim, or rolled-back database finalization recovers through HEAD of the same key; the completion write is not replayed. HEAD must match byte length and server tenant/material metadata. A wrong size or identity stays blocked and does not enter the platform queue.

Only a definite InvalidPart/InvalidPartOrder/EntityTooSmall response allows the browser to correct parts and submit completion again. Definite configured creation rejections can retry initialization. Unknown results never advertise ordinary retry. Repeated completion of an already accepted object returns its original task, including after Task 3 marks platform upload blocked; it does not requeue or change the original actor. Task 3's explicit source-retry interface is integrated separately.

After server verification, `finish_upload(session,context,material_id,parts)` is a flush-only finalizer called only from the verified phase. It marks MaterialFile stored and writes ObjectUpload.task_id plus the real transactional outbox in one transaction:

```text
task_name = materials.upload_original
task_key = upload-original:{material_id}
payload = {material_id: UUID string}
```

No Celery publish occurs here. Task 3 must register the real task before production completion can succeed; isolated Task 2 tests register only the dispatch mapping and do not supply a fake consumer. Missing task registration correctly prevents accepting the database completion.

Batch status is persisted after object transitions. Task 3 calls `refresh_upload_batch(session, tenant_id=..., batch_id=...)` in its own mutation transaction. Reads join the latest source attempt in SQL and rederive current progress. Platform attempt status and actual advertiser ID are preserved; public error strings are allowlisted. Task 2 `can_retry` remains false for stored/platform states until a real source retry handler is integrated.

## Original access for Task 3/4

```python
with open_original(
    database_engine=engine, context=context, bc_id=bc_id,
    material_id=material_id, action="upload", deadline=deadline,
) as original:
    # OriginalFile(path: str, byte_size: int, sha256: str, md5: str)
    ...
```

The context manager validates live tenant/BC/action and stored state, closes the read transaction, then streams 8 MiB blocks into a 0700 temporary directory and 0600 file. It verifies length, identity metadata, and any known hashes. Default download deadline is 300 seconds; callers may pass their earlier aware UTC deadline. Temporary files and response streams close on cancellation/error; consumer exceptions retain their own identity. The hashes describe downloaded bytes. Callers must separately reauthorize their remote upload after reading.

## Deployment and evidence boundary

Use a private bucket, the configured S3 endpoint/region/access credentials, and narrowly scoped server IAM. The endpoint must support private multipart creation (the request explicitly uses ACL private), signing UploadPart, CompleteMultipartUpload, HeadObject, GetObject and ListMultipartUploads for recovery. Configure browser CORS for the actual frontend origins and PUT, expose ETag, and allow the required upload headers. Do not configure lifecycle deletion for completed originals; cleaning abandoned incomplete multipart sessions is a separate operator policy.

Boto3 uses SigV4, explicit credentials, connect timeout 5 s, read timeout 30 s, and one total attempt. HTTP/botocore wire loggers are suppressed before signing or sending. No S3/TikTok business endpoint was contacted during this implementation. PostgreSQL tests are real; botocore Stubber validates transport arguments, and deterministic S3 doubles exercise uncertainty/locking boundaries. Browser against deployed S3/CORS and live TikTok upload remain separate evidence gates.
