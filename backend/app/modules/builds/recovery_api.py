"""Recovery request receipts are immutable; job progress is read separately."""

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.modules.builds import recovery
from app.modules.builds.recovery_models import RecoveryReceipt, RecoveryRequestInput
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["builds"])


@router.post(
    "/submissions/{submission_id}/retry",
    response_model=RecoveryReceipt,
    status_code=202,
    operation_id="builds-retry_submission",
    description="返回永久原始 QUEUED/0 回执；分段调度进度读取 submission-recoveries。",
)
def retry_submission(
    tenant_id: UUID,
    submission_id: UUID,
    body: RecoveryRequestInput,
    session: SessionDep,
    user: CurrentUser,
) -> RecoveryReceipt:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    receipt = recovery.request_recovery(
        session,
        context=context,
        submission_id=submission_id,
        request_id=body.request_id,
        kind="RETRY",
    )
    session.commit()
    return receipt


@router.post(
    "/submissions/{submission_id}/reconcile",
    response_model=RecoveryReceipt,
    status_code=202,
    operation_id="builds-reconcile_submission",
    description="仅安排只读核实；返回永久原始 QUEUED/0 回执。",
)
def reconcile_submission(
    tenant_id: UUID,
    submission_id: UUID,
    body: RecoveryRequestInput,
    session: SessionDep,
    user: CurrentUser,
) -> RecoveryReceipt:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    receipt = recovery.request_recovery(
        session,
        context=context,
        submission_id=submission_id,
        request_id=body.request_id,
        kind="RECONCILE",
    )
    session.commit()
    return receipt


@router.get(
    "/submission-recovery-requests/{request_id}",
    response_model=RecoveryReceipt,
    operation_id="builds-saved_submission_recovery",
    description="只读原始回执：状态始终 QUEUED、scheduled_count 始终 0；不会产生或重启工作。",
)
def saved_submission_recovery(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> RecoveryReceipt:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return recovery.get_request(session, context=context, request_id=request_id)


@router.get(
    "/submission-recoveries/{recovery_id}",
    response_model=RecoveryReceipt,
    operation_id="builds-get_submission_recovery",
    description="只读当前分段调度进度；COMPLETED 表示扫描调度完成，不表示所有远端核实已成功。",
)
def get_submission_recovery(
    tenant_id: UUID, recovery_id: UUID, session: SessionDep, user: CurrentUser
) -> RecoveryReceipt:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return recovery.get_recovery(session, context=context, recovery_id=recovery_id)
