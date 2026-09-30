# B2 Fix Report — Round 3

## 修复范围

- `_observation_membership()` now requires a completed, published `AdDirectoryRun`
  matching report tenant, advertiser, BC, connection and channel, with campaign
  directory rows and grouping projections at or before the frozen directory
  completion/version. Empty or missing directory scope, incomplete coverage, or
  cross-route rows cannot append a usable observation. The digest is the stable
  sorted campaign/provider/drama/status/grouping membership set, still appended
  in the same successful publish transaction with replay idempotence.
- Trend buckets floor in the observation timezone and return explicit timezone
  aware local day/hour instants. Local date comparison is used for delta reasons,
  so positive-offset hourly buckets crossing UTC midnight remain comparable on
  the same local date. Mixed timezone and granularity series remain separate.
- Drama trend directory/configuration predicates are applied per member before
  grouping, while IDs, keyword text, spend, and D0 ROAS predicates are applied
  to the projected aggregate series. Group IDs and member refs therefore match
  dimension-row semantics (including `min_spend`/D0 thresholds after summation).

## 验证

Using the dedicated PostgreSQL/Redis `test.env` environment, without TikTok/MCP,
advertising writes, or deployment:

```text
uv run --frozen pytest tests/modules/reporting tests/integrations/tiktok/test_reporting_adapters.py -q  # 79 passed
uv run --frozen ruff check app/modules/reporting app/integrations/tiktok/adapters/sdk_reporting.py tests/modules/reporting tests/integrations/tiktok/test_reporting_adapters.py
git diff --check
```

Added regressions cover empty/frozen directory proof, membership digest changes,
local Shanghai day/hour periods across UTC midnight, and drama aggregate spend
filtering after member aggregation.
