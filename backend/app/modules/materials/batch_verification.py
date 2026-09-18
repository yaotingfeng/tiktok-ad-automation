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
from .batch_models import MaterialShareBatch
from .models import (
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
    MaterialUploadAttempt,
)
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
    read_only: bool = False,
) -> bool:
    from . import distribution as single

    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=READ_HARD_LIMIT - 5)
    with Session(database_engine) as db, db.begin():
        anchor = single._load_distribution(db, context, distribution_id)
        anchor_op = db.get(MaterialAssetOperation, anchor.operation_id)
        if (
            anchor_op is None
            or anchor.superseded_by_id is not None
            or anchor_op.superseded_by_id is not None
            or anchor_op.status not in {"verifying", "result_unknown"}
        ):
            return False
        if anchor_op.remote_response.get(
            "reconciliation_complete"
        ) or anchor_op.remote_response.get("reconciliation_stopped"):
            return False
        discover = (
            not anchor_op.remote_response.get("video_id")
            and anchor_op.remote_response.get("transport") == "native_share"
            and bool(anchor_op.remote_response.get("source_mid"))
            and not anchor_op.remote_response.get("batch_discovery_incomplete")
        )
        if read_only and not discover:
            return False
        if not discover and not anchor_op.remote_response.get("video_id"):
            return False
        # 已有VID的刷新分发初始是queued，但其操作已进入verifying。
        # 它们同样可以合并只读核验；无VID的共享发现仍只接收已发送状态。
        candidate_states = (
            ("verifying", "result_unknown")
            if discover
            else ("queued", "verifying", "result_unknown")
        )
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
                col(MaterialDistribution.superseded_by_id).is_(None),
                col(MaterialAssetOperation.superseded_by_id).is_(None),
                MaterialDistribution.bc_id == anchor.bc_id,
                MaterialDistribution.actor_id == context.actor_id,
                MaterialDistribution.advertiser_id == anchor.advertiser_id,
                MaterialDistribution.id == anchor.id if read_only else true(),
                col(MaterialDistribution.status).in_(candidate_states),
                col(MaterialAssetOperation.status).in_(["verifying", "result_unknown"]),
                col(MaterialAssetOperation.remote_response)[
                    "reconciliation_complete"
                ].astext.is_distinct_from("true"),
                col(MaterialAssetOperation.remote_response)[
                    "reconciliation_stopped"
                ].astext.is_distinct_from("true"),
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
        # 单个已 ACK 原生共享也需先做目标 VID 实证；未 ACK 单项保留原分页语义。
        minimum = (
            1
            if read_only
            or (discover and anchor_op.remote_response.get("share_batch_id"))
            or (
                discover
                and anchor_op.remote_response.get("share_acknowledged") is True
                and anchor_op.remote_response.get("source_video_id")
                and anchor_op.remote_response.get("source_bc_id") == anchor.bc_id
            )
            else 2
        )
        if len(selected) < minimum or not any(
            dist.id == anchor.id for dist, _ in selected
        ):
            return False
        for material_id in sorted({dist.material_id for dist, _ in selected}):
            _locked_material(db, context, material_id)
        work: list[dict[str, Any]] = []
        eligible = []
        for dist, operation in selected:
            db.refresh(dist)
            op = _locked_operation(db, context, operation.id)
            if (
                op.status not in {"verifying", "result_unknown"}
                or op.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or op.remote_response.get("reconciliation_complete")
                or op.remote_response.get("reconciliation_stopped")
                or dist.status not in candidate_states
                or dist.operation_id != op.id
                or (op.claimed_until and op.claimed_until > now)
            ):
                return False
            if dist.id == anchor.id and not single._delivery_matches(
                op,
                operation_id=operation_id,
                revision=revision,
                recovery_claim_id=recovery_claim_id,
            ):
                return False
            # 批量入口不能绕过逐项预算；锁内停止耗尽成员，其他成员保留独立身份。
            if single._reconciliation_exhausted(op):
                single._stop_reconciliation(dist, op)
                continue
            eligible.append((dist, op))
        selected = eligible
        if len(selected) < minimum or not any(
            dist.id == anchor.id for dist, _ in selected
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
            single._claim_reconciliation(op)
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
                    "source_video_id": op.remote_response.get("source_video_id"),
                    "source_bc_id": op.remote_response.get("source_bc_id"),
                    "transport": op.remote_response.get("transport"),
                    "share_acknowledged": op.remote_response.get("share_acknowledged"),
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
                read_only=read_only,
            )
        advertiser_id = anchor.advertiser_id
        # 新增的单项 VID 候选读取不得把原名称分页恢复改成 MID 批量发现。
        name_fallback = (
            discover
            and len(work) == 1
            and (read_only or not anchor_op.remote_response.get("share_batch_id"))
        )

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
                    or any(
                        op.remote_response.get(key) != item[key]
                        for key in (
                            "transport",
                            "source_video_id",
                            "source_bc_id",
                            "share_acknowledged",
                        )
                    )
                    or op.superseded_by_id is not None
                    or dist.superseded_by_id is not None
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
    admission_deferred = False
    records: tuple[VideoRecord, ...] = ()
    direct_evidence: dict[UUID, dict[str, str]] = {}
    name_matches: set[str] = set()
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
                candidates = [
                    item
                    for item in work
                    if item["share_acknowledged"] is True
                    and item["source_bc_id"] == route.bc_id
                    and isinstance(item["source_video_id"], str)
                    and item["source_video_id"]
                ]
                if candidates:
                    # 原生共享 ACK 不等于目标可用；仅把源 VID 当查询候选，
                    # 必须由冻结目标账户详情回读证明身份、内容、尺寸和可用性。
                    require_execution_config(
                        upload=False,
                        endpoint="materials.get_videos",
                        channel=route.channel,
                    )
                    target_records = gateway.materials.read_videos(
                        advertiser_id=advertiser_id,
                        video_ids=tuple(
                            dict.fromkeys(
                                item["source_video_id"] for item in candidates
                            )
                        ),
                        budget=RemoteCallBudget(
                            deadline,
                            READ_HARD_LIMIT,
                            admission_policy("materials.get_videos").lease_ms,
                        ),
                    )
                    for item in candidates:
                        matches = [
                            row
                            for row in target_records
                            if row.video_id == item["source_video_id"]
                            and row.advertiser_id == advertiser_id
                        ]
                        record = matches[0] if len(matches) == 1 else None
                        evidence = (
                            api.video_identity(
                                record,
                                advertiser_id=advertiser_id,
                                video_id=item["source_video_id"],
                                md5=item["md5"] or "",
                                expected_size=item["size"],
                            )
                            if record is not None and record.displayable is True
                            else None
                        )
                        if evidence:
                            direct_evidence[item["operation"]] = evidence
                remaining = [
                    item for item in work if item["operation"] not in direct_evidence
                ]
                if remaining:
                    mids = tuple(
                        dict.fromkeys(item["source_mid"] for item in remaining)
                    )
                    page = gateway.materials.search_videos(
                        advertiser_id=advertiser_id,
                        material_ids=() if name_fallback else mids,
                        page=1,
                        video_name=remaining[0]["remote_name"]
                        if name_fallback
                        else None,
                        budget=budget,
                    )
                    if name_fallback:
                        identities, _ = api.search_page(
                            api.video_page_data(page),
                            page=1,
                            remote_name=remaining[0]["remote_name"],
                            md5=remaining[0]["md5"] or "",
                        )
                        name_matches = {identity["video_id"] for identity in identities}
                    # 一页完整枚举才可按 MID 排除同名/重复；其余沿原逐项只读分页恢复。
                    incomplete_discovery = (
                        page.page != 1
                        or page.total_pages > 1
                        or (
                            page.total_number != len(page.rows)
                            and (not name_fallback or page.total_number is not None)
                        )
                        or (
                            not name_fallback
                            and any(row.mid not in mids for row in page.rows)
                        )
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
        admission_deferred = isinstance(error, api.SdkAdmissionDeferred)
        error_code = (
            error.code
            if isinstance(error, DomainError)
            else "material_response_unknown"
        )
        if discover and error_code.startswith("unsupported"):
            incomplete_discovery = True
    by_id = {record.video_id: record for record in records}
    with Session(database_engine) as db, db.begin():
        # 一个核验窗口可能跨多个物理共享批次。先锁齐素材、操作，再按批次 ID
        # 锁齐账本；不能在逐素材落账时交错获取批次锁，否则不同账户会形成锁环。
        db.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_({item["material"] for item in work}),
            )
            .order_by(col(MaterialFile.id))
            .with_for_update()
        ).all()
        operations = db.exec(
            select(MaterialAssetOperation)
            .where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                col(MaterialAssetOperation.id).in_(
                    {item["operation"] for item in work}
                ),
            )
            .order_by(col(MaterialAssetOperation.id))
            .with_for_update()
        ).all()
        batch_ids = {
            UUID(op.remote_response["share_batch_id"])
            for op in operations
            if op.remote_response.get("share_batch_id")
        }
        if batch_ids:
            db.exec(
                select(MaterialShareBatch)
                .where(
                    MaterialShareBatch.tenant_id == context.tenant_id,
                    col(MaterialShareBatch.id).in_(batch_ids),
                )
                .order_by(col(MaterialShareBatch.id))
                .with_for_update()
            ).all()
        for item in sorted(
            work, key=lambda item: (item["material"], item["operation"])
        ):
            dist = single._load_distribution(db, context, item["dist"])
            material = _locked_material(db, context, dist.material_id)
            op = _locked_operation(db, context, item["operation"])
            if (
                op.attempt_token != item["claim"]
                or op.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or dist.operation_id != op.id
                or op.request_digest != item["digest"]
                or op.remote_response.get("video_id") != item["video_id"]
                or any(
                    op.remote_response.get(key) != item[key]
                    for key in (
                        "transport",
                        "source_video_id",
                        "source_bc_id",
                        "share_acknowledged",
                    )
                )
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
                if name_fallback:
                    # 共用原单项名称/摘要匹配，不自建第二套名称身份规则。
                    matches = [row for row in records if row.video_id in name_matches]
                else:
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
            evidence = direct_evidence.get(item["operation"]) or api.verified_video(
                {"list": [api.video_record_data(record)] if record else []},
                md5=item["md5"] or "",
                expected_video_id=record.video_id
                if discover and record
                else item["video_id"] or "",
                expected_size=item["size"]
                if (discover and not name_fallback) or item["strict"]
                else None,
            )
            code = None if evidence else error_code
            try:
                if evidence:
                    single._publish_mapping(
                        db, context, dist, evidence, connection_id=route.connection_id
                    )
                    op.remote_response = {**op.remote_response, **evidence}
                    op.status, dist.status = "succeeded", "ready"
                else:
                    op.status, dist.status = "verifying", "verifying"
                    if not discover and error_code is None:
                        # 与单项详情共用负证据规则；网络/准入错误不推翻已有正回执。
                        single._invalidate_mapping(
                            db,
                            context,
                            dist,
                            {
                                "video_id": item["video_id"],
                                "target_route": item["route"],
                            },
                        )
                    if discover and candidate_identity and record is not None:
                        # 仅缺可用状态/尺寸时保留真实 VID，后续详情读取必须继续核对尺寸。
                        op.remote_response = {
                            **op.remote_response,
                            "video_id": record.video_id,
                            "batch_discovered": not name_fallback or item["strict"],
                        }
                    elif discover and ambiguous:
                        op.remote_response = {
                            **op.remote_response,
                            "batch_discovery_incomplete": True,
                        }
            except DomainError as error:
                code = error.code
                op.status, dist.status = "result_unknown", "blocked"
            progress = dist.status == "ready" or (
                discover and candidate_identity and not ambiguous and code is None
            )
            if code:
                op.remote_response = {**op.remote_response, "error_code": code}
            if not admission_deferred:
                single._reconciliation_progress(op, progress=progress)
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
                    due=datetime.now(UTC) + timedelta(seconds=0 if progress else 60),
                    read_only=read_only,
                )
    return True
