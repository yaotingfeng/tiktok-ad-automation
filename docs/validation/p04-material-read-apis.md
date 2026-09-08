# Material read APIs for the workspace

These APIs support recovering an upload response, reopening upload progress and playing a preserved original on demand. They do not invoke TikTok/provider APIs or enqueue work.

| GET route under `/api/tenants/{tenant_id}/materials` | Query | Response |
| --- | --- | --- |
| `/upload-requests/{request_id}` | Required `bc_id` | Existing `UploadBatchResult`: original batch/file identities with current per-file progress |
| `/upload-batches` | Required `bc_id`, optional `cursor`, `limit` 50 or 100 (default 50) | `Page[UploadBatchSummary]` |
| `/{material_id}/preview` | Required `bc_id` | `SignedPreview {url, expires_in: 300}` with `Cache-Control: no-store` |

`UploadBatchSummary` contains `batch_id`, `bc_id`, `status`, `file_count` and `created_at`. Status is the batch's persisted progress, maintained by existing upload/source services. File count is computed from actual ObjectUpload rows in SQL. Parent batches are ordered by created_at descending, UUID descending, limited before child aggregation, and returned without full file arrays. The signed cursor binds the tenant, BC and directory identity. Changing scope requires starting a new first page. There is no additional status/date filter in this endpoint.

After POST `/upload-batches` loses its response, GET by the **same request_id and BC** recovers the original batch and its current status. A 404 means no matching row was found in that scope at read time; it does not authorize creating a replacement request key. The client should retain the original key and reconcile/retry that intent. Readers, including viewers, may look up the existing batch; current upload/retry permissions remain independently enforced.

Preview is generated only when the user opens playback; it is not a polling URL. The endpoint reloads current tenant/read permission, BC membership and file state, requires stored original bytes and the server-owned canonical tenant/material original key, and permits only the same video MIME whitelist used by upload requests. The local pinned boto3 client signs `get_object` with explicit HTTP GET, a 300-second expiry, `ResponseContentDisposition=inline` and the allowed video content type. It performs no HEAD/download/write and does not change bucket ACLs. Browser video Range requests can use the capability; actual playback depends on browser codec support and storage deployment configuration.

The URL necessarily contains its target object path, signing credential identifier and signature. It contains no secret access key, and there are no separate backend key/upload ID/credential fields in the response. The DTO excludes the URL from repr; SDK wire logs are disabled through the shared storage client factory. The endpoint sets no-store and does not log the signed URL. Previously issued URLs remain usable until their short expiry; authorization is checked before every new signature.

- Missing or foreign-scope file/request: 404 using existing material/batch codes.
- Non-stored original: 409 `upload_not_ready`.
- Noncanonical object identity: 409 `object_identity_unverified`.
- Unsupported video MIME: 422 `invalid_file`.
- Missing storage configuration: existing safe ConfigurationError `object_storage_unconfigured`, with static missing field names only.
- Signing/client configuration failure: sanitized 503 `object_storage_unavailable`; arbitrary SDK exception strings are neither returned nor logged.

## Offline verification

Nine new HTTP regressions use the separate local PostgreSQL `tiktok_material_read_apis_test`. The initial seven cases failed because the routes were missing, then passed after implementation. Coverage includes 205 actual batches with timestamp ties and varying file counts, pages 100/100/5, viewer reads, BC and tenant cursor rejection, exact request recovery, preserved outbox counts, stored/MIME/key guards, and both-tenants membership isolation. Listing explicitly fails if the full per-batch file loader is invoked.

The preview success case runs real boto3/botocore SigV4 signing with explicit fixture credentials and a transport that rejects any network send, then checks the signed GET capability's object scope, expiry, inline disposition and video MIME. An injected signer exception verifies safe 503 handling without credential/URL logging. These tests do not contact real S3/TikTok/provider APIs and do not prove remote object availability or browser codec acceptance.

New and affected upload/progress/retry suites: **58 passed**, one existing Starlette/httpx deprecation warning. Ruff, strict mypy and ty pass for the four changed production modules. Frontend/client generation and UI playback acceptance remain root/UI integration tasks.
