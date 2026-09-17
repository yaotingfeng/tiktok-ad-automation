"""共享后按 MID 发现实际 VID，已知 VID 合并详情；成员独立落账与恢复。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, or_, true
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.materials import RemoteCallBudget, VideoRecord
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.jobs.admission import admission_policy

from . import sdk_assets as api
from .models import MaterialAssetOperation, MaterialDistribution, MaterialUploadAttempt
from .readiness import require_execution_config
from .routes import load_material_route
from .source_uploads import (
    READ_CLAIM_SECONDS,
    READ_HARD_LIMIT,
    _locked_material,
    _locked_operation,
)


def try_verify_batch(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    operation_id: UUID | None,
    revision: int | None,
    recovery_claim_id: UUID | None,
) -> bool:
    from . import distribution as single

    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=READ_HARD_LIMIT - 5)
    with Session(database_engine) as db, db.begin():
        anchor = single._load_distribution(db, context, distribution_id)
        anchor_op = db.get(MaterialAssetOperation, anchor.operation_id)
        if anchor_op is None or anchor_op.status not in {"verifying", "result_unknown"}:
            return False
        discover = (
            not anchor_op.remote_response.get("video_id")
            and anchor_op.remote_response.get("transport") == "native_share"
            and bool(anchor_op.remote_response.get("source_mid"))
            and not anchor_op.remote_response.get("batch_discovery_incomplete")
        )
        if not discover and not anchor_op.remote_response.get("video_id"):
            return False
        endpoint = "materials.search_videos" if discover else "materials.get_videos"
        if operation_id is not None and anchor_op.id != operation_id:
            return False
        if (
            revision is not None
            and anchor_op.remote_response.get("revision", 0) != revision
        ):
            return False
        if (
            recovery_claim_id is not None
            and anchor_op.attempt_token != recovery_claim_id
        ):
            return False
        selected = db.exec(
            select(MaterialDistribution, MaterialAssetOperation)
            .join(
                MaterialAssetOperation,
                col(MaterialDistribution.operation_id)
                == col(MaterialAssetOperation.id),
            )
            .where(
                MaterialDistribution.tenant_id == context.tenant_id,
                MaterialDistribution.bc_id == anchor.bc_id,
                MaterialDistribution.actor_id == context.actor_id,
                MaterialDistribution.advertiser_id == anchor.advertiser_id,
                col(MaterialDistribution.status).in_(["verifying", "result_unknown"]),
                col(MaterialAssetOperation.status).in_(["verifying", "result_unknown"]),
                col(MaterialDistribution.target_route) == anchor.target_route,
                col(MaterialAssetOperation.frozen_route) == anchor.target_route,
                col(MaterialAssetOperation.remote_response)[
                    "source_mid" if discover else "video_id"
                ].astext.is_not(None),
                and_(
                    col(MaterialAssetOperation.remote_response)["video_id"].astext.is_(
                        None
                    ),
                    col(MaterialAssetOperation.remote_response)["transport"].astext
                    == "native_share",
                    col(MaterialAssetOperation.remote_response)[
                        "batch_discovery_incomplete"
                    ].astext.is_(None),
                )
                if discover
                else true(),
                col(MaterialAssetOperation.remote_response)[
                    "conflicting_video_id"
                ].astext.is_(None),
                or_(
                    col(MaterialAssetOperation.claimed_until).is_(None),
                    col(MaterialAssetOperation.claimed_until) <= now,
                ),
                ~select(MaterialUploadAttempt.id)
                .where(
                    col(MaterialUploadAttempt.operation_id)
                    == col(MaterialAssetOperation.id)
                )
                .exists(),
            )
            .order_by(
                case((col(MaterialDistribution.id) == anchor.id, 0), else_=1),
                col(MaterialDistribution.material_id),
            )
            # 搜索的 material_ids 上限是 20，不是详情批读的 50；超过后
            # 平台固定返回 40002，原任务会一直验证失败且无法发布目标映射。
            .limit(20 if discover else 50)
        ).all()
        # 单项共享同样留下冻结 MID，多个结果可安全合并读取；不能要求它们
        # 必须来自批量发送账本，否则 seed 等待者逐项共享后永远逐项核实。
        # 仅一个非批量成员时仍沿原单项分页路径，不改变既有单条恢复语义。
        minimum = (
            1 if discover and anchor_op.remote_response.get("share_batch_id") else 2
        )
        if len(selected) < minimum or not any(
            dist.id == anchor.id for dist, _ in selected
        ):
            return False
        for material_id in sorted({dist.material_id for dist, _ in selected}):
            _locked_material(db, context, material_id)
        work: list[dict[str, Any]] = []
        for dist, operation in selected:
            db.refresh(dist)
            op = _locked_operation(db, context, operation.id)
            if (
                op.status not in {"verifying", "result_unknown"}
                or dist.status not in {"verifying", "result_unknown"}
                or dist.operation_id != op.id
                or (op.claimed_until and op.claimed_until > now)
            ):
                return False
        route = load_material_route(
            anchor.target_route, context=context, bc_id=anchor.bc_id
        )
        require_execution_config(upload=False, endpoint=endpoint, channel=route.channel)
        for dist, op in selected:
            material = _locked_material(db, context, dist.material_id)
            op.attempt_token = uuid4()
            op.claimed_until = now + timedelta(seconds=READ_CLAIM_SECONDS)
            op.remote_response = {
                **op.remote_response,
                "revision": op.remote_response.get("revision", 0) + 1,
            }
            work.append(
                {
                    "dist": dist.id,
                    "material": dist.material_id,
                    "route": dist.target_route,
                    "operation": op.id,
                    "claim": op.attempt_token,
                    "digest": op.request_digest,
                    "video_id": op.remote_response.get("video_id"),
                    "source_mid": op.remote_response.get("source_mid"),
                    "remote_name": op.remote_response.get("remote_name"),
                    "md5": material.video_md5,
                    "size": material.byte_size,
                    "strict": material.current_object_generation is not None
                    or op.remote_response.get("transport") == "url_relay"
                    or op.remote_response.get("batch_discovered", False),
                }
            )
            single.queue_distribution(
                db,
                dist,
                op,
                kind="verify",
                due=op.claimed_until,
                claim_id=op.attempt_token,
            )
        advertiser_id = anchor.advertiser_id

    def check_current() -> None:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            for item in sorted(
                work, key=lambda item: (item["material"], item["operation"])
            ):
                dist = single._load_distribution(db, context, item["dist"])
                material = _locked_material(db, context, dist.material_id)
                op = _locked_operation(db, context, item["operation"])
                if (
                    dist.bc_id != route.bc_id
                    or dist.advertiser_id != advertiser_id
                    or dist.material_id != item["material"]
                    or op.attempt_token != item["claim"]
                    or dist.operation_id != op.id
                    or op.request_digest != item["digest"]
                    or op.remote_response.get("video_id") != item["video_id"]
                    or (
                        discover
                        and op.remote_response.get("source_mid") != item["source_mid"]
                    )
                    or (
                        discover
                        and op.remote_response.get("remote_name") != item["remote_name"]
                    )
                    or material.video_md5 != item["md5"]
                    or material.byte_size != item["size"]
                    or dist.target_route != item["route"]
                    or op.frozen_route != item["route"]
                    or op.claimed_until is None
                    or op.claimed_until <= datetime.now(UTC)
                ):
                    raise DomainError("material_claim_changed", "目标核实成员已变化")
            # 成员逐一锁定并校验冻结边界；同一账户授权每次物理请求只查一次，
            # 不在请求之间缓存，权限撤销仍会在发送前阻断整批。
            single._target_access(
                db, context, dist, upload=False, connection_id=route.connection_id
            )

    error_code = None
    records: tuple[VideoRecord, ...] = ()
    incomplete_discovery = False
    try:
        check_current()
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check_current,
        ) as gateway:
            budget = RemoteCallBudget(
                deadline, READ_HARD_LIMIT, admission_policy(endpoint).lease_ms
            )
            if discover:
                mids = tuple(dict.fromkeys(item["source_mid"] for item in work))
                page = gateway.materials.search_videos(
                    advertiser_id=advertiser_id,
                    material_ids=mids,
                    page=1,
                    video_name=None,
                    budget=budget,
                )
                # 一页完整枚举才可按 MID 排除同名/重复；其余沿原逐项只读分页恢复。
                incomplete_discovery = (
                    page.page != 1
                    or page.total_pages > 1
                    or page.total_number != len(page.rows)
                    or any(row.mid not in mids for row in page.rows)
                )
                if not incomplete_discovery:
                    records = page.rows
            else:
                records = gateway.materials.read_videos(
                    advertiser_id=advertiser_id,
                    video_ids=tuple(dict.fromkeys(item["video_id"] for item in work)),
                    budget=budget,
                )
    except SDK_SCOPE_INTERRUPTS:
        raise
    except Exception as error:
        error_code = (
            error.code
            if isinstance(error, DomainError)
            else "material_response_unknown"
        )
        if discover and error_code.startswith("unsupported"):
            incomplete_discovery = True
    by_id = {record.video_id: record for record in records}
    with Session(database_engine) as db, db.begin():
        for item in sorted(
            work, key=lambda item: (item["material"], item["operation"])
        ):
            dist = single._load_distribution(db, context, item["dist"])
            material = _locked_material(db, context, dist.material_id)
            op = _locked_operation(db, context, item["operation"])
            if (
                op.attempt_token != item["claim"]
                or dist.operation_id != op.id
                or op.request_digest != item["digest"]
                or op.remote_response.get("video_id") != item["video_id"]
                or (
                    discover
                    and op.remote_response.get("source_mid") != item["source_mid"]
                )
                or (
                    discover
                    and op.remote_response.get("remote_name") != item["remote_name"]
                )
                or op.frozen_route != item["route"]
                or dist.target_route != item["route"]
                or material.video_md5 != item["md5"]
                or material.byte_size != item["size"]
                or op.claimed_until is None
                or op.claimed_until <= datetime.now(UTC)
            ):
                continue
            record = by_id.get(item["video_id"])
            ambiguous = incomplete_discovery
            candidate_identity = False
            if discover:
                matches = [row for row in records if row.mid == item["source_mid"]]
                ambiguous = ambiguous or len(matches) > 1
                record = matches[0] if len(matches) == 1 else None
                candidate_identity = bool(
                    record is not None
                    and record.md5 == item["md5"]
                    and record.file_name == item["remote_name"]
                    and (record.size is None or record.size == item["size"])
                )
                if not candidate_identity:
                    record = None
            evidence = api.verified_video(
                {"list": [api.video_record_data(record)] if record else []},
                md5=item["md5"] or "",
                expected_video_id=record.video_id
                if discover and record
                else item["video_id"] or "",
                expected_size=item["size"] if discover or item["strict"] else None,
            )
            code = error_code
            try:
                if evidence:
                    single._publish_mapping(
                        db, context, dist, evidence, connection_id=route.connection_id
                    )
                    op.remote_response = {**op.remote_response, **evidence}
                    op.status, dist.status = "succeeded", "ready"
                else:
                    op.status, dist.status = "verifying", "verifying"
                    if discover and candidate_identity and record is not None:
                        # 仅缺可用状态/尺寸时保留真实 VID，后续详情读取必须继续核对尺寸。
                        op.remote_response = {
                            **op.remote_response,
                            "video_id": record.video_id,
                            "batch_discovered": True,
                        }
                    elif discover and ambiguous:
                        op.remote_response = {
                            **op.remote_response,
                            "batch_discovery_incomplete": True,
                        }
            except DomainError as error:
                code = error.code
                op.status, dist.status = "result_unknown", "blocked"
            op.attempt_token, op.claimed_until = None, None
            dist.reason_code = (
                None if dist.status == "ready" else code or "material_result_pending"
            )
            from .batch_distribution import sync_batch_member

            sync_batch_member(db, op, dist)
            if dist.status == "verifying":
                single.queue_distribution(
                    db,
                    dist,
                    op,
                    kind="verify",
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )
    return True
