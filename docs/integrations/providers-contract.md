# Provider protocol contract

Reviewed 2026-09-09. These adapters are an offline implementation of inspected protocol code. No login, authenticated business request, channel creation, or real link write was performed during development. Synthetic fixtures are explicitly labelled and contain no copied account/session data. TikTok itself continues to use the official SDK; httpx here is solely for copyright-provider endpoints.

## Evidence

Only protocol functions and explanatory documents were read in the original repository:

- `TikTok AD/jiashu-drama-link-tool/jiashu-link-cli.js`: `post`, `loginAccount`, `discoverAccount`, `searchDrama`, `listChannels`, `createChannel`, `generateGuideUrl`, `saveGuideUrl`, `getGuideUrl`; accompanying README.
- `TikTok AD/wangyan-drama-link-tool/drama-link-cli.js`: `login`, `request`, `getDramaList`, `createPromoteLink`, `getPromoteLinksRaw`; `短剧分销平台 API 分析.md` and targeted protocol sections of `短剧分销平台 - 推广链接生成CLI工具.md`. No Wangyan README.md exists at that path. No credential/account store, session file, or output report was read or imported.
- Public static JavaScript fetched without cookies/authentication, without running it, from `https://partners.shortswave.com/assets/20260724/business-A-IRbkW0.js`. HTTP 200, 511,608 bytes; SHA256 `7a9b1de1f4f10cde11e58839270c89d8c40435454c681d1fda07685a7c90344f`. This proves that version's frontend protocol/rendering, not current authenticated business-service behavior.

Offsets below are zero-based decoded Unicode character offsets in that exact bundle:

| Offset | Source fact |
| --- | --- |
| 24620 | `Fi` requests GET `/api/account/group/apps`, empty explicit payload. |
| 28258 | `fetchAppList` reads the returned array; indexes each application's `package_name`. |
| 31398 / 35187 | App selection uses package_name, name, and is_tt; applications are not globally identical or implicitly single-app. |
| 93764 | Drama list uses page/page_size; its wider frontend protocol also reads a top-level response total. |
| 122250 | `LinkAdvertModal` constructs the b/s/c prefix from drama_int_id/id/chapter_index; Minis mode retains `tt_minis_link` and appends the looked-up drama title to the campaign name. |
| 128015 | Promotion-link list uses page/page_size, optional start/end from time_range, and top-level total. This does not prove omitted dates provide complete history or an exact-ID endpoint. |

The old document's claim that a bundle had been verified was not used as fresh proof: the public bytes were retrieved and the relevant functions independently inspected. No business response has been marked live-verified.

## Session and request boundary

Each persisted connection holds a tenant-encrypted credential envelope. Saving credentials takes current manage permission, accepts only username/password (Jiashu) or email/password (Wangyan), and invalidates the old session generation. Clients use per-request session/Cookie headers. The service owns a new httpx.Client per verification or execution unit, closes it after use, disables environment proxy inheritance and redirects, and never installs global authentication headers. Login/query data and credentials are never logged or copied into provider error text.

`ProviderSession(connection_id, application_id, http, client)` uses the external provider application ID string. The internal ProviderApplication UUID is never sent as that ID. `open_provider_session(*, database_engine, context, connection_id, application_id, action="read", transport=None)` rechecks live tenant/actor authority and current connection/application generation before creating its HTTP client; callers reopen for each durable task unit. `action="provider_write"` is required by link preparation/execution. It holds no DB session over the yielded HTTP work.

`save_connection(session, *, context, kind, display_name, credentials, connection_id=None)` participates in the caller transaction. `verify_connection(*, database_engine, context, connection_id, transport=None)` owns its transactions: save claim/version under a row lock, commit, login/discover, then atomically promote if claim/version/status/authority still match. A fresh verifying claim blocks duplicate verification; an explicit verification after five minutes may replace an abandoned claim, and the old response cannot promote. A disabled connection is never reactivated. Successful verification retains a generation token on the connection and on each found application's channel_config. Historical unseen applications remain for referential integrity but cannot open a session. Failed verification retains the encrypted credentials, records only a stable code, and exposes no remote diagnostic body.

`10001` marks only the current connection reauth_required; explicit verification logs in only that connection. It never switches accounts. The adapter does not automatically replay the failed request, especially a write. `10005` is an application permission failure; it does not try another application or tenant's credentials. Orchestration can show blocked_auth for these stable errors.

## Jiashu

Host: `https://video-wechat-open.eastdrama.net`. All methods POST JSON. Business query parameters are channel=external application ID, channel_from=7, channel_type=1, site_type=oversea_video_iaa. Session goes in the `session` request header. Origin/Referer identify `https://m.eastdrama.net`. The CLI's cache-busting `time` query is not a business input and is omitted. Login instead uses site_type=video. App discovery uses empty channel/channel_from/channel_type with oversea_video_iaa.

