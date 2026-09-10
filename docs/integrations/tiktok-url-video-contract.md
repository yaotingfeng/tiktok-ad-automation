# Official SDK URL video foundation

This note covers offline SDK contracts only. It does not establish deployment permission, a working source-to-target transfer, or permission to delete R2 originals. No live TikTok/R2 operation was performed.

## Pinned evidence

The installed `python-sdk` revision is `f809c396520df2d7b201a9ccc5378d822b728ed3`, as fixed in `backend/pyproject.toml` and `uv.lock`. The [official generated FileApi documentation at that revision](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/docs/FileApi.md) exposes `ad_video_upload` and account-scoped `ad_video_info`. Inspection of the installed generated `file_api.py`, `api_client.py`, and `rest.py` confirms the multipart path, field names, request timeout forwarding and response handling. `video_url` and `video_signature` are ordinary multipart fields; the `video_file` file slot is independent and omitted for this wrapper.

`test_url_sdk_contract.py` preserves the real generated API method, SDK serialization/deserialization, REST client and urllib3 multipart encoding. Only the final HTTPS connection's `urlopen` is doubled. Tests decode the resulting multipart body and verify exactly these fields: `advertiser_id`, `upload_type=UPLOAD_BY_URL`, `video_url`, `file_name`, `video_signature`, `auto_bind_enabled`, and `auto_fix_enabled`. Both booleans are passed to the SDK as `False`; the installed multipart encoder produces the text `False`. There is no file part and no local original is opened. This proves the client's serialization, not remote server acceptance of every field.

The [official video info reference](https://business-api.tiktok.com/portal/docs?id=1740050161973250) is linked by the pinned SDK, but its response documentation was not available as readable content during this task. The generated response uses generic `data`, not a typed preview model. Consequently the strict source response shape below is an application validation contract exercised with offline transport fixtures; this task has not independently established the current production response or CDN fleet. An unrelated endpoint's preview lifetime is not evidence for this endpoint, so no fixed TikTok URL lifetime is assumed.

## Caller interface

Exports in `app.modules.materials.sdk_assets`:

```python
RemoteCallBudget(deadline: datetime, hard_limit_seconds: int, lease_ms: int)

upload_video_url(
    client, *, advertiser_id: str, video_url: str,
    remote_name: str, md5: str, budget: RemoteCallBudget,
) -> object

read_source_preview(
    client, *, advertiser_id: str, video_id: str, md5: str,
    allowed_hosts: frozenset[str], budget: RemoteCallBudget,
) -> SourcePreview
```

The budget uses the actual timezone-aware worker deadline, its actual enforced process hard limit, and that endpoint's admitted lease. The lease must exceed the hard limit plus five seconds. Connect/read timeouts share the remaining deadline minus five seconds; their maxima are 10/300 seconds for upload and 5/30 seconds for info. Expired/invalid budgets fail before network activity. These socket limits do not bound DNS or a whole process: the caller must still use the existing prefork hard limit, durable claim and per-call Redis admission. There is no new default execution window.

`upload_video_url` accepts only a freshly issued authorized R2 GET URL or a freshly verified source URL. It validates HTTPS structure and rejects userinfo, IP literals, nonstandard ports, fragments, control characters and invalid host syntax. It cannot prove object ownership from a string; the caller must select and authorize its object generation/source. The stable filename is 1–100 characters. The MD5 is a required 32-character hex whole-file verifier result, normalized to lowercase. Format validation cannot prove digest provenance: never pass a browser claim, multipart ETag or arbitrary digest as trusted evidence.

The return is the same official response consumed by existing `parse_upload`; target VID/MID are never copied from the source or invented. Parse and durably save any known receipt inside caller-owned SDK/admission scopes, before cleanup can fail. The fixture demonstrates the returned receipt remains available when later SDK cleanup fails, but durable database recovery remains an integration responsibility.

`read_source_preview` performs a new official info call for the exact source advertiser and known VID on every invocation. The caller must validate current actor, tenant, BC, connection and account read authority before the call, and fence authority again before persisting readiness. It requires a single row with exact VID, matching whole-file `signature`, literal `displayable=True`, positive integer dimensions/size, a positive finite duration, supported video format and usable `preview_url`. An advertiser ID, if present, must also match. MID may be absent; a present invalid MID fails closed. The format set is the application's existing conservative filename set (`mp4`, `mov`, `m4v`, `avi`, `webm`, `mpeg`, `3gp`), not a claim about TikTok's complete limits.

`SourcePreview` retains advertiser ID, VID, optional actual MID, MD5, displayable, width, height, size, duration, format and URL. URL and MD5 are excluded from its repr. The object is an in-memory handoff, not a persistence DTO: never serialize the whole dataclass into the database, queue, exception metadata or logs. Browser preview routes must return only an authorized short-lived response with `Cache-Control: no-store`; this helper does not implement a route or persist a URL.

## Host policy and failure behavior

No repository contract or inspected official primary source supplied a verified deployment CDN host allowlist. `allowed_hosts` is therefore required and has no permissive default. An empty/malformed policy fails before SDK I/O. Entries are exact lowercase DNS hostnames; wildcard, suffix, URL, local single-label and IP entries are rejected. A syntactically valid host is not automatically vetted. The deployment owner must provide independently reviewed actual CDN hosts. A fresh response on any other host stays blocked; subdomains do not match automatically. Redirects are not followed and the app never downloads the preview video.

Use `official_client`/`sdk_client`, whose real urllib3 policy disables retry/redirect and disables SDK/http wire logging. SDK/business/HTTP exceptions preserve the existing `tiktok_response_error` classification with an application-owned message and suppressed exception chain. Other call/deserialization failures become `material_response_unknown`. Process interrupts propagate to preserve existing lease behavior. Neither classification proves that a POST had no effect: after arming/sending, reconcile the original path and known IDs instead of posting again. Pre-call validation uses `material_digest_missing`, `material_request_invalid`, `material_preview_unverified`, `material_deadline`, or `admission_policy_invalid`; malformed media evidence uses `unsupported_material_schema`. The owning application must register/surface newly introduced codes where needed without exposing raw exception details.

## Required integration and release evidence

- Pass trusted verifier MD5, stable attempt name, exact selected advertiser, actual admission policy, process deadline and fresh authorized URL. Commit the send fence first, close transactions before SDK I/O, and retain unknown-result reconciliation.
- Configure reviewed exact CDN hosts (the root integration plans `MATERIAL_REMOTE_MEDIA_HOSTS` with an empty blocking default). Establish current video-info response shape and media-field semantics using separately authorized deployment evidence.
- Prove the returned source preview serves bytes that preserve the expected digest and can be ingested by the target with these exact official SDK fields. The fact that the SDK accepts `video_signature` in URL mode does not prove server verification of that field or byte preservation through a preview transformation. Require target-account exact VID/content/media read-back before declaring the transfer verified.
- Prove current source/target permissions, receipt recovery, ambiguous/missing media behavior and the complete no-original source-to-target path before enabling immediate original cleanup. This helper does not choose sources, implement sharing, change readiness, or authorize real calls.
- Keep URL-upload capacity limits separate from the old local SDK 256 MiB memory guard. This wrapper does not assert any TikTok URL-upload size or lifetime limit and provides no throughput claim.
