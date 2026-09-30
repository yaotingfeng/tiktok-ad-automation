"""广告目录分页暂存与发布。

采集和发布严格分成两个事务：采集器只写冻结运行的页，发布器在确认页链完整、
当前 claim 仍然有效并重新核验冻结路由后，才把远端观察投影到目录表。这样网络
请求期间连接被撤销时，旧 worker 不能继续制造可见版本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import DirectoryPage, EntityRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.models import TenantBC
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory import append_campaign_name_projection
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting.sync_models import SyncSchedule


def _ref_json(ref: EntityRef | None) -> dict[str, str] | None:
    if ref is None:
        return None
    return {
        "tenant_id": str(ref.tenant_id),
        "advertiser_id": ref.advertiser_id,
        "kind": ref.kind,
        "remote_id": ref.remote_id,
    }


def _json_value(value: Any) -> Any:
    """递归规范化配置中的 UUID、时间和定点数，避免把 psycopg 当序列化器。"""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _entity_json(entity: Any) -> dict[str, Any]:
    """将 A3 frozen DTO 转为 JSONB；UUID 和 datetime 不能直接交给 psycopg。"""
    return {
        "ref": _ref_json(entity.ref),
        "parent_ref": _ref_json(entity.parent_ref),
        "ad_type": entity.ad_type,
        "name": entity.name,
        "configuration": _json_value(entity.configuration),
        "operation_status": entity.operation_status,
        "review_status": entity.review_status,
        "delivery_status": entity.delivery_status,
        "observed_at": entity.observed_at.isoformat(),
    }


def _material_json(material: Any) -> dict[str, Any]:
    use = material.use_ref
    return {
        "use_ref": {
            "ad_ref": _ref_json(use.ad_ref),
            "platform_material_id": use.platform_material_id,
            "ad_material_id": use.ad_material_id,
            "material_type": use.material_type,
        },
        "local_material_id": (
            str(material.local_material_id) if material.local_material_id else None
        ),
        "operation_status": material.operation_status,
        "complete": material.complete,
        "name": material.name,
        "main_material_id": material.main_material_id,
        "main_material_type": material.main_material_type,
        "creative_ids": list(material.creative_ids),
    }


def _evidence_json(page: DirectoryPage) -> dict[str, Any]:
    return asdict(page.evidence)


def _domain(code: str, message: str) -> DomainError:
    return DomainError(code, message)


def _page_payload(
    page: DirectoryPage,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [_entity_json(item) for item in page.items],
        [_material_json(item) for item in page.materials],
    )


def _same_staged_page(
    row: AdDirectoryPage,
    *,
    page: DirectoryPage,
    claim_generation: int,
    require_claim: bool = True,
) -> bool:
    items, materials = _page_payload(page)
    coverage = _coverage(page)
    return (
        (not require_claim or row.claim_generation == claim_generation)
        and row.next_page == page.next_page
        and row.complete == page.complete
        and row.items == items
        and row.materials == materials
        and row.evidence == _evidence_json(page)
        and row.coverage == coverage
        and row.missing_reason == page.material_missing_reason
    )


def _coverage(page: DirectoryPage) -> str:
    if not page.complete:
        return "PARTIAL_MATERIALS" if not page.materials_complete else "PARTIAL"
    return "COMPLETE_MATERIALS" if page.materials_complete else "MATERIALS_INCOMPLETE"


def stage_directory_page(
    session: Session,
    *,
    run_id: UUID,
    page: DirectoryPage,
    claim_generation: int,
) -> None:
    """暂存一页；相同页重试幂等，冲突内容永远不能覆盖证据。"""
    if type(claim_generation) is not int or claim_generation < 1:
        raise _domain("directory_claim_invalid", "目录 claim 代数无效")
    # 先锁运行行，再检查代数并写页；claim 替换无法插入旧代数的页。
    run = session.exec(
        select(AdDirectoryRun)
        .where(AdDirectoryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None:
        raise _domain("directory_run_not_found", "目录同步运行不存在")
    if run.published_version is not None or run.status in {
        "COMPLETE",
        "CANCELLED",
        "STALE",
    }:
        raise _domain("directory_run_closed", "目录同步运行已经结束")
    if page.page < 1:
        raise _domain("directory_page_invalid", "目录页码无效")
    if claim_generation > run.claim_generation:
        raise _domain("directory_claim_lost", "目录同步 claim 已被替换")
    if claim_generation < run.claim_generation:
        existing = session.get(
            AdDirectoryPage, (run_id, page.page), populate_existing=True
        )
        if existing is None or not _same_staged_page(
            existing, page=page, claim_generation=claim_generation
        ):
            raise _domain("directory_claim_lost", "旧目录 worker 不能追加暂存页")
        return

    for item in page.items:
        if (
            item.ref.tenant_id != run.tenant_id
            or item.ref.advertiser_id != run.advertiser_id
        ):
            raise _domain("directory_scope_mismatch", "目录对象不属于冻结账户")
        if item.ref.kind != run.kind and not (
            run.kind == "ad"
            and run.ad_type == "SMART_PLUS"
            and item.ref.kind == "creative"
            and item.parent_ref is not None
            and item.parent_ref.kind == "ad"
        ):
            raise _domain("directory_kind_mismatch", "目录对象类型不属于本次分区")
    for usage in page.materials:
        if (
            usage.use_ref.ad_ref.tenant_id != run.tenant_id
            or usage.use_ref.ad_ref.advertiser_id != run.advertiser_id
        ):
            raise _domain("directory_scope_mismatch", "素材使用不属于冻结账户")

    items, materials = _page_payload(page)
    existing = session.get(AdDirectoryPage, (run_id, page.page), populate_existing=True)
    if existing is not None:
        # 页证据是不可变事实；新的 claim 可以继续使用旧代数已经接受的相同页。
        if _same_staged_page(
            existing, page=page, claim_generation=claim_generation, require_claim=False
        ):
            return
        raise _domain("directory_page_conflict", "同一目录页已有不同暂存证据")
    terminal = session.exec(
        select(AdDirectoryPage)
        .where(AdDirectoryPage.run_id == run_id, AdDirectoryPage.complete.is_(True))
        .with_for_update()
    ).first()
    if terminal is not None:
        raise _domain("directory_run_terminal", "目录运行已暂存末页")

    session.add(
        AdDirectoryPage(
            run_id=run.id,
            page=page.page,
            tenant_id=run.tenant_id,
            advertiser_id=run.advertiser_id,
            claim_generation=claim_generation,
            next_page=page.next_page,
            complete=page.complete,
            evidence=_evidence_json(page),
            coverage=_coverage(page),
            missing_reason=page.material_missing_reason,
            items=items,
            materials=materials,
        )
    )
    session.flush()


def _parse_ref(value: Any) -> tuple[UUID, str, str, str] | None:
    if not isinstance(value, dict):
        return None
    try:
        tenant_id = UUID(str(value["tenant_id"]))
        advertiser_id = str(value["advertiser_id"])
        kind = str(value["kind"])
        remote_id = str(value["remote_id"])
    except KeyError, TypeError, ValueError:
        return None
    if (
        not advertiser_id
        or not remote_id
        or kind not in {"campaign", "adgroup", "ad", "creative"}
    ):
        return None
    return tenant_id, advertiser_id, kind, remote_id


def _observed(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return fallback


def _route_for_run(run: AdDirectoryRun) -> FrozenTikTokRoute:
    try:
        route = FrozenTikTokRoute.model_validate(run.frozen_route)
    except (TypeError, ValueError) as exc:
        raise _domain("frozen_route_changed", "目录运行的冻结路由无效") from exc
    if (
        route.tenant_id != run.tenant_id
        or route.bc_id != run.bc_id
        or route.connection_id != run.connection_id
        or route.channel != run.channel
    ):
        raise _domain("frozen_route_scope_mismatch", "目录运行与冻结路由范围不一致")
    return route


def _ensure_route_authority(
    session: Session, run: AdDirectoryRun, route: FrozenTikTokRoute
) -> None:
    # 后台发布没有 HTTP context，仍以运行中冻结的 actor 重建当前租户权限。
    verify_route(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        route=route,
        advertiser_id=run.advertiser_id,
        capability="read",
    )


def _complete_chain(pages: list[AdDirectoryPage]) -> list[AdDirectoryPage]:
    by_number = {row.page: row for row in pages}
    if not by_number or 1 not in by_number:
        raise _domain("directory_incomplete", "目录暂存缺少第一页")
    ordered: list[AdDirectoryPage] = []
    page_no = 1
    seen: set[int] = set()
    while True:
        row = by_number.get(page_no)
        if row is None or row.page in seen:
            raise _domain("directory_incomplete", "目录分页存在缺页")
        seen.add(row.page)
        ordered.append(row)
        if row.complete:
            if row.next_page is not None:
                raise _domain("directory_incomplete", "完整页不能继续分页")
            break
        if row.next_page is None:
            raise _domain("directory_incomplete", "目录中间页没有后续页")
        page_no = row.next_page
    if len(seen) != len(by_number):
        raise _domain("directory_incomplete", "目录暂存包含断开的页")
    return ordered


def _existing_object(
    session: Session, ref: tuple[UUID, str, str, str]
) -> AdObject | None:
    return session.get(AdObject, ref, populate_existing=True)


def _upsert_entity(
    session: Session,
    data: dict[str, Any],
    *,
    run: AdDirectoryRun,
    version: int,
    fallback_observed: datetime,
) -> AdObject | None:
    ref = _parse_ref(data.get("ref"))
    if ref is None:
        raise _domain("directory_page_invalid", "暂存对象身份无效")
    tenant_id, advertiser_id, kind, remote_id = ref
    if tenant_id != run.tenant_id or advertiser_id != run.advertiser_id:
        raise _domain("directory_scope_mismatch", "暂存对象超出冻结账户")
    parent = _parse_ref(data.get("parent_ref"))
    if parent is not None and parent[:2] != ref[:2]:
        raise _domain("directory_scope_mismatch", "暂存父对象超出冻结账户")
    observed = _observed(data.get("observed_at"), fallback_observed)
    existing = _existing_object(session, ref)
    if existing is not None and existing.observed_at > observed:
        # full 与 targeted 分区可能重叠；旧观察不得覆盖较新的对象事实。
        return existing
    values = {
        "parent_kind": parent[2] if parent else None,
        "parent_remote_id": parent[3] if parent else None,
        "ad_type": str(data.get("ad_type") or run.ad_type),
        "name": str(data.get("name") or ""),
        "configuration": data.get("configuration")
        if isinstance(data.get("configuration"), dict)
        else {},
        "operation_status": data.get("operation_status"),
        "review_status": data.get("review_status"),
        "delivery_status": data.get("delivery_status"),
        "observed_at": observed,
        "published_version": version,
        "source_connection_id": run.connection_id,
        "source_channel": run.channel,
    }
    if existing is None:
        existing = AdObject(
            tenant_id=tenant_id,
            advertiser_id=advertiser_id,
            kind=kind,
            remote_id=remote_id,
            **values,
        )
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    session.add(existing)
    return existing


def _upsert_material(
    session: Session, data: dict[str, Any], *, run: AdDirectoryRun, version: int
) -> None:
    use = data.get("use_ref")
    ad_ref = _parse_ref(use.get("ad_ref")) if isinstance(use, dict) else None
    if (
        ad_ref is None
        or ad_ref[0] != run.tenant_id
        or ad_ref[1] != run.advertiser_id
        or ad_ref[2] != "ad"
    ):
        raise _domain("directory_scope_mismatch", "暂存素材使用身份无效")
    platform_id = str(use.get("platform_material_id") or "")
    material_type = str(use.get("material_type") or "")
    inner_id = use.get("ad_material_id")
    stmt = select(AdMaterialReference).where(
        AdMaterialReference.tenant_id == run.tenant_id,
        AdMaterialReference.advertiser_id == run.advertiser_id,
        AdMaterialReference.ad_remote_id == ad_ref[3],
        AdMaterialReference.platform_material_id == platform_id,
        AdMaterialReference.material_type == material_type,
    )
    stmt = stmt.where(
        AdMaterialReference.ad_material_id.is_(None)
        if inner_id is None
        else AdMaterialReference.ad_material_id == inner_id
    )
    existing = session.exec(stmt.with_for_update()).first()
    complete = bool(data.get("complete"))
    if existing is not None and not complete:
        # 不完整素材响应不能清空已有平台素材或把本地关联改成猜测值。
        return
    try:
        local_material_id = (
            UUID(data["local_material_id"]) if data.get("local_material_id") else None
        )
    except (TypeError, ValueError) as exc:
        raise _domain("directory_page_invalid", "本地素材身份无效") from exc
    values = {
        "tenant_id": run.tenant_id,
        "advertiser_id": run.advertiser_id,
        "ad_remote_id": ad_ref[3],
        "platform_material_id": platform_id,
        "ad_material_id": inner_id,
        "material_type": material_type,
        "name": str(data.get("name") or ""),
        "main_material_id": data.get("main_material_id"),
        "main_material_type": data.get("main_material_type"),
        "creative_ids": list(data.get("creative_ids") or []),
        "local_material_id": local_material_id,
        "operation_status": data.get("operation_status"),
        "complete": complete,
        "published_version": version,
    }
    if existing is None:
        existing = AdMaterialReference(**values)
    else:
        for key, value in values.items():
            if key not in {
                "tenant_id",
                "advertiser_id",
                "ad_remote_id",
                "platform_material_id",
                "ad_material_id",
                "material_type",
            }:
                setattr(existing, key, value)
    session.add(existing)


def _schedule_key(
    route: FrozenTikTokRoute,
    *,
    actor_id: UUID,
    ref: tuple[UUID, str, str, str],
    ad_type: str,
) -> str:
    payload = {
        "route": route.model_dump(mode="json"),
        "actor_id": str(actor_id),
        "ref": {
            "tenant_id": str(ref[0]),
            "advertiser_id": ref[1],
            "kind": ref[2],
            "remote_id": ref[3],
        },
        "ad_type": ad_type,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _queue_missing_parent(
    session: Session,
    *,
    run: AdDirectoryRun,
    route: FrozenTikTokRoute,
    ref: tuple[UUID, str, str, str],
    ad_type: str,
) -> None:
    serialized = {
        "tenant_id": str(ref[0]),
        "advertiser_id": ref[1],
        "kind": ref[2],
        "remote_id": ref[3],
    }
    key = _schedule_key(route, actor_id=run.actor_id, ref=ref, ad_type=ad_type)
    row = session.exec(
        select(SyncSchedule)
        .where(
            SyncSchedule.tenant_id == run.tenant_id,
            SyncSchedule.bc_id == run.bc_id,
            SyncSchedule.advertiser_id == run.advertiser_id,
            SyncSchedule.scope == "targeted",
            SyncSchedule.schedule_key == key,
        )
        .with_for_update()
    ).first()
    target = {"ref": serialized, "ad_type": ad_type}
    if row is None:
        row = SyncSchedule(
            tenant_id=run.tenant_id,
            advertiser_id=run.advertiser_id,
            bc_id=run.bc_id,
            actor_id=run.actor_id,
            connection_id=run.connection_id,
            channel=run.channel,
            frozen_route=run.frozen_route,
            scope="targeted",
            schedule_key=key,
            next_due_at=datetime.now(UTC),
            refs=[serialized],
            requested_coverage={
                "mode": "once",
                "reason": "missing_parent",
                "directory_targets": [target],
            },
            enabled=True,
        )
    else:
        # 活跃任务合并目标时保留原 claim/截止时间，避免发布事务偷走 A7 worker 的租约。
        targets = (
            list(row.requested_coverage.get("directory_targets", []))
            if isinstance(row.requested_coverage, dict)
            else []
        )
        if target not in targets:
            targets.append(target)
        row.requested_coverage = {
            "mode": "once",
            "reason": "missing_parent",
            "directory_targets": targets,
        }
        if serialized not in row.refs:
            row.refs = [*row.refs, serialized]
        row.enabled = True
    session.add(row)


def publish_directory(session: Session, *, run_id: UUID, claim_generation: int) -> int:
    """验证完整分页后原子发布；返回本运行使用的单一 published_version。"""
    if type(claim_generation) is not int or claim_generation < 1:
        raise _domain("directory_claim_invalid", "目录 claim 代数无效")
    with session.begin_nested():
        run = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == run_id).with_for_update()
        ).one_or_none()
        if run is None:
            raise _domain("directory_run_not_found", "目录同步运行不存在")
        if run.published_version is not None:
            if run.claim_generation != claim_generation:
                raise _domain("directory_claim_lost", "目录同步 claim 已被替换")
            return run.published_version
        if run.claim_generation != claim_generation:
            raise _domain("directory_claim_lost", "目录同步 claim 已被替换")
        if run.request_sequence is not None:
            newer_published = session.exec(
                select(AdDirectoryRun.id)
                .where(
                    AdDirectoryRun.tenant_id == run.tenant_id,
                    AdDirectoryRun.advertiser_id == run.advertiser_id,
                    AdDirectoryRun.kind == run.kind,
                    AdDirectoryRun.ad_type == run.ad_type,
                    AdDirectoryRun.request_sequence > run.request_sequence,
                    AdDirectoryRun.published_version.is_not(None),
                )
                .limit(1)
            ).first()
            if newer_published is not None:
                raise _domain("directory_run_stale", "较早目录运行不能晚于新运行发布")
        route = _route_for_run(run)
        _ensure_route_authority(session, run, route)
        pages = session.exec(
            select(AdDirectoryPage)
            .where(AdDirectoryPage.run_id == run.id)
            .order_by(AdDirectoryPage.page)
            .with_for_update()
        ).all()
        ordered = _complete_chain(pages)
        # Tenant 行是现有模型中的稳定锁点；先串行化版本分配，再读取最大值，
        # 避免两个账户同步同时取得同一个 published_version。
        tenant = session.exec(
            select(TenantBC)
            .where(TenantBC.tenant_id == run.tenant_id, TenantBC.bc_id == run.bc_id)
            .with_for_update()
        ).one_or_none()
        if tenant is None:
            raise _domain("account_not_in_bc", "目录运行的 BC 不存在")
        # PostgreSQL greatest/NULL 的行为会随空表变化；分别读取更清晰，也兼容测试数据库。
        object_max = (
            session.exec(select(func.max(AdObject.published_version))).one() or 0
        )
        material_max = (
            session.exec(select(func.max(AdMaterialReference.published_version))).one()
            or 0
        )
        version = max(object_max, material_max, 0) + 1
        fallback = datetime.now(UTC)
        entities: list[dict[str, Any]] = []
        materials: list[dict[str, Any]] = []
        for page in ordered:
            entities.extend(page.items)
            materials.extend(page.materials)
        published_objects: list[AdObject] = []
        for data in entities:
            obj = _upsert_entity(
                session, data, run=run, version=version, fallback_observed=fallback
            )
            if obj is not None:
                published_objects.append(obj)
        session.flush()
        for data in materials:
            _upsert_material(session, data, run=run, version=version)
        session.flush()
        for obj in published_objects:
            if obj.parent_kind is None or obj.parent_remote_id is None:
                continue
            parent = session.get(
                AdObject,
                (
                    run.tenant_id,
                    run.advertiser_id,
                    obj.parent_kind,
                    obj.parent_remote_id,
                ),
                populate_existing=True,
            )
            if parent is None:
                _queue_missing_parent(
                    session,
                    run=run,
                    route=route,
                    ref=(
                        run.tenant_id,
                        run.advertiser_id,
                        obj.parent_kind,
                        obj.parent_remote_id,
                    ),
                    ad_type=obj.ad_type,
                )
        for obj in published_objects:
            if obj.kind == "campaign":
                append_campaign_name_projection(
                    session,
                    campaign_ref=obj.ref,
                    raw_name=obj.name,
                    parser_revision=1,
                )
        material_incomplete = any(
            page.coverage in {"PARTIAL_MATERIALS", "MATERIALS_INCOMPLETE"}
            for page in ordered
        )
        run.status = "COMPLETE"
        # 分页已经完整不代表素材子查询完整；运行摘要保留这两个覆盖维度。
        run.coverage = "MATERIALS_INCOMPLETE" if material_incomplete else "COMPLETE"
        run.observed_at = fallback
        run.published_version = version
        run.completed_at = fallback
        run.missing_reason = next(
            (
                page.missing_reason
                for page in ordered
                if page.missing_reason is not None
            ),
            None,
        )
        session.add(run)
        session.flush()
        for page in ordered:
            page.published_version = version
            session.add(page)
        session.flush()
    return version