| Method path | JSON input | Checked response data |
| --- | --- | --- |
| `/User/login` | username, password | nonempty session, retained only in encrypted/private connection context |
| `/Oversea/App/getAppSwitchList` | type=1 | complete array of appid/name; discover every app, never first-app fallback |
| `/Oversea/AppChannelConfig/getOptions` | customer_id="" | channel_prefix for that specific application; absent/underscore fails closed |
| `/Oversea/Video/getVideoList` | keywords, page, page_size=20 | data array, count integer; video_id/name/language |
| `/Oversea/AppChannelConfig/getChannelList` | page, page_size=20, channel, customer_id="", remark="" | data array, count integer; filter exact channel locally |
| `/Oversea/AppChannelConfig/create` | channel, remark, customer_id="" | true or positive integer acknowledgment |
| `/Oversea/AppChannelConfig/generateGuideUrl` | channel, vid string, drama_num positive integer | nonempty url and minis_path |
| `/Oversea/AppChannelConfig/saveGuideUrl` | channel, vid string, drama_num, jump_url, minis_path | true or positive integer acknowledgment |
| `/Oversea/AppChannelConfig/getGuideUrl` | channel | config object, including jump_url/vid/drama_num/minis_path when present |

Success is string code `0000`; 10001 means expired session, 10005 means forbidden application, other business codes map to provider_rejected. Missing/malformed pagination evidence fails provider_schema_unsupported. The complete list is proved only when current page and integer count agree; count beyond this page requires a full page and continuation. Empty config/URL proves only that this existing channel lacks a saved URL, not that channel creation/generation never happened.

`search(title,page)` returns `{items:[{external_drama_id,title,language}],next_cursor,complete}`. `channel_for(drama_id)` uses the discovered current app prefix; no historic account prefix fallback. `find_existing(drama_id,config,cursor)` returns the same page wrapper with exact matches `{remote_id:channel,channel,remark}`. Only `episode` (default 1) is a supported requested promotion setting; unknown effective settings fail config_unverifiable rather than being silently discarded.

`read_link(channel)` returns `{remote_id,url,config,protected_base,attribution}`. It preserves the original URL and minis_path, permits only named business config fields, checks the HTTPS TikTok URL's channel/vid/dramaNum/charge_level, and detects duplicate, missing, or conflicting identity parameters. Jiashu's evidenced attribution is in those URL parameters; no separate campaign-name prefix is prescribed, so protected_base is the explicit empty string. Unknown configuration fields are not construed as verified settings.

Writes are deliberately separate calls:

- `create_step("create", {channel,vid,remark})` creates only the channel; returns `{accepted:true}`.
- `create_step("generate", {channel,vid,drama_num,existing_config:{}})` generates; returns `{url,minis_path}`.
- `create_step("save", {channel,vid,drama_num,jump_url,minis_path,existing_config:{}})` saves exactly those values; returns `{accepted:true}`.

The channel must equal channel_for(vid). Generate/save require the orchestration's preceding persisted config read and refuse an existing nonempty jump_url; the task must compare config and reuse instead of overwriting. Task 3 serializes the whole connection+application+channel scope across create/generate/save/read. A read of empty saved config cannot establish that a timed-out generate did not happen: such generation stays result_unknown, with no automatic replay. There is no evidenced independent generation-result lookup.

## Wangyan

Host: `https://partners.shortswave.com`. POST `/api/login/pwd_login` sends email/password JSON; success code is integer 0 and the token is from Set-Cookie `x-ds-admin-token`, not a guessed JSON field. Subsequent requests carry that exact cookie on the instance's request. GET `/api/login/userinfo` is documented by the old CLI but is not used for application inference.

Public frontend source confirms GET `/api/account/group/apps` with no explicit app selection returns an array with package_name/name/is_tt. Discovery retains all applications, does not guess TikTok Minis IDs, and fails if required shape/identity is missing. An application package name is not a Minis ID.

GET `/api/distribute_admin/drama/list` sends explicit app, title, page, page_size=20. It normalizes only id/title/lang; id is the opaque external drama identity, int_id is a different numeric attribution/search identity. Search continues past a short nonempty page until an empty page; malformed/non-array data is rejected. This search completeness does not authorize link creation.

The old raw link method is GET `/api/distribute_admin/promote/link/list` with app/page/page_size/start/end and defaults to only the preceding 30 days. Public source confirms pagination/total and a date filter, but neither date omission nor a clear-all UI proves backend full-history coverage. No exact remote-ID lookup contract is established. Therefore `find_existing` and `read_link` currently raise lookup_incomplete, and `create_step` also raises lookup_incomplete before any HTTP request. A frontend boolean cannot bypass this gate. The known future create transport is POST `/api/distribute_admin/promote/link/create` with app/drama_id/chapter_index/promote_platform; it is not executed by this revision.

`render_attribution(row,title)` preferentially retains a nonempty remote campaign_name. Otherwise the retrieved public source establishes `{b<drama_int_id>/s<id>/c<chapter_index>}-<title>` for the inspected Minis renderer. Missing/invalid identities or title raise attribution_contract_unverified; no generic fallback prefix is manufactured. The title must come from the identified drama lookup; merely calling this normalizer does not prove application permission or complete link history and cannot enable creation.

## Failure and verification boundary

Transport failures, server errors, malformed responses, and malformed acknowledgments after a write map to provider_result_unknown with retryable=false. Corresponding reads use provider_unavailable with retryable=true. Known authentication/permission/rejection codes remain explicit. No adapter retries a network call, follows redirects, copies raw response text into exceptions, or chooses another connection.

Before production acceptance, obtain authorized business response examples for both providers, validate listing count/types/end semantics, confirm Wangyan complete-history/exact lookup, confirm actual app-to-Minis mapping, and validate a specifically approved create/read-back flow. The offline suite and public static source inspection do not satisfy that business-service acceptance boundary.
