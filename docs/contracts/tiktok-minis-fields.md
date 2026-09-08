# Smart+ Minis scene evidence

Implementation contract: `minis-docs-2026-09-09-v3`. SDK pin:
`f809c396520df2d7b201a9ccc5378d822b728ed3`.
The public documentation was read on 2026-09-09 through TikTok's unauthenticated
portal documentation service. `tiktok-minis-source-hashes.json` records SHA-256 of
the complete markdown documents used, including nested request/response tables.
Fixtures are synthetic values shaped from those tables. No fixture is a recording
of a customer account. No live TikTok, provider, or object-storage call was made.

## Public sources and exact wire contracts

| Evidence | Request and facts used |
| --- | --- |
| [Minis GET, 1853450329535490](https://business-api.tiktok.com/portal/docs?id=1853450329535490) | GET `/open_api/v1.3/minis/get/`: `advertiser_id`, `page`, `page_size` (1–50). Response `data.list[].minis_id/minis_status/minis_type/region_codes` plus `page_info.page/page_size/total_page/total_number`. Select only the exact `ProviderApplication.tiktok_minis_id`, with ACTIVE and MINI_SERIES. External provider application ID is never treated as Minis ID. |
| [Current user's BC assets, 1739432717798401](https://business-api.tiktok.com/portal/docs?id=1739432717798401) | Generated `BCApi.bc_asset_get`, `asset_type=ADVERTISER`, **no filtering argument**. The documented default binds `user_id` to the TikTok for Business user who issued the auth code. `data.list[].advertiser_role` is that user's role. ADMIN/OPERATOR permit campaign creation/editing; ANALYST does not. Check exact advertiser ID, complete pagination, and same credential version. This avoids selecting an arbitrary BC administrator or assuming a core user ID equals a BC member ID. |
| [Identity GET, 1740218420781057](https://business-api.tiktok.com/portal/docs?id=1740218420781057) | Generated `IdentityApi.identity_get`; explicit `identity_type=BC_AUTH_TT`, current `identity_authorized_bc_id`, actual `advertiser_id`, page/size. The docs only guarantee correct page info when identity type is specified. Response is `identity_list`; require exact BC/type, AVAILABLE, `can_push_video=true`, `is_gpppa=false`. Preserve only identity IDs/type/BC. Multiple eligible identities block selection; never select the first. |
| [CTA recommendation GET, 1739362202742785](https://business-api.tiktok.com/portal/docs?id=1739362202742785) | GET `/creative/cta/recommend/`, `new_version=true`, objective APP_PROMOTION, promotion MINI_APP, optimization VALUE, placements `[PLACEMENT_TIKTOK]`, actual advertiser. Read all `recommend_assets[].asset_ids`. These are recommendation asset IDs, **not** a portfolio ID. No `app_id` is substituted for Minis. |
| [VBO status, 1770016073586753](https://business-api.tiktok.com/portal/docs?id=1770016073586753) | GET `/tool/vbo_status/`, objective APP_PROMOTION, app promotion MINIS, promotion MINI_APP, placements TikTok, `campaign_automation_type=UPGRADED_SMART_PLUS`, `budget_optimize_on=true`, actual advertiser. Require `vo_min_roas=QUALIFIED`; preserve status facts without inferring a 0/7-day window. No app/pixel/Minis parameter is invented. The generated VBO method lacks the documented automation field, so the fixed official ApiClient GET preserves it. |
| [OAuth scope hierarchy, 1753986142651394](https://business-api.tiktok.com/portal/docs?id=1753986142651394) | Scope receipt is a strict integer array. Parent 2 covers Ads Management; 6 covers Creative Management, 61 Video Management, 611 video creation/update. Build capability requires parent 2 plus the actual current user's ADMIN/OPERATOR role. Upload capability requires 6/61/611 plus that role. Visibility alone never promotes a grant. Narrow ad-management leaves are not assumed to cover Smart+ when that mapping is unverified. |
| [Advertiser roles, 1737174886619138](https://business-api.tiktok.com/portal/docs?id=1737174886619138) | Advertiser-info `ROLE_ADVERTISER`/`ROLE_CHILD_ADVERTISER` describe business account categories, not the token user's write permission. These values never establish a VERIFIED grant. |
| [Portfolio list, 1766324010279938](https://business-api.tiktok.com/portal/docs?id=1766324010279938) | This list excludes CTA portfolios. It cannot prove a selected dynamic CTA. Scene reads return recommendation IDs and `requires_portfolio_creation=true`; a later explicit CTA execution step must create/read its own portfolio and persist `call_to_action_id`. |

All calls use the pinned official ApiClient. Existing generated endpoint methods
are used when they expose the complete required request. Minis and CTA have no
matching method in this SDK pin. The fallback takes only a closed resource enum,
never an arbitrary path, method, URL, or query supplied by a caller.

## Minis scene and constraint semantics

The [non-catalog Mini Dramas guide, 1853377811982657](https://business-api.tiktok.com/portal/docs?id=1853377811982657)
confirms campaign APP_PROMOTION/MINIS, REGULAR_CAMPAIGN, catalog disabled;
ad-group MINI_APP and actual Minis ID, VALUE / ACTIVE_PAY, OCPM,
BID_TYPE_NO_BID / VO_MIN_ROAS. The supported dynamic CTA field is
`call_to_action_id`; do not send `call_to_action_list`. The chosen provider link
must remain ready, verified, and tied to the currently verified provider app.
Scene dictionaries cannot override user budget, ROAS, account, parent IDs, names,
or creation status. Region codes are allowed regions, not a selected region.

| Field | Confirmed constraint and source |
| --- | --- |
| Campaign name | 512 weighted characters, Chinese/Japanese count two; no emoji. [Campaign create, 1843312852800706](https://business-api.tiktok.com/portal/docs?id=1843312852800706). |
| Ad group name / ROAS | Name 512 characters; the table does not specify CJK weighting. ROAS bid 0.01–1000 inclusive. [Ad-group create, 1843314887930946](https://business-api.tiktok.com/portal/docs?id=1843314887930946). |
| Ad name / K / N / text count | Name 512 CJK-weighted characters, no emoji. At most 50 creative entries per ad (K); at most 30 ads per ad group (N). Text list contains at most 5 entries. [Ad create, 1843317390059522](https://business-api.tiktok.com/portal/docs?id=1843317390059522). |
| Daily campaign budget | For this non-PRODUCT_SALES objective, the USD row requires minimum 50 inclusive, maximum 10000000 exclusive, precision 0.01. Other currencies remain explicitly unverified by this bounded implementation. [Currency budget table, 1737585839634433](https://business-api.tiktok.com/portal/docs?id=1737585839634433). |
| Per-text length | **Unverified.** The complete nested `ad_text_list.ad_text` tables in create, [update 1843317411665921](https://business-api.tiktok.com/portal/docs?id=1843317411665921), and [preview 1843317445798914](https://business-api.tiktok.com/portal/docs?id=1843317445798914) only identify the text field. The [creative combinations table 1847839781968897](https://business-api.tiktok.com/portal/docs?id=1847839781968897) confirms counts but not text length. The pinned `SmartPlusAdCreateBodyAdTextList` setter checks non-null only. Ordinary ad/create's 100-character constraint is not imported into this Smart+ scene. |

`scene_constraints.py` is the reviewed, versioned constraint source. Unknown copy
length is `None` there and `0` only in the compatibility DTO; zero is never a
platform quota. `field_limits_unverified` currently blocks every real scene.
A later evidence-backed change updates that module's constants and revision;
neither client input nor preview code can clear the blocker. Constraint revision
participates in `capability_revision`; changing it invalidates frozen comparisons.
The module is not an environment-configurable escape hatch.

## Service and persistence contract

`builds.scene.read_scene_context(session, *, context, bc_id, advertiser_id,
link_id)` reads only local state under `session.no_autoflush`. It performs no SDK
call, credential decryption, grant mutation, flush, material distribution, or ad
creation. It reloads tenant role, account/BC/grant usability, credential version,
provider verification and link version, then requires fresh completed evidence.
The result in `scene_schemas.py` is a frozen dataclass with recursively frozen
fact dictionaries. `to_snapshot()` returns a small JSON document suitable for
frozen previews. Evidence IDs refer to append-only, sanitized page records.

`refresh_scene_context(*, database_engine, redis_client, context, bc_id,
advertiser_id, link_id, resource, previous_evidence_id=None)` returns frozen
`SceneRefreshResult(evidence_id, resource, complete, next_page, reason_codes)`.
Resources: `account_roles`, `identity`, `minis`, `cta`, `vbo`. Each invocation
performs at most one GET. A noncomplete successful page returns its evidence ID;
pass that ID for the next page. No ID starts a new generation. An active claim
returns `scene_refresh_in_progress` without another request. A stale pointer is
rejected; an expired worker claim can be reclaimed because the operation is read
only. The future preparation task must durably schedule/recover these units.
This module does not add a Celery task or invoke a refresh from preview.

A short transaction commits the generation/attempt-token claim before I/O.
Every actual GET obtains actual App + endpoint + tenant + advertiser admission.
The DB snapshot is closed before SDK transport. Production refresh requires
Celery prefork, non-eager/non-direct invocation, and effective hard limit <=45s;
claim is 60s and admission lease must exceed 50s, both checked before claiming.
The hard limit is an operational deadline, not a TikTok quota. Socket timeout is
5/30s; retries remain zero and SDK debug/logging remains disabled. Completion
rechecks authority, credential/link basis, generation, token, expiry and deadline.
Losing any fence prevents evidence or capability publication. Each lease is
released after the SDK call and its owned client have finished.

Pagination is capped at 50 records/page and 1000 pages; total counts must remain
stable and the completed count must match. Repeated IDs are rejected within each
page and across every previously accepted page of the same generation. Each
append-only page stores at most 50 SHA-256 ID digests; a scoped JSONB overlap
query checks history after the attempt fence, without growing the compact state.
Contract v2 invalidates prior evidence that lacked this uniqueness proof.
Only two matching assets are retained
(to distinguish unique from ambiguous), at most 50 CTA asset IDs, and at most 300
region entries. These are local safety bounds, not claimed platform quotas.
No names, emails, tokens, raw scope receipts or raw remote errors enter evidence.
Known safe reason codes are persisted; unexpected exceptions map to
`scene_refresh_failed`. Existing evidence is never rewritten on a new refresh.

Scope receipt is retained only in tenant-bound encrypted credential JSON as a
canonical JSON string, preserving the existing string-valued encryption schema.
It travels privately through the bounded OAuth child pipe and candidate staging;
callback DTOs and dispatch payloads do not expose it. Old credentials without
scope remain ACTIVE and readable, with UNKNOWN capability until reauthorization
provides evidence. Scene refresh never remotely revokes a credential.

## Offline verification

`tests/modules/builds/scene/` exercises PostgreSQL tenant FKs, complete paging,
unknown/malformed facts, exact identity ownership, no first-item selection,
current token role plus separate scope, old-token read continuity, local-read
immutability, current permission reload, credential rotation, claim concurrency,
expired-claim recovery and stale attempt fencing. The transport callback acquires
both connection and scene rows NOWAIT during the SDK request, proving no DB row
lock is held over that request. Redis assertions verify the actual App/advertiser
leases exist during I/O and are released afterwards. OAuth scope/process tests
exercise the official serializer and private receipt path.

## Frozen request compiler and official create calls

`builds.sdk_requests.compile_request(kind, *, fixed, resolved) -> dict` accepts
only campaign/adgroup/ad kinds and JSON-compatible server-owned facts. It copies
its inputs; resolved facts cannot contain advertiser/parent IDs, any layer name,
budget, CBO, ROAS, or operation status. All three layers compile ENABLE; campaign
CBO is true and ad-group budget is rejected. Verified unmodeled fields such as
`minis_id` remain in native dictionaries. No typed SDK model filters them away.
This is not a public arbitrary-JSON API and does not replace preview validation.

`invoke_create(client, *, kind, body) -> RemoteCreated(remote_id, request_id,
operation_status)` calls the generated `CampaignCreationApi`, `AdgroupApi`, or
`AdApi` Smart+ create method. Corresponding response ID keys are `campaign_id`,
`adgroup_id`, and **`smart_plus_ad_id`**. `ad_id` is not accepted as a substitute.
No create route, task, submission, advertising activation, or rollback operation
is registered by this module.

The pinned official `ApiClient.call_api` synchronous convenience branch returns
a dictionary on success and turns nonzero responses into `TiktokSDKError`. That
exception does not retain structured code/request ID attributes; it concatenates
remote text. We use the official generated method's `async_req=True` option and
wait on its returned `.get()`, which preserves `InlineResponse200`. There is no
custom HTTP gateway, private method patch, or parsing of exception text. Socket
timeout is 5/30s, retries are zero, and a containing prefork hard deadline must
bound the entire wait and owned client cleanup. There is deliberately no separate
future timeout that would return while the SDK thread is still sending.

The executor must commit its attempt before entering this thin SDK function,
recheck frozen scene and current permission, and obtain actual App, endpoint,
tenant and advertiser admission. Hold the lease through `.get()` and client
cleanup; process hard limit must be shorter than the lease with a cleanup margin.
The function neither owns a DB session nor infers authorization from a caller's
body. `CREATE_ENDPOINTS` and `ID_KEYS` expose the fixed maps for the executor.

`TikTokResponseError` contains only the application-owned reason, integer
`remote_code`, and sanitized `request_id`. Nonzero structured codes produce
`tiktok_create_rejected`; missing/malformed expected IDs, transport failures,
or malformed responses produce `create_result_unknown`. `remote_code=-1` means
no trustworthy structured response, and zero with a missing ID still means
unknown outcome. Neither a transport error nor a nonzero remote code is by itself
proof of no external effect. Recovery belongs to the executor and readback.

`tests/fakes/tiktok.py::FakeTikTokAPI.call_api` exposes only Smart+ create/get;
unknown operations and status updates fail the test. Its synthetic per-layer
store supports account-scoped pagination/filtering and the official future
shape. Separately, `test_sdk_contract.py` intercepts urllib3 beneath the real
pinned SDK to verify all three JSON POSTs, ENABLE, unmodeled Minis retention,
nonzero structured errors, missing IDs, no retry, secret-free exceptions, and
waiting for transport completion before owned client cleanup.

## Executable target creatives and dynamic CTA

The [dynamic CTA guide](https://business-api.tiktok.com/portal/docs?id=1740307296329730)
and [portfolio create](https://business-api.tiktok.com/portal/docs?id=1739091950439426)
require each recommended `asset_content` to remain bound to its actual `asset_ids`.
Contract v3 retains only those two fields from each recommendation, plus the
flattened IDs used by preview selection. Generic extra response metadata is dropped.
CTA text is public creative content, not an authentication secret. Old v2 evidence
is invalidated by the scene basis revision.

`cta_portfolio` compiles `creative_portfolio_type=CTA` and the bound recommended
content. `invoke_portfolio` uses the pinned generated `CreativeManagementApi`
(the package exports this class), awaits its official future, and expects
`creative_portfolio_id`. Missing IDs and nonzero responses remain unknown outcomes.
The separate CTA step is never counted as an advertising object. The
[portfolio get](https://business-api.tiktok.com/portal/docs?id=1739092113671170)
endpoint can read an already known portfolio ID; the list endpoint cannot recover
an unknown CTA ID by absence.

`ad_assets` uses every target-account video and verified cover in a material
group for each SP, with one frozen ad text and URL. Identity cannot overwrite
video/cover/ad-format fields. Empty or incomplete mappings fail before a request
is armed. The eight new offline asset/wire cases and existing 26 official SDK
contracts pass; together with the 58 scene tests this change passed 92 cases.
