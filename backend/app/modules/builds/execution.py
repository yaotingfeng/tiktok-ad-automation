"""One bounded create or local material dependency per committed attempt."""

from datetime import UTC, datetime
from decimal import Decimal
from math import ceil, isfinite
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import AccountAdmissionDeferred, sdk_client
from app.modules.accounts.access import resolve_account_access
from app.modules.builds.execution_admission import HARD_LIMIT, admitted_build_call
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.execution_state import (
    active_attempt,
    arm_request,
    evidence,
    finish_local,
    preserve_created_receipt,
    record_created,
    record_unknown,
)
from app.modules.builds.preview_models import (
    PlannedAd,
    PlannedGroup,
    PreviewGroupMaterial,
)
from app.modules.builds.preview_schemas import FrozenUnit
from app.modules.builds.scene import read_scene_context
from app.modules.builds.scene_jobs import ensure_scene_preparation
from app.modules.builds.sdk_requests import (
    CREATE_ENDPOINTS,
    PORTFOLIO_ENDPOINT,
    RemoteCreated,
    TikTokResponseError,
    ad_assets,
    compile_request,
    cta_portfolio,
    invoke_create,
    invoke_portfolio,
)
from app.modules.builds.submissions import claim_step, load_execution_unit
from app.modules.materials.distribution import ensure_target_asset
from app.modules.tenants.permissions import require_tenant

SCENE_FIELDS = (
    "campaign_fields",
    "adgroup_fields",
    "creative_fields",
    "cta_fields",
    "field_constraints",
    "name_limit",
    "creative_limit",
    "copy_length_limit",
)


def _require_bounded_worker() -> None:
    task = current_task
    limit = (
        (
            (task.request.timelimit or (None, None))[0]
            or getattr(task, "time_limit", None)
        )
        if task
        else None
    )
    if (
        not task
        or not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or task.request.called_directly
        or task.request.is_eager
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not 0 < limit <= HARD_LIMIT
    ):
        raise DomainError("build_worker_unbounded", "广告执行需要有界后台任务")


def exact_number(value: Decimal) -> int | float:
    """The official JSON serializer needs numbers; reject any lossy conversion."""
    if not value.is_finite():
        raise DomainError("invalid_build_request", "预算或出价无效")
    number = float(value)
    if not isfinite(number) or Decimal(str(number)) != value:
        raise DomainError("decimal_serialization_loss", "数值精度超出官方 SDK 支持范围")
    return int(value) if value == value.to_integral() else number


def _frozen(session: Session, context: TenantContext, claim: StepClaim) -> FrozenUnit:
    unit = load_execution_unit(
        session,
        context=context,
        submission_id=claim.submission_id,
        unit_id=claim.unit_id,
    )
    if unit.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    access = resolve_account_access(
        session,
        context=context,
        bc_id=claim.bc_id,
        advertiser_id=claim.advertiser_id,
        action="build",
    )
    if access.connection_id != unit.frozen.connection_id:
        raise DomainError("account_authorization_changed", "冻结账户授权已变化")
    if (access.currency, access.timezone) != (
        unit.frozen.currency,
        unit.frozen.timezone,
    ):
        raise DomainError("account_metadata_changed", "账户币种或时区已变化")
    return unit.frozen


def _current_scene(
    session: Session, context: TenantContext, frozen: FrozenUnit
) -> None:
    scene = read_scene_context(
        session,
        context=context,
        bc_id=frozen.bc_id,
        advertiser_id=frozen.advertiser_id,
        link_id=frozen.link_id,
    )
    if not scene.supported:
        transient = bool(
            set(scene.reason_codes)
            & {
                "scene_evidence_missing",
                "scene_evidence_expired",
                "account_scope_unverified",
                "account_build_unverified",
            }
        )
        raise DomainError(
            "scene_refresh_required" if transient else "scene_no_longer_supported",
            "当前账户投放信息需要重新核实",
            retryable=transient,
        )
    snapshot = scene.to_snapshot()
    # New evidence IDs alone must not change frozen business intent. New facts do.
    if any(snapshot.get(key) != frozen.scene_snapshot.get(key) for key in SCENE_FIELDS):
        raise DomainError(
            "scene_intent_changed", "当前投放条件与预览不一致，请重新生成预览"
        )


