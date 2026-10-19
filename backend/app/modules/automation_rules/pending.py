"""规则重新命中时，先等待同一实际对象/字段的上一笔操作结算。"""

from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.modules.ad_management.execution import _material, _ref
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ads.models import AdObject
from app.modules.automation_rules.actions import target_index, use_key


def pending_targets(
    session: Session,
    tenant_id: UUID,
    targets: dict[str, dict[str, Any]],
    operation: str,
) -> set[str]:
    if not targets:
        return set()
    # 延迟导入避免执行器与目标映射相互初始化；预算仍按实际预算持有者匹配。
    from app.modules.automation_rules.executor import _OwnerMap

    index = target_index(list(targets.values()))
    owners = _OwnerMap(session, tenant_id, list(targets.values()), index)
    rows = (
        cast(SASession, session)
        .execute(
            sa_select(
                col(ManagementTaskItem.id),
                col(ManagementTaskItem.ref),
                col(ManagementTaskItem.material_use),
                col(ManagementTaskItem.execution_result),
                col(ManagementTask.bc_id),
            )
            .join(
                ManagementTask,
                col(ManagementTask.id) == col(ManagementTaskItem.task_id),
            )
            .where(
                col(ManagementTaskItem.tenant_id) == tenant_id,
                col(ManagementTask.tenant_id) == tenant_id,
                col(ManagementTask.bc_id).in_({t["bc_id"] for t in targets.values()}),
                col(ManagementTaskItem.capability)["operation"].astext == operation,
                col(ManagementTaskItem.ref)["advertiser_id"].astext.in_(
                    {t["advertiser_id"] for t in targets.values()}
                ),
                col(ManagementTaskItem.execution_result).in_(
                    ["PENDING", "UNKNOWN", "NOT_SENT", "ACCEPTED"]
                ),
            )
        )
        .all()
    )
    blocked: set[str] = set()
    for item_id, ref_json, material_json, result, bc_id in rows:
        ref, material = _ref(ref_json), _material(material_json)
        if material is not None:
            # 同一视频在其他广告中的使用不能阻塞这一次使用。
            if (
                "use",
                *use_key(
                    ref.advertiser_id,
                    ref.remote_id,
                    material.platform_material_id,
                    material.ad_material_id,
                    material.material_type,
                ),
            ) not in index:
                continue
        keys: Iterable[str] = owners.keys(ref, material)
        keys = [
            key for key in keys if key in targets and targets[key]["bc_id"] == bc_id
        ]
        if not keys:
            continue
        if result in {"NOT_SENT", "ACCEPTED"}:
            attempt = session.exec(
                select(
                    ManagementRequestAttempt.retryable,
                    ManagementRequestAttempt.request_at,
                )
                .where(
                    ManagementRequestAttempt.tenant_id == tenant_id,
                    ManagementRequestAttempt.task_item_id == item_id,
                )
                .order_by(col(ManagementRequestAttempt.attempt).desc())
                .limit(1)
            ).first()
            if result == "NOT_SENT":
                if attempt is None or not attempt[0]:
                    continue
            else:
                # 平台已受理但目录尚未刷新时，不依据旧状态再次提交。
                observed = session.exec(
                    select(AdObject.observed_at).where(
                        AdObject.tenant_id == tenant_id,
                        AdObject.advertiser_id == ref.advertiser_id,
                        AdObject.kind == ref.kind,
                        AdObject.remote_id == ref.remote_id,
                    )
                ).first()
                if (
                    attempt is not None
                    and observed is not None
                    and observed > attempt[1]
                ):
                    continue
        blocked.update(keys)
    return blocked
