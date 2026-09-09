# Target-account cover contract

Verified against public official documentation on 2026-09-09; source hashes are in [tiktok-cover-source-hashes.json](tiktok-cover-source-hashes.json). This records an implementation contract, not live advertiser acceptance.

## Video cover and image identity

[`/file/video/ad/info/`](https://business-api.tiktok.com/portal/docs?id=1740050161973250) returns `video_cover_url`, a temporary URL. The adapter verifies the requested target VID, content signature, displayable status and positive dimensions. It retains the URL only in memory for image ingestion. A video VID, MID, source-account image ID and temporary URL are different values.

When that URL is absent, [`/file/video/suggestcover/`](https://business-api.tiktok.com/portal/docs?id=1740051189071873) provides thumbnail candidates. The adapter chooses a valid URL with the video's aspect ratio; the suggestion's `id` is not treated as an image uploaded to the target account. Recent videos may still be processing; delayed reads must use durable scheduling rather than sleeping in a Worker.

## Upload and verification

[`/file/image/ad/upload/`](https://business-api.tiktok.com/portal/docs?id=1739067433456642) requires JSON for `UPLOAD_BY_URL`. The pinned generated `FileApi.ad_image_upload` always selects multipart, so this mode uses the official `ApiClient.call_api` with a fixed endpoint and JSON body: advertiser ID, stable internal file name, upload type and the received cover URL. No separate HTTP transport is introduced. The response is a data object containing `image_id`, unlike video upload's data array. Names are unique within an advertiser and limited to 100 characters; an uncertain operation keeps its original name.

[`/file/image/ad/info/`](https://business-api.tiktok.com/portal/docs?id=1740051721711618) reads the known target image ID. Readiness checks the same ID and internal name, `displayable=true`, valid dimensions/aspect ratio and the image signature. A received ID is retained even if optional upload metadata is missing; it remains pending readback. Temporary image URLs are not persisted or exposed as permanent cover URLs.

For an unknown upload receipt, [`/file/image/ad/search/`](https://business-api.tiktok.com/portal/docs?id=1740052016789506) is paginated at 100 rows. It exposes at most 10,000 images ordered by modification time, so an empty result or the end of that window never proves no upload happened. Stable totals, unique IDs and a unique exact internal-name candidate are required before a known-ID read. An ambiguous or incomplete search remains UNKNOWN; it cannot authorize a replacement POST.

## Permission and execution boundaries

The [official scope hierarchy](https://business-api.tiktok.com/portal/docs?id=1753986142651394) distinguishes image write (601), image read (600), video read (610) and thumbnail generation (612), including their respective parents. A video-upload-only grant is insufficient for image upload. Every actual request separately checks current tenant/account/connection authority and endpoint scope, uses shared App admission, and has a prefork deadline. No network call holds a database transaction open.

The cover job permanently binds tenant, BC, material, actual target asset/VID, connection and original actor. Its write intent is committed before upload. Known IDs have an append-only fallback receipt before SDK cleanup; unknown writes use only readback. Updating the target mapping requires its current VID and connection to still match. The advertising material step succeeds only after its target cover has been verified.