def _parent(session: Session, step: ExecutionStep) -> str:
    parent = (
        session.get(ExecutionStep, step.parent_step_id) if step.parent_step_id else None
    )
    if (
        parent is None
        or parent.tenant_id != step.tenant_id
        or parent.submission_id != step.submission_id
        or parent.status != "SUCCEEDED"
        or not parent.remote_id
    ):
        raise DomainError("parent_not_ready", "上级对象尚未创建", retryable=True)
    return parent.remote_id


def _group(session: Session, step: ExecutionStep) -> PlannedGroup:
    group = session.exec(
        select(PlannedGroup).where(
            PlannedGroup.tenant_id == step.tenant_id,
            PlannedGroup.preview_id == step.preview_id,
            PlannedGroup.unit_id == step.unit_id,
            PlannedGroup.id == step.group_id,
        )
    ).one_or_none()
    if group is None:
        raise DomainError("resource_not_found", "冻结广告组不存在")
    return group


def prepare_request(
    session: Session, *, context: TenantContext, claim: StepClaim, frozen: FrozenUnit
) -> dict[str, Any]:
    """Only local reads / durable material preparation; never calls an SDK."""
    step = session.get(ExecutionStep, claim.step_id)
    assert step
    snapshot = frozen.scene_snapshot
    if step.kind == "CTA":
        first = session.exec(
            select(PlannedAd)
            .join(
                PlannedGroup,
                (col(PlannedGroup.tenant_id) == PlannedAd.tenant_id)
                & (col(PlannedGroup.id) == PlannedAd.group_id),
            )
            .where(
                PlannedAd.tenant_id == step.tenant_id,
                PlannedGroup.unit_id == step.unit_id,
            )
            .order_by(col(PlannedAd.id))
            .limit(1)
        ).first()
        if first is None:
            raise DomainError("cta_unavailable", "冻结创意缺少 CTA")
        selected = set(first.cta_option_ids)
        content = []
        seen: set[str] = set()
        for asset in snapshot["cta_fields"].get("recommend_assets", []):
            ids = [value for value in asset["asset_ids"] if value in selected]
            if ids:
                content.append(
                    {"asset_ids": ids, "asset_content": asset["asset_content"]}
                )
                seen.update(ids)
        if seen != selected:
            raise DomainError("cta_unavailable", "所选 CTA 缺少冻结推荐证据")
        return cta_portfolio(advertiser_id=frozen.advertiser_id, assets=content)
    fixed: dict[str, Any] = {"advertiser_id": frozen.advertiser_id}
    resolved: dict[str, Any]
    if step.kind == "CAMPAIGN":
        fixed.update(
            campaign_name=frozen.campaign_name, budget=exact_number(frozen.budget)
        )
        resolved = snapshot["campaign_fields"]
    elif step.kind == "ADGROUP":
        group = _group(session, step)
        fixed.update(
            campaign_id=_parent(session, step),
            adgroup_name=group.name,
            roas_bid=exact_number(frozen.target_roas),
        )
        resolved = dict(snapshot["adgroup_fields"])
        if not resolved.get("targeting_spec", {}).get("location_ids"):
            raise DomainError("scene_targeting_unavailable", "缺少已核实的投放地区")
        # FROM_NOW has a required UTC start. Resolve once at first arm; the entire
        # body is persisted and immutable before I/O, including this timestamp.
        resolved.update(
            schedule_type="SCHEDULE_FROM_NOW",
            schedule_start_time=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        )
    elif step.kind == "AD":
        group = _group(session, step)
        ad = session.exec(
            select(PlannedAd).where(
                PlannedAd.tenant_id == step.tenant_id,
                PlannedAd.group_id == group.id,
                PlannedAd.id == step.planned_ad_id,
            )
        ).one_or_none()
        if ad is None:
            raise DomainError("resource_not_found", "冻结创意不存在")
        material_ids = session.exec(
            select(PreviewGroupMaterial.material_id)
            .where(
                PreviewGroupMaterial.tenant_id == step.tenant_id,
                PreviewGroupMaterial.preview_id == step.preview_id,
                PreviewGroupMaterial.drama_id == frozen.drama_id,
                PreviewGroupMaterial.group_no == group.group_no,
            )
            .order_by(col(PreviewGroupMaterial.position))
            .limit(51)
        ).all()
        if not 1 <= len(material_ids) <= 50:
            raise DomainError("invalid_material_group", "冻结素材组无效")
        mappings = []
        waiting = False
        for material_id in material_ids:
            ready = ensure_target_asset(
                session,
                context=context,
                bc_id=claim.bc_id,
                material_id=material_id,
                advertiser_id=claim.advertiser_id,
                task_key=f"build:{claim.step_id}:{material_id}",
            )
            if ready.state == "blocked":
                raise DomainError(
                    ready.reason_code or "target_asset_unavailable", "目标素材不可用"
                )
            if ready.state != "ready" or not ready.mapping:
                waiting = True
                continue
            mappings.append(
                {
                    "video_id": ready.mapping.video_id,
                    "image_id": ready.mapping.image_id or "",
                }
            )
        if waiting:
            # Caller commits the queued dependencies before waiting.
            raise DomainError(
                "material_refresh_required", "正在核实目标素材", retryable=True
            )
        identity = {
            key: value
            for key, value in snapshot["creative_fields"]["creative_info"].items()
            if key != "ad_format"
        }
        resolved = ad_assets(mappings, text=ad.text, url=frozen.url, identity=identity)
        cta = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == step.tenant_id,
                ExecutionStep.submission_id == step.submission_id,
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind == "CTA",
            )
        ).one()
        if cta.status != "SUCCEEDED" or not cta.remote_id:
            raise DomainError("cta_not_ready", "CTA 尚未准备完成", retryable=True)
        resolved["ad_configuration"] = {"call_to_action_id": cta.remote_id}
        fixed.update(adgroup_id=_parent(session, step), ad_name=ad.name)
    else:
        raise DomainError("invalid_build_kind", "该步骤不是广告创建请求")
    return compile_request(step.kind.lower(), fixed=fixed, resolved=resolved)


