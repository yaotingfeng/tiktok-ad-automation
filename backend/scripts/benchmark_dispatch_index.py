"""Measure the expansion-slot query in a newly owned, disposable *_test database."""

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core.config import settings
from scripts.benchmark_batches import Parameters, Recorder, owned_database

QUERY = """EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT * FROM pending_dispatch
WHERE tenant_id=:tenant AND published_at IS NULL AND available_at<=now()
AND task_name='builds.expand_submission'
ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"""


def measure(rows: int) -> dict[str, Any]:
    if type(rows) is not int or not 1 <= rows <= 1_000_000:
        raise ValueError("Probe rows must be between 1 and 1000000")
    source = Recorder(Parameters()).report
    result: dict[str, Any] = {
        "schema": "p07-capacity-outbox-index-v1",
        "query_source": "3655936",
        "source_revision": source["source_revision"],
        "source_dirty": source["source_dirty"],
        "environment": source["environment"],
        "kind": "SQL microbenchmark, synthetic dispatch references; no workers or external services",
        "ordinary_rows": rows,
        "expansion_rows": 1,
        "complete": False,
    }
    with owned_database(str(settings.DATABASE_URL)) as engine:
        tenant, actor = uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO pending_dispatch(id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
                SELECT gen_random_uuid(),:tenant,:actor,'builds.process_unit','synthetic-unit-'||n,'{}'::jsonb,
                now()-interval '1 hour'+n*interval '1 microsecond',NULL,0 FROM generate_series(1,:rows) n"""),
                {"tenant": tenant, "actor": actor, "rows": rows},
            )
            connection.execute(
                text("""INSERT INTO pending_dispatch(id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
                VALUES(gen_random_uuid(),:tenant,:actor,'builds.expand_submission','synthetic-expander','{}'::jsonb,
                now()-interval '1 second',NULL,0)"""),
                {"tenant": tenant, "actor": actor},
            )
            connection.execute(text("ANALYZE pending_dispatch"))
        for stage in ("before", "after"):
            with engine.begin() as connection:
                if stage == "after":
                    connection.execute(
                        text("""CREATE INDEX ix_dispatch_expansion_pending_probe
                    ON pending_dispatch(tenant_id,available_at,id)
                    WHERE published_at IS NULL AND task_name='builds.expand_submission'""")
                    )
                plan = connection.execute(QUERY_TEXT, {"tenant": tenant}).scalar_one()[
                    0
                ]
                assert plan["Plan"]["Actual Rows"] == 1
                encoded = json.dumps(plan).replace(str(tenant), "<synthetic-tenant>")
                result[stage] = {
                    "execution_ms": plan["Execution Time"],
                    "shared_hit_blocks": plan["Plan"]["Shared Hit Blocks"],
                    "plan": json.loads(encoded),
                }
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
            connection.exec_driver_sql("""PREPARE priority_probe(uuid,text) AS SELECT * FROM pending_dispatch
            WHERE tenant_id=$1 AND published_at IS NULL AND available_at<=now()
            AND task_name=$2 ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""")
            # Both literals are controlled synthetic values, never user input.
            plan = connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) EXECUTE priority_probe('{tenant}','builds.expand_submission')"
            ).scalar_one()[0]
            assert plan["Plan"]["Actual Rows"] == 1
            result["after_generic_bound_task"] = {
                "execution_ms": plan["Execution Time"],
                "shared_hit_blocks": plan["Plan"]["Shared Hit Blocks"],
                "plan": json.loads(
                    json.dumps(plan).replace(str(tenant), "<synthetic-tenant>")
                ),
            }
            connection.exec_driver_sql("DEALLOCATE priority_probe")
            connection.execute(text("SET LOCAL plan_cache_mode = auto"))
            connection.exec_driver_sql("""PREPARE priority_probe_auto(uuid,text) AS SELECT * FROM pending_dispatch
            WHERE tenant_id=$1 AND published_at IS NULL AND available_at<=now()
            AND task_name=$2 ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""")
            execute = (
                f"EXECUTE priority_probe_auto('{tenant}','builds.expand_submission')"
            )
            for _ in range(10):
                assert connection.exec_driver_sql(execute).first() is not None
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + execute
            ).scalar_one()[0]
            counters = connection.exec_driver_sql(
                "SELECT generic_plans,custom_plans FROM pg_prepared_statements WHERE name='priority_probe_auto'"
            ).one()
            result["after_auto_bound_task"] = {
                "execution_ms": plan["Execution Time"],
                "shared_hit_blocks": plan["Plan"]["Shared Hit Blocks"],
                "generic_plans": counters[0],
                "custom_plans": counters[1],
                "plan": json.loads(
                    json.dumps(plan).replace(str(tenant), "<synthetic-tenant>")
                ),
            }
            connection.exec_driver_sql("DEALLOCATE priority_probe_auto")
            connection.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
            connection.exec_driver_sql("""PREPARE priority_probe_literal(uuid) AS SELECT * FROM pending_dispatch
            WHERE tenant_id=$1 AND published_at IS NULL AND available_at<=now()
            AND task_name='builds.expand_submission' ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""")
            plan = connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) EXECUTE priority_probe_literal('{tenant}')"
            ).scalar_one()[0]
            result["after_generic_literal_task"] = {
                "execution_ms": plan["Execution Time"],
                "shared_hit_blocks": plan["Plan"]["Shared Hit Blocks"],
                "plan": json.loads(
                    json.dumps(plan).replace(str(tenant), "<synthetic-tenant>")
                ),
            }
            connection.exec_driver_sql("DEALLOCATE priority_probe_literal")
    result["complete"] = True
    return result


QUERY_TEXT = text(QUERY)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(  # noqa: T201 -- bounded synthetic metrics only.
        json.dumps(
            {key: result[key] for key in ("ordinary_rows", "complete")}
            | {
                key: {
                    field: value
                    for field, value in result[key].items()
                    if field != "plan"
                }
                for key in (
                    "before",
                    "after",
                    "after_generic_bound_task",
                    "after_auto_bound_task",
                    "after_generic_literal_task",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
