# Shared Smart+ Minis scene preparation

Implemented offline on 2026-09-09. External behavior is verified with the pinned official SDK and transport doubles; no live TikTok, provider, OAuth or S3 operation was performed.

## Public service contract

`app.modules.builds.scene.read_scene_context(session, *, context, bc_id, advertiser_id, link_id) -> SceneContext` is a local, immutable snapshot reader. It works inside a PostgreSQL `READ ONLY` transaction. It never decrypts credentials, calls Redis/SDK, publishes grants, or queues work. A missing, incomplete, expired or semantically unsupported snapshot returns `supported=False` and explicit safe reasons.

`app.modules.builds.scene_jobs.ensure_scene_preparation(session, *, context, bc_id, advertiser_id, link_id) -> ScenePreparation` validates current operator authority and the current tenant link/provider/application. It only reuses or enqueues durable local work; its caller must commit. The result is the immutable dataclass `(job_id: UUID | None, state: "ready" | "queued" | "blocked", reason_code: str | None)`. **Consumers must use `state`, not infer it from `SceneJob.status`.** A complete, fresh asset job can return `queued` while only its expired BC capability proof is renewed. Existing asset evidence is retained in that case.

`process_scene_job(*, database_engine, redis_client, tenant_id, actor_id, payload)` consumes the exact payload `{job_id, revision}`. The Celery task is `builds.refresh_scene`, queue `resources`, hard limit 45 seconds, soft limit 40 seconds. `builds.repair_scenes` belongs on the control queue/Beat (15–60 second cadence); it inspects at most 100 jobs per call and uses a 120 second repair horizon. Root integration owns Celery includes, Beat and deployment environment mapping.

## Evidence and sharing boundaries

One complete BC capability job proves the current token's actual BC role plus the separately stored OAuth App scope. Preparation waits for its complete, atomically paginated publication before strict build-account resolution. Directory visibility and `VERIFIED` flags alone are insufficient. An old token without a scope receipt stays ACTIVE/readable, but preparation finishes with `capability_scope_unknown`; an analyst cannot build. No per-drama BC role request is scheduled.

The shared scene key includes tenant, BC, advertiser, selected TikTok connection and credential version, advertiser grant discovery generation and metadata, TikTok App ID, provider connection/version/successful verification, external provider application, actual Minis ID, SDK/constraint revisions and configured cache age. It excludes drama/link ID, URL and link version because none is an input to these reads. Each ensure/read still validates the current supplied link. Jobs themselves use account/application scope and remain independent of any representative link.

A successful generation reads identity, Minis, CTA recommendations, VBO eligibility and targeting countries. Every task makes at most one actual GET. Identity/Minis use 50-row pages and validate all IDs, including ineligible/unselected rows. Every page retains bounded ID hashes in `SceneJobPage`; a SQL existence query checks all earlier pages in the same tenant/job/resource. A duplicate is an error, never silently deduplicated into a complete list. The compact job facts retain only selection facts and counts. Multiple eligible identities remain blocked; no first-item fallback exists. CTA facts preserve each recommended text and its corresponding asset IDs.

`DraftScenePreparation` persists independent connection and advertiser cursors, with `DraftCapabilityDependency` and `DraftSceneDependency` rows. Draft preparation performs bounded local pages (100), waits for links/materials, then schedules one account/application job per selected account. It does not load the full account × drama product in Python, hold a draft transaction across network, or require a link to bootstrap account permissions. A completed scene that ages while the rest of a large draft waits is recorded as expired rather than triggering an endless whole-draft refresh sweep. Execution can call ensure again in a separate committed transaction when needed.

## Claims, retries and freshness

The worker validates prefork execution and a hard limit at most 45 seconds (including explicit task overrides). SDK socket timeout is `(5, 30)`, retries are disabled by the shared SDK factory, and admission uses the actual configured TikTok App. The admission lease must exceed the hard limit plus cleanup margin. A 60-second nonce claim is committed before SDK work; receipt persistence reloads actor authority, credentials, provider/application and current capability proof, and fences the exact job/revision/nonce. No database session survives across the network call.

