"""运维只读快照。需显式租户/提交；不读取凭据、素材地址或请求正文。"""

import argparse
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

from redis import Redis
from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine
from app.core.diagnostics import error_fields

COVERS = """with target_jobs as (
 select distinct j.id from execution_step e
 join material_distribution d on d.id=e.distribution_id and d.tenant_id=e.tenant_id
 join material_cover_job j on j.tenant_id=d.tenant_id and j.bc_id=d.bc_id
 and j.material_id=d.material_id and j.advertiser_id=d.advertiser_id
 and j.superseded_by_id is null
 where e.submission_id=:submission and e.tenant_id=:tenant
) """

QUERIES = {
    "submission": """select id,bc_id,status,created_at,updated_at,error_code
        from build_submission where tenant_id=:tenant and id=:submission""",
    "steps": """select kind,status,error_code,count(*) n,min(attempt) min_attempt,
        max(attempt) max_attempt,sum(attempt) total_attempts,max(updated_at) last_change
        from execution_step where tenant_id=:tenant and submission_id=:submission
        group by 1,2,3 order by 1,2,3""",
    "covers": COVERS
    + """select j.status,j.error_code,count(*) n,
        max(j.updated_at) last_change,max(j.revision) max_revision
        from material_cover_job j join target_jobs t on t.id=j.id group by 1,2""",
    "cover_batches": COVERS
    + """select b.id,b.status,b.error_code,b.armed_at,b.request_id,
        jsonb_array_length(b.members) members,count(*) unresolved,
        max(j.revision) max_revision from material_cover_job j
        join target_jobs t on t.id=j.id join material_cover_share_batch b on b.id=j.share_batch_id
        where j.status<>'READY' group by b.id order by unresolved desc""",
    "task_dispatch": """select d.task_name,count(*) n,
        extract(epoch from now()-min(d.available_at)) oldest_due_seconds,
        count(*) filter(where d.published_at is null) unpublished,
        max(extract(epoch from d.published_at-d.available_at)) max_publish_delay_seconds
        from execution_step e join pending_dispatch d on d.id=e.dispatch_id and d.tenant_id=e.tenant_id
        where e.tenant_id=:tenant and e.submission_id=:submission group by 1""",
    "tenant_due_outbox": """select task_name,count(*) n,
        extract(epoch from now()-min(available_at)) oldest_due_seconds
        from pending_dispatch where tenant_id=:tenant and published_at is null
        and available_at<=now() group by 1 order by 2 desc""",
    "create_interval": """with intervals as (
        select s.kind,e.step_id,e.attempt,
        min(e.observed_at) filter(where e.conclusion='REQUEST_ARMED') armed,
        min(e.observed_at) filter(where e.conclusion='CREATED') created
        from step_evidence e join execution_step s on s.id=e.step_id and s.tenant_id=e.tenant_id
        where e.tenant_id=:tenant and e.submission_id=:submission
        and e.conclusion in ('REQUEST_ARMED','CREATED') group by 1,2,3)
        select kind,count(*) n,
        percentile_cont(0.5) within group(order by extract(epoch from created-armed)) p50_seconds,
        percentile_cont(0.95) within group(order by extract(epoch from created-armed)) p95_seconds
        from intervals where created>=armed group by 1""",
}


def snapshot(tenant: UUID, submission: UUID) -> dict:
    params = {"tenant": str(tenant), "submission": str(submission)}
    result = {"at": datetime.now(UTC).isoformat(), **params}
    for name, query in QUERIES.items():
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout='5s'"))
                rows = [
                    dict(row)
                    for row in connection.execute(text(query), params).mappings()
                ]
                if name == "submission" and not rows:
                    raise ValueError("submission outside tenant scope")
                result[name] = rows
        except Exception as error:
            result[name] = error_fields(error)
            if name == "submission":
                return result
    try:
        with Redis.from_url(
            settings.REDIS_URL, socket_timeout=3, socket_connect_timeout=3
        ) as redis:
            result["deployment_queue_depths"] = {
                queue: sum(
                    redis.llen(queue + ("\x06\x16" + str(priority) if priority else ""))
                    for priority in (0, 3, 6, 9)
                )
                for queue in ("control", "builds", "resources", "resource-results")
            }
    except Exception as error:
        result["deployment_queue_depths"] = error_fields(error)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--submission", required=True, type=UUID)
    args = parser.parse_args()
    sys.stdout.write(
        json.dumps(snapshot(args.tenant, args.submission), default=str) + "\n"
    )
