# P07 capacity validation

Status: measurement in progress. The runnable harness and small end-to-end regression are available; the full 100k directory and 200×1000 plan are not yet certified by this document.

Run from `backend/` with a private `DATABASE_URL` naming an explicit PostgreSQL `*_test` database and `TEST_REDIS_URL` selecting a separate nonzero Redis database. The base database is only a connection template: each run creates, migrates and drops its own randomly named database with a checked ownership marker. External DNS is disabled; TikTok GETs and provider search use strict transport doubles. No advertising executor or real external API is called.

```sh
TEST_REDIS_URL=redis://127.0.0.1:16379/8 uv run python scripts/benchmark_batches.py \
  --accounts 1000 --dramas 10 --target-accounts 100 \
  --group-size 10 --creative-count 2 --seed 20260908 \
  --output ../docs/acceptance/capacity-run-baseline-1000.json
```

The harness seeds synthetic source inventory and verified provider link history. It then executes real BC capability pagination/publication and official-SDK Scene refresh, provider search plus normal local-link reuse, draft preparation, immutable preview generation and submission expansion. It does not patch Scene/material readiness, permission resolution, the planner, or insert empty READY previews. Task bodies run synchronously with only the prefork runtime guard substituted; these measurements are local database/CPU capacity, not a claim about deployed Celery worker throughput or platform QPS.

Every directory/preview response uses the current 100-item limit. Page consumption checks strict ordered IDs, count and a streaming digest without keeping business rows. Metrics include P95, maximum serialized page bytes, wall time, SQL statement count, process peak RSS and database size. Fixed seed controls synthetic material identities; business-generated IDs and names retain normal service behavior.

The default BC evidence age has been corrected from 14,400 to 86,400 seconds (engineering cache, allowed60..86400). At the existing 5-second outbox cadence, 100k accounts require 2000 read pages and1000 publication pages: 15,000 seconds before extra processing/scheduling margin. The old default expired too early. Explicit deployment overrides remain explicit; configure the Compose fallback to86400. Shared Scene age already defaults to86400.

Remaining measurements: 1000/10000/100000 directories, full 200 dramas×1000 accounts with30 materials/K10/N2, optimization comparisons if justified, T2 fairness/shared-admission and retry measurements, final environment and actual numeric results. Live SDK/public network and deployed multiworker throughput remain separate unexecuted acceptance items.