Repair keeps the current dispatch ID, revision and payload. Unpublished outbox rows preserve broker backoff; published rows are rearmed using the same identity. Live claims are not redelivered, and malformed dispatch identities become terminal safe blocks. Read failures have a bounded retry budget; malformed/duplicate evidence does not become usable. Restored local authority may create a new generation and bootstrap missing proof again; every asset GET still requires current positive capability proof. An existing negative proof is terminal. The old blocked generation and late receipts stay fenced. Unsupported scope and malformed evidence are not repeatedly retried by ensure.

`SCENE_MAX_AGE_SECONDS` defaults to 86400 and accepts 60..604800. This is an engineering cache policy, not a platform permission lifetime. Its value participates in the scope basis. Expiry is measured from the first successful observation and never extended by later pages or completion. New evidence IDs alone do not change business facts; the executor separately compares current resolved facts with the frozen preview.

## Official targeting and application copy policy

The official [available locations by settings endpoint](https://business-api.tiktok.com/portal/docs?id=1737189539571713) documents `/tool/region/`, `APP_PROMOTION`, `app_promotion_type=MINIS`, `promotion_type=MINI_APP`, TikTok placement, and country-level `region_list`/`region_info`. The pinned SDK's generated `ToolApi.tool_region` omits the two Minis parameters, so the implementation uses that SDK's `ApiClient.call_api` with a fixed GET path and fixed query shape. It does not introduce a generic HTTP gateway. The parser checks complete matching country-code sets, unique location IDs, COUNTRY/ADMIN semantics and a bounded response. It intersects all actual Minis-allowed countries with the advertiser's available countries and produces:

- `adgroup_fields.placement_type = "PLACEMENT_TYPE_NORMAL"`
- `adgroup_fields.placements = ["PLACEMENT_TIKTOK"]`
- `adgroup_fields.targeting_spec.location_ids =` sorted actual returned IDs
- `field_constraints.target_regions = [{region_code, location_id}, ...]` for a clear preview

The [Smart+ ad group create contract](https://business-api.tiktok.com/portal/docs?id=1843314887930946) requires real location IDs for this targeting mode. Country codes and provider application identifiers are never substituted as IDs. An empty intersection is `scene_targeting_unavailable`.

The [Smart+ ad create contract](https://business-api.tiktok.com/portal/docs?id=1843317390059522) defines required text content but the reviewed nested `ad_text` definition does not establish a platform maximum. `platform_copy_length` therefore remains `None`. Separately, this tool's reviewed English copy pool uses application policy 1..100 characters: `copy_policy.source="APPLICATION"`, `minimum=1`, `maximum=100`, `measurement="characters"`; `copy_length=100` and `copy_measurement="characters"` are its effective validation fields. This is not an environment override or a claim about a TikTok quota. The compiler/preview validates nonempty content and this application maximum. An undocumented platform maximum alone no longer causes `field_limits_unverified`.

Constraint revision: `minis-constraints-2026-09-09-v2`. SDK scene contract: `minis-docs-2026-09-09-v4`. Official SDK pin: `f809c396520df2d7b201a9ccc5378d822b728ed3`. Existing standalone `refresh_scene_context` remains a diagnostic compatibility service; its per-link role evidence cannot substitute for the formal BC capability proof or shared complete scene.

Public English Markdown was retrieved from the official portal's `/gateway/api/doc/client/node/get/v2/` response (`data.content_data`), cached only in `/tmp/p06-doc-<ID>.md`. SHA-256 evidence:

| Document | SHA-256 |
| --- | --- |
| 1737189539571713 | `632b3d856a1a7ff9fac3581ee8028702deb2dac41aeffda78d99cac3bc816a45` |
| 1843314887930946 | `58778497df9159ff2935cd05ddff8819fb2e14aad0bf2f7709e7ca22c4b3509d` |
| 1843317390059522 | `0db3e651a7f701b0165c5de0cbbcf97560dab4e2f10ae6b5bef98696ccc5c2ec` |

Synthetic country/identity/Minis/CTA values in tests are transport fixtures, not discovered production account defaults.
