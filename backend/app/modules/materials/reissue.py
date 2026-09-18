"""管理员显式接受素材重复风险后的单项接替；不提供广告重建或全量 UNKNOWN 重试。"""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.execution_models import Submission
from app.modules.tenants.permissions import require_tenant


class MaterialReissueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    kind: Literal["VIDEO", "COVER"]
    old_id: UUID
    accepted_duplicate_materials: StrictBool

    @field_validator("accepted_duplicate_materials")
    @classmethod
    def require_acceptance(cls, value: bool) -> bool:
        if not value:
            raise ValueError("必须明确接受重复素材风险")
        return value


class MaterialReissueReceipt(BaseModel):
    authorization_id: UUID
    request_id: UUID
    submission_id: UUID
    bc_id: str
    kind: Literal["VIDEO", "COVER"]
    old_id: UUID
    replacement_id: UUID
    accepted_duplicate_materials: Literal[True] = True


def _receipt(row: Any) -> MaterialReissueReceipt:
    video = row.kind == "VIDEO"
    return MaterialReissueReceipt(
        authorization_id=row.id,
        request_id=row.request_id,
        submission_id=row.submission_id,
        bc_id=row.bc_id,
        kind=row.kind,
        old_id=row.old_distribution_id if video else row.old_cover_job_id,
        replacement_id=(row.new_distribution_id if video else row.new_cover_job_id),
    )


def authorize_material_reissue(
    session: Session,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    submission_id: UUID,
    body: MaterialReissueInput,
) -> MaterialReissueReceipt:
    from .reissue_models import MaterialReissueAuthorization

    require_tenant(session, tenant_id=tenant_id, actor_id=actor_id, action="manage")
    # 授权登记只做本地短事务，锁竞争必须有界退出，不能占住 API 无限等待。
    SASession.execute(session, text("SET LOCAL lock_timeout = '5s'"))
    SASession.execute(session, text("SET LOCAL statement_timeout = '5s'"))
    # 两个并发点击或不同 request key 指向同一旧请求，也只能消费一次授权。
    # 每次只处理一个精确素材，避免授权事务锁住整个批次。
    for key in (
        f"material-reissue-request:{tenant_id}:{body.request_id}",
        f"material-reissue-old:{tenant_id}:{body.kind}:{body.old_id}",
    ):
        locked = SASession.execute(
            session,
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": key},
        ).scalar_one()
        if not locked:
            raise DomainError("material_reissue_busy", "该项素材正在登记补发", True)
    scope = {
        "tenant_id": str(tenant_id),
        "submission_id": str(submission_id),
        "kind": body.kind,
        "old_id": str(body.old_id),
        "accepted_duplicate_materials": True,
    }
    digest = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    requested = session.exec(
        select(MaterialReissueAuthorization).where(
            MaterialReissueAuthorization.tenant_id == tenant_id,
            MaterialReissueAuthorization.request_id == body.request_id,
        )
    ).first()
    if requested:
        if requested.scope_digest != digest:
            raise DomainError("request_id_conflict", "原请求与本次范围不一致")
        return _receipt(requested)
    old_column = (
        MaterialReissueAuthorization.old_distribution_id
        if body.kind == "VIDEO"
        else MaterialReissueAuthorization.old_cover_job_id
    )
    prior = session.exec(
        select(MaterialReissueAuthorization).where(
            MaterialReissueAuthorization.tenant_id == tenant_id,
            old_column == body.old_id,
        )
    ).first()
    if prior:
        if prior.scope_digest != digest:
            raise DomainError("material_reissue_conflict", "原素材已在另一范围获准接替")
        return _receipt(prior)
    submission = session.exec(
        select(Submission).where(
            Submission.tenant_id == tenant_id,
            Submission.id == submission_id,
        )
    ).first()
    if not submission:
        raise DomainError("resource_not_found", "原提交不存在")
    # helper 只操作原冻结依赖。它与不可变授权、outbox 共用一个事务；任一失败全部回滚。
    if body.kind == "VIDEO":
        from .video_reissue import create_video_replacement

        details = create_video_replacement(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            submission_id=submission_id,
            distribution_id=body.old_id,
        )
        targets: dict[str, Any] = {
            "old_distribution_id": body.old_id,
            "new_distribution_id": UUID(details["new_distribution_id"]),
        }
    else:
        from .cover_reissue import create_cover_replacement

        details = create_cover_replacement(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            submission_id=submission_id,
            cover_job_id=body.old_id,
        )
        targets = {
            "old_cover_job_id": body.old_id,
            "new_cover_job_id": UUID(details["new_cover_job_id"]),
        }
    row = MaterialReissueAuthorization(
        tenant_id=tenant_id,
        bc_id=submission.bc_id,
        submission_id=submission_id,
        actor_id=actor_id,
        request_id=body.request_id,
        kind=body.kind,
        accepted_duplicate_materials=True,
        scope_digest=digest,
        details=details,
        old_distribution_id=targets.get("old_distribution_id"),
        new_distribution_id=targets.get("new_distribution_id"),
        old_cover_job_id=targets.get("old_cover_job_id"),
        new_cover_job_id=targets.get("new_cover_job_id"),
    )
    session.add(row)
    session.flush()
    return _receipt(row)