def _local_result(
    database_engine: Any, claim: StepClaim, error: DomainError, *, delay: int = 15
) -> str:
    with Session(database_engine) as session, session.begin():
        return finish_local(
            session,
            claim=claim,
            status="PENDING" if error.retryable else "FAILED",
            code=error.code,
            delay=delay,
        )


def _prepare_scene_dependency(
    database_engine: Any, context: TenantContext, claim: StepClaim
) -> str:
    """Commit resource preparation separately after the unarmed read transaction ends."""
    with Session(database_engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.id == claim.step_id,
                ExecutionStep.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).one()
        if not active_attempt(step, claim, phase="CLAIMED"):
            return step.status
        try:
            frozen = _frozen(session, context, claim)
            preparation = ensure_scene_preparation(
                session,
                context=context,
                bc_id=frozen.bc_id,
                advertiser_id=frozen.advertiser_id,
                link_id=frozen.link_id,
            )
            return finish_local(
                session,
                claim=claim,
                status="FAILED" if preparation.state == "blocked" else "PENDING",
                code=preparation.reason_code
                if preparation.state == "blocked"
                else "scene_refresh_required",
                delay=0 if preparation.state == "ready" else 15,
            )
        except DomainError as error:
            return finish_local(
                session,
                claim=claim,
                status="PENDING" if error.retryable else "FAILED",
                code=error.code,
                delay=15,
            )


