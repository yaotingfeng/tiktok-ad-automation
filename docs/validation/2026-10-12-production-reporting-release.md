# 2026-10-12 production reporting release

## Release

- Production release batch: `20261012T152713-9ccf48`.
- Main revision: `dcd06a142e47cc137a39202ee61360d7798ca853`.
- Image digest: `sha256:99c2fd94101d8e8123a4a5ce7362b3a9d11a727e70dabb89b1c8d2b849a80491`.
- Database head: `0024_drop_legacy_reporting`.
- The production and staging policy audit found no `reports.task_create`, `advertiser_calls_per_window`, or `advertiser_window_ms` entries before release; no deletion was needed.

## Runtime acceptance

- All 9 application Pods became Ready; 7 workers and 1 Beat were present.
- `ADS_SYNC_ENABLED`, `MATERIAL_INGEST_ENABLED`, and `MATERIAL_CLEANUP_ENABLED` are `true`.
- HTTPS health returned 200 and real application login succeeded.
- The active official API BC is `7683817908149272592`; its connection-level completed discovery records (`bc_id IS NULL`) now create an enabled sync plan for 120 accounts.
- The reporting scheduler created jobs after rollout. `ad_day` and `campaign_day_utc` each completed 10 jobs with no failures. Their current queried windows returned zero rows, which is a valid empty provider result, not fabricated coverage.

## Remaining limitation

The provider rejected the current `reports.material_breakdown` requests for the sampled production accounts with business code `40002` (`report_query_invalid`). Ten `material_day` jobs therefore remain failed and no material facts were published. This is recorded as a partial acceptance rather than claiming full-dimension synchronization. The production API still serves account, campaign, ad-group, ad, and drama report endpoints with HTTP 200; material coverage must be revalidated after the provider request contract is corrected.

