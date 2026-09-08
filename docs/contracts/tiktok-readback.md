# P06 bounded official SDK result reconciliation

This is local implementation and offline transport evidence, not a live TikTok
integration result. No real account, upload, create, status update, or delete was
performed. Public documentation was downloaded without an account/session on
2026-09-09. The dependency remains pinned to official SDK commit
`f809c396520df2d7b201a9ccc5378d822b728ed3`, `python_sdk/business_api_client`.

## Official protocol evidence

| Purpose | Pinned official method | Official documentation |
| --- | --- | --- |
| Campaign | `CampaignCreationApi.smart_plus_campaign_get` | [Upgraded Smart+ campaign get](https://business-api.tiktok.com/portal/docs?id=1843312818332930) |
| Ad group | `AdgroupApi.smart_plus_adgroup_get` | [Upgraded Smart+ ad group get](https://business-api.tiktok.com/portal/docs?id=1843314879617026) |
| Ad | `AdApi.smart_plus_ad_get` | [Upgraded Smart+ ad get](https://business-api.tiktok.com/portal/docs?id=1843317378982914) |
| Missing ad group operation status | `AdgroupApi.adgroup_get` | [Standard ad group get](https://business-api.tiktok.com/portal/docs?id=1739314558673922) |
| Known CTA portfolio | `CreativeManagementApi.creative_portfolio_get` | [Creative portfolio get](https://business-api.tiktok.com/portal/docs?id=1739092113671170) |

The installed generated SDK sources and these complete official field tables
were inspected. Tests exercise the actual generated methods, asynchronous SDK
future, serialization, response model, and owned-client cleanup, with only the
HTTP transport replaced. No handwritten HTTP business gateway is used.

Campaign and ad group name filters are fuzzy; local exact comparison is required.
Ad get has no `ad_name` filter. It is queried with actual `smart_plus_ad_ids` when
known, otherwise with the frozen parent `adgroup_ids`, and locally filtered by
exact name. Ad group requests always include the frozen actual `campaign_ids`.
All list calls request 100 rows; `page`, `page_size`, `total_number`, and
`total_page` must be consistent with the returned rows. A zero-result page is
inconclusive, never evidence that a prior write did not happen.

CTA get requires the actual `creative_portfolio_id`; no supported unknown-ID
list is invented. Its response is not documented to repeat `advertiser_id`:
account binding comes from the currently authorized advertiser-scoped GET, and
the returned portfolio ID, type, text, and associated asset-ID lists must agree.

## Execution and scheduling contract

```python
process_reconciliation(
    *, database_engine, redis_client, context, step_id: UUID, revision: int
) -> ReconciliationResult(state, needs_more=False, retry_after_seconds=0)
```

This function owns short database transactions and makes at most one actual GET.
It does not enqueue anything. Root registers the task, controls durable outbox
and Beat repair, and routes explicit user reconciliation commands. Root must
preserve both `ExecutionStep.resolved.reconciliation` (bounded scan progress) and
`resolved.reconciliation_delivery` (saved result of the current dispatch).

| Result | Scheduling |
| --- | --- |
| `PENDING`, `needs_more=True` | Delay 0 means next page or separate standard ad group status GET; a positive delay preserves a future step due time. Enqueue next revision no earlier than that delay. |
| `BUSY`, `needs_more=True`, delay 60 | A live owner exists; safely reschedule after the lease, without disturbing that owner. |
| `UNKNOWN`, `needs_more=True` | Admission wait uses its actual rounded-up delay; transport uncertainty uses 30 seconds. Root applies its durable retry/backoff policy. |
| `UNKNOWN`, `needs_more=False` | Empty/ambiguous/incomplete/inconsistent result, unknown CTA ID, or authorization/configuration block. Preserve uncertainty; explicit later reconciliation may enqueue a new revision. |
| `SUCCEEDED` / `MISMATCH` | Local readback finished; neither result sends a status update. |
| `STALE` / `WAITING` | No GET; stale revision or not yet eligible for this readback entry point. |

Completed deliveries store their result. Replaying the same revision returns
that result without another GET, including after a lost ACK. Another page or
an explicit new scan needs a newly persisted dispatch revision. Unpublished
outbox repair must retain the existing revision/dispatch ID and backoff.

The task must actually run in Celery prefork with a positive hard limit no
greater than 45 seconds. `process_reconciliation` enforces this itself, rejecting
eager, direct, solo, missing-limit, and longer-limit execution. The claim is 60
seconds. Each endpoint's real shared Redis admission lease must exceed 50
seconds (45 seconds plus cleanup margin); standard reads can use 60 seconds.
Socket timeouts are `(5, 30)` and SDK automatic retries remain disabled. Admission
covers the asynchronous future and SDK cleanup. No database transaction or
connection remains open during either. This is not proof of deployed worker
configuration; P07 must exercise real prefork termination/worker-loss recovery.

The submission's original actor is checked against the supplied tenant context.
Tenant build permission, current BC/account build capability, and equality to
the frozen connection are checked on claim and again after admission immediately
before creating the short-lived SDK client. Viewer access is local-only. A
changed connection produces `account_authorization_changed`; the service never
silently substitutes another token or freezes new draft/strategy intent.

## Matching, evidence and fencing

Expected data comes solely from the immutable saved `request_body` and its
SHA-256 digest, including actual parent IDs and target-account material mapping.
Missing or inconsistent body evidence cannot be repaired from the latest draft.
Returned business fields include account/parent/name, campaign budget and CBO,
ad group ROAS and documented scene fields, exact creative video/cover/identity
sets, URL and copy lists, and the ad configuration's actual CTA portfolio ID.
List order is irrelevant; lengths and content remain significant. Decimal
budget/ROAS strings and numbers compare by finite numeric value. Source VIDs
cannot substitute for target VIDs.

Actual IDs from `remote_id` and append-only `CREATED`/`LATE_CREATED` receipts take
priority. More than one distinct receipt ID blocks automatic selection. Without
an ID, every page must be consistent and exactly one exact-name candidate must
match all required business fields before an ID is bound. Each page appends
bounded ID/comparison/request-ID evidence. PostgreSQL checks previous pages for
repeated IDs; Python never retains the entire remote inventory. The mutable
cursor retains only one candidate, a count capped at two, and page totals.

This checks observed pagination consistency, not a provider-guaranteed atomic
snapshot: the public API offers no snapshot token. Missing fields, changed
totals, duplicate IDs, wrong scope, or multiple candidates remain UNKNOWN. A
later explicit scan can reassess them; no elapsed time or empty result permits
create replay.

When Smart+ ad group get omits operation status, another delivery performs the
standard ad group GET with the actual ID and parent scope. That response must
identify the same object and recheck ROAS. Non-ENABLE states are recorded as mismatches without
changing TikTok. Known IDs preserve the successful creation fact even when
parameters differ. Returned secondary/review status and successful check time
are persisted; ENABLE does not imply approval, delivery, or spend.

UNKNOWN create steps and their READBACK steps use the same create-object fence.
READBACK locks the parent create first, then itself, and places the same nonce
on both, leaving the parent's successful status intact so creation of children
is independent of readback. Finalization compares both tokens, attempts,
revisions, and expiries. Late responses append `LATE_READBACK` only, without
changing a newer owner or its progress. No new table or migration is introduced.

## Verification

The dedicated reconciliation tests include real PostgreSQL and Redis, a remote
transport that commits one create then loses its response, second-page Ad
recovery, duplicate delivery, concurrent claim/expired nonce/late response,
authorization revoked after admission, frozen connection change, wrong tenant,
actual Redis denial/lease release, unknown CTA and conflicting receipts,
parameter/status mismatch, strict page consistency, empty-page uncertainty,
credential-safe logging, and all five actual generated GET serialization paths.
Deployment and real provider-account verification remain P07/external work.
