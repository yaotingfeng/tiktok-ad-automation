"""已获授权的独立补建只读导入。应用自行回读验证，外部声明不能标记成功。"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from redis import Redis
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import (
    AdCreate,
    AdGroupCreate,
    BuildReadQuery,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.modules.accounts.routing import verify_route
from app.modules.builds.correction_models import VerifiedReplacement
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.readback_compare import (
    compare_record,
    safe_remote_id,
    with_adgroup_status,
)
from app.modules.builds.reconciliation import (
    _known_ids,
    original_create_attempt,
    source_intent,
)
from app.modules.builds.routes import verify_unit_route
from app.modules.tenants.permissions import require_tenant


def digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def resolved_sql(alias: str = "s") -> str:
    """原 AD、其 READBACK、已替换组及组 READBACK 都只在业务投影中闭环。"""
    if alias not in {"s", "e", "candidate", "child", "execution_step"}:
        raise ValueError("invalid SQL alias")
    return f"""EXISTS (SELECT 1 FROM build_verified_replacement vr
 WHERE vr.tenant_id={alias}.tenant_id AND vr.submission_id={alias}.submission_id
 AND (vr.source_step_id={alias}.id OR vr.source_group_step_id={alias}.id
 OR ({alias}.kind='READBACK' AND {alias}.parent_step_id IN (vr.source_step_id,vr.source_group_step_id))))"""


def is_resolved(session: Session, step: ExecutionStep) -> bool:
    return bool(
        SASession.execute(
            session,
            text(
                "SELECT "
                + resolved_sql("s")
                + " FROM execution_step s WHERE s.tenant_id=:tenant AND s.id=:step"
            ),
            {"tenant": step.tenant_id, "step": step.id},
        ).scalar_one()
    )


def _invalid(code: str = "replacement_unverified") -> DomainError:
    return DomainError(code, "补建证据或对象范围未通过核实，原结果保持待核实")


def _compatible(
    original: AdCreate | AdGroupCreate,
    replacement: AdCreate | AdGroupCreate,
    *,
    allowed: set[str],
) -> bool:
    left, right = original.model_dump(mode="json"), replacement.model_dump(mode="json")
    for key in allowed:
        left.pop(key, None)
        right.pop(key, None)
    if original.kind == "AD":
        # 封面可替换；视频、多重素材的顺序和身份、文案、链接、CTA 必须保留。
        for data in (left, right):
            for asset in data["assets"]:
                asset.pop("image_id", None)
    return bool(left == right)


def import_verified_replacement(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    request_id: UUID,
    source_step_id: UUID,
    source_request_digest: str,
    replacement_ad_id: str,
    replacement_adgroup_id: str,
    ad_intent: AdCreate,
    adgroup_intent: AdGroupCreate,
    audit_reference: str,
    task_deadline: datetime,
) -> UUID:
    """内部运维入口，无公开成功声明 API，也没有任何广告写调用。

    调用者提供已获授权的冻结补建意图及审计引用；本函数必须在 45 秒内
    从原冻结连接读取新 AD、新组、新组状态与旧组停用状态后才可落账。
    """
    now = datetime.now(UTC)
    if (
        task_deadline.tzinfo is None
        or not 0 < (task_deadline - now).total_seconds() <= 45
    ):
        raise _invalid("replacement_deadline_invalid")
    if not all(
        safe_remote_id(value) for value in (replacement_ad_id, replacement_adgroup_id)
    ):
        raise _invalid()
    if not audit_reference.strip() or len(audit_reference) > 1024:
        raise _invalid()
    # model_copy 也不能绕过枚举和完整类型校验。
    ad_intent = AdCreate.model_validate(ad_intent.model_dump())
    adgroup_intent = AdGroupCreate.model_validate(adgroup_intent.model_dump())
    frozen_intent = {
        "ad": ad_intent.model_dump(mode="json"),
        "adgroup": adgroup_intent.model_dump(mode="json"),
    }
    request_digest = digest(
        {
            "source": str(source_step_id),
            "source_digest": source_request_digest,
            "ad_id": replacement_ad_id,
            "adgroup_id": replacement_adgroup_id,
            "intent": frozen_intent,
            "audit": audit_reference,
            "actor": str(context.actor_id),
        }
    )
    with Session(database_engine) as session, session.begin():
        # 该导入事务需持有原步骤锁跨四次只读请求；不能使用专供短回调的
        # 五秒 bounded_session。SQL/锁等待各有五秒上限，空闲事务与远端
        # 请求共享整体截止时间，所有设置仅对本事务生效。
        SASession.execute(session, text("SET LOCAL statement_timeout = '5s'"))
        SASession.execute(session, text("SET LOCAL lock_timeout = '5s'"))
        SASession.execute(
            session, text("SET LOCAL idle_in_transaction_session_timeout = '50s'")
        )
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="manage",
        )
        # 同租户导入串行化，保证远端 AD/组不能被并发分配到两份原意图。
        SASession.execute(
            session,
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"build-replacement:{context.tenant_id}"},
        )
        saved = session.exec(
            select(VerifiedReplacement).where(
                VerifiedReplacement.tenant_id == context.tenant_id,
                VerifiedReplacement.request_id == request_id,
            )
        ).one_or_none()
        if saved:
            if (
                saved.request_digest != request_digest
                or saved.actor_id != context.actor_id
            ):
                raise _invalid("idempotency_conflict")
            return saved.id
        source = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.id == source_step_id,
            )
            .with_for_update()
        ).one_or_none()
        if source is None:
            raise _invalid("resource_not_found")
        submission = session.exec(
            select(Submission)
            .where(
                Submission.tenant_id == context.tenant_id,
                Submission.id == source.submission_id,
            )
            .with_for_update()
        ).one()
        group = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.id == source.parent_step_id,
            )
            .with_for_update()
        ).one_or_none()
        unit = session.get(BuildUnit, source.unit_id)
        if (
            source.kind != "AD"
            or source.status != "UNKNOWN"
            or _known_ids(session, source)
            or source.dispatch_id
            or source.lease_token
            or not unit
            or not group
            or group.kind != "ADGROUP"
            or group.status != "SUCCEEDED"
            or not group.remote_id
            or group.dispatch_id
            or group.lease_token
            or (group.submission_id, group.unit_id, group.bc_id)
            != (source.submission_id, source.unit_id, source.bc_id)
            or source.request_body_digest != source_request_digest
            or digest(source.request_body) != source_request_digest
            or not group.request_body
            or digest(group.request_body) != group.request_body_digest
        ):
            raise _invalid("replacement_source_changed")
        route = verify_unit_route(
            session, context=context, unit=unit, capability="read"
        )
        old_ad, old_group = (
            source_intent(session, source, route),
            source_intent(session, group, route),
        )
        attempt, attempt_id = original_create_attempt(session, source)
        if (
            not isinstance(old_ad, AdCreate)
            or not isinstance(old_group, AdGroupCreate)
            or old_ad.adgroup_id != group.remote_id
            or replacement_adgroup_id == group.remote_id
            or ad_intent.adgroup_id != replacement_adgroup_id
            or ad_intent.advertiser_id != unit.advertiser_id
            or adgroup_intent.advertiser_id != unit.advertiser_id
            or not _compatible(old_ad, ad_intent, allowed={"name", "adgroup_id"})
            or not _compatible(
                old_group, adgroup_intent, allowed={"name", "schedule_start_time"}
            )
        ):
            raise _invalid("replacement_intent_mismatch")
        existing = session.exec(
            select(VerifiedReplacement).where(
                VerifiedReplacement.tenant_id == context.tenant_id,
                (VerifiedReplacement.source_step_id == source.id)
                | (
                    (VerifiedReplacement.bc_id == source.bc_id)
                    & (VerifiedReplacement.advertiser_id == unit.advertiser_id)
                    & (VerifiedReplacement.remote_ad_id == replacement_ad_id)
                ),
            )
        ).first()
        if existing:
            raise _invalid("replacement_ownership_conflict")
        group_links = session.exec(
            select(VerifiedReplacement).where(
                VerifiedReplacement.tenant_id == context.tenant_id,
                VerifiedReplacement.bc_id == source.bc_id,
                VerifiedReplacement.advertiser_id == unit.advertiser_id,
                (VerifiedReplacement.source_group_step_id == group.id)
                | (VerifiedReplacement.remote_adgroup_id == replacement_adgroup_id),
            )
        ).all()
        if any(
            link.source_group_step_id != group.id
            or link.remote_adgroup_id != replacement_adgroup_id
            or link.replacement_intent["adgroup"] != frozen_intent["adgroup"]
            for link in group_links
        ):
            raise _invalid("replacement_ownership_conflict")
        # 停用旧组不能掩盖同组已知成功广告，也不能借用其他任务已有远端对象。
        occupied = SASession.execute(
            session,
            text("""SELECT 1 FROM execution_step e JOIN build_unit u ON u.id=e.unit_id AND u.tenant_id=e.tenant_id
 WHERE e.tenant_id=:tenant AND e.bc_id=:bc AND u.advertiser_id=:advertiser AND
 ((e.kind='AD' AND e.parent_step_id=:group_id AND e.remote_id IS NOT NULL)
 OR (e.kind='AD' AND e.remote_id=:ad) OR (e.kind='ADGROUP' AND e.remote_id=:new_group)) LIMIT 1"""),
            {
                "tenant": context.tenant_id,
                "bc": source.bc_id,
                "advertiser": unit.advertiser_id,
                "group_id": group.id,
                "ad": replacement_ad_id,
                "new_group": replacement_adgroup_id,
            },
        ).first()
        if occupied:
            raise _invalid("replacement_ownership_conflict")
        verification: dict[str, Any] = {}
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=task_deadline,
        ) as gateway:
            for label, intent, remote_id in (
                ("ad", ad_intent, replacement_ad_id),
                ("adgroup", adgroup_intent, replacement_adgroup_id),
            ):
                query = BuildReadQuery(intent=intent, remote_id=remote_id)
                page = gateway.builds.read_page(query=query)
                if (
                    not page.complete
                    or page.page != 1
                    or len(page.rows) != 1
                    or page.total_number != 1
                ):
                    raise _invalid()
                record = page.rows[0]
                verification[label + "_read"] = asdict(page.evidence)
                if label == "adgroup":
                    status = gateway.builds.read_adgroup_status(
                        advertiser_id=unit.advertiser_id, adgroup_id=remote_id
                    )
                    if (
                        status.operation_status != "ENABLE"
                        or status.adgroup_id != remote_id
                        or status.advertiser_id != unit.advertiser_id
                    ):
                        raise _invalid()
                    if record.intent is None:
                        record = with_adgroup_status(record=record, status=status)
                    verification["adgroup_status_read"] = asdict(status.evidence)
                if (
                    compare_record(query=query, record=record) != "MATCH"
                    or record.operation_status != "ENABLE"
                ):
                    raise _invalid()
                verification[label] = record.model_dump(mode="json")
            old_status = gateway.builds.read_adgroup_status(
                advertiser_id=unit.advertiser_id, adgroup_id=group.remote_id
            )
            if (
                old_status.operation_status != "DISABLE"
                or old_status.adgroup_id != group.remote_id
                or old_status.advertiser_id != unit.advertiser_id
            ):
                raise _invalid()
            verification["original_group_status"] = old_status.model_dump(mode="json")
        if datetime.now(UTC) >= task_deadline:
            raise _invalid("replacement_deadline_invalid")
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="manage",
        )
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=unit.advertiser_id,
            capability="read",
        )
        row = VerifiedReplacement(
            tenant_id=context.tenant_id,
            bc_id=source.bc_id,
            submission_id=source.submission_id,
            preview_id=source.preview_id,
            advertiser_id=unit.advertiser_id,
            request_id=request_id,
            actor_id=context.actor_id,
            source_step_id=source.id,
            source_group_step_id=group.id,
            source_attempt=attempt,
            source_attempt_id=attempt_id,
            original_adgroup_id=group.remote_id,
            remote_adgroup_id=replacement_adgroup_id,
            remote_ad_id=replacement_ad_id,
            source_request_digest=source_request_digest,
            intent_digest=digest(frozen_intent),
            request_digest=request_digest,
            audit_reference=audit_reference,
            route=route.model_dump(mode="json"),
            original_intent={
                "ad": source.request_body,
                "adgroup": group.request_body,
                "adgroup_digest": group.request_body_digest,
            },
            replacement_intent=frozen_intent,
            verification=verification,
        )
        if datetime.now(UTC) >= task_deadline:
            raise _invalid("replacement_deadline_invalid")
        session.add(row)
        session.flush()
        from app.modules.builds.submissions import get_submission

        submission.status = get_submission(
            session, context=context, submission_id=submission.id
        ).status
        submission.updated_at = datetime.now(UTC)
        session.add(submission)
        return row.id