def process_step(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    step_id: UUID,
    revision: int,
) -> str:
    """One effect maximum. Any uncertainty after arming is read-only recovery."""
    _require_bounded_worker()
    if type(revision) is not int or revision < 0:
        raise DomainError("dispatch_payload_invalid", "执行代际无效")
    with Session(database_engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.id == step_id,
            )
            .with_for_update()
        ).one_or_none()
        if step is None:
            raise DomainError("resource_not_found", "执行步骤不存在")
        submission = session.get(Submission, step.submission_id)
        if submission is None or submission.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "提交操作者不匹配")
        if (
            step.dispatch_revision != revision
            or step.status in {"SUCCEEDED", "FAILED", "UNKNOWN"}
            or step.remote_id
            or step.phase == "REQUEST_ARMED"
        ):
            return step.status
        if step.kind == "READBACK":
            raise DomainError("invalid_build_kind", "回读步骤需要只读执行器")
        try:
            claim = claim_step(
                session,
                context=context,
                step_id=step.id,
                owner=uuid4(),
                lease_seconds=60,
            )
        except DomainError as error:
            step.status, step.error_code, step.phase = "FAILED", error.code, "DONE"
            evidence(
                session,
                step=step,
                claim=None,
                conclusion="LOCAL_FAILED",
                summary={"reason_code": error.code},
            )
            session.add(step)
            return "FAILED"
        if claim is None:
            return step.status
    assert claim
    armed = False
    result: RemoteCreated | None = None
    try:
        with Session(database_engine) as session, session.begin():
            frozen = _frozen(session, context, claim)
            if claim.kind == "MATERIAL":
                assert claim.material_id
                ready = ensure_target_asset(
                    session,
                    context=context,
                    bc_id=claim.bc_id,
                    material_id=claim.material_id,
                    advertiser_id=claim.advertiser_id,
                    task_key=f"build:{claim.step_id}",
                )
                if ready.state == "queued":
                    step = session.get(ExecutionStep, claim.step_id)
                    assert step
                    step.distribution_id = ready.task_id
                    session.add(step)
                    return finish_local(
                        session,
                        claim=claim,
                        status="PENDING",
                        code="material_pending",
                        delay=15,
                    )
                if (
                    ready.state == "blocked"
                    or not ready.mapping
                    or not ready.mapping.image_id
                ):
                    return finish_local(
                        session,
                        claim=claim,
                        status="FAILED",
                        code=ready.reason_code or "target_asset_incomplete",
                    )
                return finish_local(
                    session,
                    claim=claim,
                    status="SUCCEEDED",
                    resolved={"mapping": ready.mapping.model_dump(mode="json")},
                )
            _current_scene(session, context, frozen)
            try:
                body = prepare_request(
                    session, context=context, claim=claim, frozen=frozen
                )
            except DomainError as error:
                # Catch inside the transaction so newly queued material dependencies
                # and this pending receipt commit together instead of being rolled back.
                return finish_local(
                    session,
                    claim=claim,
                    status="PENDING" if error.retryable else "FAILED",
                    code=error.code,
                    delay=15,
                )
        endpoint = (
            PORTFOLIO_ENDPOINT
            if claim.kind == "CTA"
            else CREATE_ENDPOINTS[claim.kind.lower()]
        )
        with admitted_build_call(
            redis_client,
            context=context,
            endpoint=endpoint,
            advertiser_id=claim.advertiser_id,
        ):
            with Session(database_engine) as session:
                frozen = _frozen(session, context, claim)
                _current_scene(session, context, frozen)
                with sdk_client(
                    session, context=context, connection_id=frozen.connection_id
                ) as client:
                    arm_request(session, context=context, claim=claim, body=body)
                    session.commit()
                    session.close()
                    armed = True
                    result = (
                        invoke_portfolio(client, body=body)
                        if claim.kind == "CTA"
                        else invoke_create(client, kind=claim.kind.lower(), body=body)
                    )
                    # Save the known ID before client cleanup; cleanup failure cannot
                    # turn a recorded success into a duplicate create.
                    try:
                        with Session(database_engine) as receipt, receipt.begin():
                            outcome = record_created(receipt, claim=claim, result=result)
                    except Exception:
                        # Cleanup may itself terminate this worker. Preserve the
                        # received ID before entering that cleanup boundary.
                        with Session(database_engine) as receipt, receipt.begin():
                            outcome = preserve_created_receipt(
                                receipt, claim=claim, result=result
                            )
            return outcome
    except AccountAdmissionDeferred as error:
        return _local_result(
            database_engine,
            claim,
            error,
            delay=max(1, ceil(error.retry_after_ms / 1000)),
        )
    except Exception as error:
        if armed:
            with Session(database_engine) as session, session.begin():
                if result is not None:
                    # A received ID is useful even when its success transaction
                    # rolled back. CTA has no unknown-ID list fallback. Append the
                    # receipt without overwriting any newer owner or claiming a
                    # failed commit succeeded; subsequent recovery is read-only.
                    return preserve_created_receipt(session, claim=claim, result=result)
                return record_unknown(
                    session,
                    claim=claim,
                    code=error.code
                    if isinstance(error, TikTokResponseError)
                    else "create_result_unknown",
                    request_id=error.request_id
                    if isinstance(error, TikTokResponseError)
                    else None,
                    remote_code=error.remote_code
                    if isinstance(error, TikTokResponseError)
                    else -1,
                )
        safe = (
            error
            if isinstance(error, DomainError)
            else DomainError("execution_prepare_failed", "执行准备未完成")
        )
        if safe.code == "scene_refresh_required":
            return _prepare_scene_dependency(database_engine, context, claim)
        return _local_result(database_engine, claim, safe)
