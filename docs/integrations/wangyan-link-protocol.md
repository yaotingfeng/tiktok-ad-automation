# Wangyan link protocol supplement

Reviewed 2026-09-09 against public static source and local CLI protocol functions. This supersedes the earlier conclusion in providers-contract.md that exact-ID lookup was unavailable. No authentication, business-service request or provider write was performed. Fixtures are synthetic, not captured business receipts.

Public source: `https://partners.shortswave.com/assets/20260724/business-A-IRbkW0.js`, 511,608 bytes, SHA256 `7a9b1de1f4f10cde11e58839270c89d8c40435454c681d1fda07685a7c90344f`. Offsets are decoded Unicode character offsets. Local CLI source: `TikTok AD/wangyan-drama-link-tool/drama-link-cli.js`, only createPromoteLink/getPromoteLinks/getPromoteLinksRaw and related protocol functions were read; no account/session/output data was used.

| Offset | Observed source fact |
| --- | --- |
| 7819 | `_e` forwards GET payload into params, unwraps `data=x.data.data`, and retains `rawRes=x`. |
| 24901 | `Qi` GETs `/api/distribute_admin/promote/link/list`; `Hi` POSTs `/api/distribute_admin/promote/link/create`. |
| 126800 / 129108 | Link form contains id, labelled 推广链ID, plus app and drama_id. |
| 127900–128067 | Loader forwards the cloned form with page/page_size/start/end to Qi and reads rawRes.data.total. Therefore total is a response-body field alongside the data array. |
| 112900–115300 | AddLink form retains promote_name in the Minis create payload; only unrelated landing/IAP/postback fields are removed. The name is required in the UI and editable. |
| 122250 | Minis renderer uses remote tt_minis_link and `{b<drama_int_id>/s<id>/c<chapter_index>}-<looked-up title>`. |

`find_existing(drama_id, config, cursor)` accepts episode (default 1). It sends explicit app, opaque drama_id, page, page_size=20, start=1970-01-01 and end=the UTC day after the first page. This end date stays fixed across later pages and midnight. The former preceding-30-days range was a CLI default, not an evidenced provider restriction.

The response must have an integer code=0, data array, integer total, the exact number of rows expected for that page and unique positive link IDs within the page. Total must remain unchanged across pages. Only rows with matching app/opaque drama_id/TikTok platform/chapter_index become candidates. Missing comparable fields raise config_unverifiable rather than being silently filtered into an empty result.

Each page returns items, next_cursor, complete, total and observed_ids. The last field includes every row, including filtered-out rows. The durable workflow must reject cross-page duplicates before accepting complete history. A cursor contains version, app/drama/episode scope digest, fixed dates, next page and total; it never accumulates all historical IDs. The 10,000-page / 200,000-row ceiling is an engineering bound. Exceeding it returns lookup_incomplete, never a complete partial history.

`read_link(id)` uses the same list endpoint with exact positive numeric id, current app and explicit history dates. It requires total=1 and the returned ID to equal the requested ID, then checks app/platform and configuration. Zero or multiple results produce lookup_incomplete. An absent read is not proof that an unknown POST had no effect.

Normalized candidates and readbacks contain remote_id, remote tt_minis_link as url, config `{vid: opaque drama_id, drama_num: chapter_index, jump_url}`, optional promote_name, optional protected_base from remote campaign_name and a narrow attribution projection. URL values are not manufactured and must be HTTPS TikTok URLs when nonempty. Numeric drama_int_id is retained only for attribution, never substituted for opaque drama_id. The workflow uses the formally selected drama title with render_attribution; unrelated remote fields are not retained.

`create_step('create', {vid, drama_num, promote_name?})` performs one POST with explicit app/drama_id/chapter_index/promote_platform=tiktok. Optional promote_name is supported by public source, nonblank and limited to 255 characters by this application. A stable operation name enables positive correlation; the source does not establish a server idempotency-key guarantee. A valid integer code=0 acknowledgement returns accepted=true, retaining optional positive data.id as remote_id. Missing ID requires later read-back. Malformed codes/data/IDs and ambiguous transport failures become non-retryable provider_result_unknown. No implicit retry, second POST, or later-zero-results replay is implemented in the adapter. Durable lookup history, write fencing, receipt persistence, name correlation and ambiguity handling belong to the workflow.

Public source establishes implementable request and response access patterns. It does not prove server-side date-span coverage, absence of hidden truncation, exact-ID semantics under every account, snapshot consistency or unchanged totals during concurrent remote writes. Those require authorized live read-only release acceptance, followed separately by a specifically authorized create/read-back check. The implementation does not conceal these gaps behind a permanent placeholder flag or describe offline fixtures as live verification.
