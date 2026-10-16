"""定向回读与批量目录共用领取合同，但不共用批量读取的排队位置。"""

from app.core.context import TenantContext
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

TARGETED_TASK_NAME = "ads.targeted_step"
TASK_NAME = "ads.sync_step"
PUBLISH_TASK_NAME = "ads.publish_step"
register_dispatch_task(TASK_NAME, "ads-directory")
register_dispatch_task(PUBLISH_TASK_NAME, "ads-publish")
register_dispatch_task(TARGETED_TASK_NAME, "ad-management")


def is_targeted(run) -> bool:
    return bool(run.query.get("ids") or run.query.get("parent_ids"))


def directory_step_key(run) -> str:
    prefix = TARGETED_TASK_NAME if is_targeted(run) else TASK_NAME
    return f"{prefix}:{run.id}:{run.claim_generation}:page:{run.next_page}"


def enqueue_directory_step(session, run, *, publish_only=False) -> None:
    enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TARGETED_TASK_NAME
        if is_targeted(run)
        else (PUBLISH_TASK_NAME if publish_only else TASK_NAME),
        task_key=directory_step_key(run),
        payload={"run_id": str(run.id), "claim_generation": run.claim_generation},
    )
