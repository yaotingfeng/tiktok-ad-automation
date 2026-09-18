"""一次请求仅批准一个明确素材，不隐式扫描或补发其它 UNKNOWN。"""

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep

from .reissue import (
    MaterialReissueInput,
    MaterialReissueReceipt,
    authorize_material_reissue,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["materials"])


@router.post(
    "/submissions/{submission_id}/material-reissues",
    response_model=MaterialReissueReceipt,
    status_code=202,
    operation_id="materials-authorize_reissue",
    description="管理员明确接受重复素材风险后，保留旧证据并单次补发指定素材；不重建未知广告。",
)
def authorize_reissue(
    tenant_id: UUID,
    submission_id: UUID,
    body: MaterialReissueInput,
    session: SessionDep,
    user: CurrentUser,
) -> MaterialReissueReceipt:
    receipt = authorize_material_reissue(
        session,
        tenant_id=tenant_id,
        actor_id=user.id,
        submission_id=submission_id,
        body=body,
    )
    session.commit()
    return receipt
