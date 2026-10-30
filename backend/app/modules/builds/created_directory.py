"""创建事实直接进入目录；不调度同步、不发布报表或账户覆盖水位。"""

from copy import deepcopy
from typing import cast

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.integrations.tiktok.adapters.ads_read import material_usages
from app.integrations.tiktok.contracts.ads import EntityKind, EntityRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.ads.directory import append_campaign_name_projection
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import _next_directory_version
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.models import MaterialFile


def publish_created_object(
    session: Session, *, step: ExecutionStep, route: FrozenTikTokRoute
) -> None:
    """只补首次确认创建的身份，重复/晚到回执不能覆盖已同步的对象或素材。"""
    if (
        step.kind not in {"CAMPAIGN", "ADGROUP", "AD"}
        or step.status != "SUCCEEDED"
        or step.mismatch
        or not step.remote_id
        or not step.request_body
    ):
        return
    unit = session.get(BuildUnit, step.unit_id)
    if (
        unit is None
        or unit.tenant_id != step.tenant_id
        or unit.bc_id != step.bc_id
        or unit.preview_id != step.preview_id
        or route.tenant_id != step.tenant_id
        or route.bc_id != step.bc_id
        or route.connection_id != unit.connection_id
        or step.request_body.get("advertiser_id") != unit.advertiser_id
    ):
        return
    kind = cast(EntityKind, step.kind.lower())
    body = deepcopy(step.request_body)
    # 请求 ENABLE 不等于平台已确认 ENABLE；未知状态必须留空。
    body.pop("operation_status", None)
    if step.operation_status is not None:
        body["operation_status"] = step.operation_status
    body["smart_plus_ad_id" if kind == "ad" else f"{kind}_id"] = step.remote_id
    parent_kind = {"adgroup": "campaign", "ad": "adgroup"}.get(kind)
    parent_id = body.get(f"{parent_kind}_id") if parent_kind else None
    if parent_kind and not parent_id:
        return
    version = _next_directory_version(session)
    inserted = (
        session.connection()
        .execute(
            insert(AdObject)
            .values(
                tenant_id=step.tenant_id,
                advertiser_id=unit.advertiser_id,
                kind=kind,
                remote_id=step.remote_id,
                parent_kind=parent_kind,
                parent_remote_id=parent_id,
                ad_type="SMART_PLUS",
                name=body.get(f"{kind}_name", ""),
                configuration=body,
                operation_status=step.operation_status,
                review_status=step.review_status,
                delivery_status=None,
                observed_at=step.checked_at or step.updated_at,
                published_version=version,
                source_connection_id=route.connection_id,
                source_channel=route.channel,
            )
            .on_conflict_do_nothing()
            .returning(col(AdObject.remote_id))
        )
        .scalar_one_or_none()
    )
    if inserted is None:
        return
    ref = EntityRef(step.tenant_id, unit.advertiser_id, kind, step.remote_id)
    if kind == "campaign":
        append_campaign_name_projection(
            session,
            campaign_ref=ref,
            raw_name=body.get("campaign_name", ""),
            parser_revision=1,
        )
    elif kind == "ad":
        uses, _ = material_usages(body, ref, smart=True)
        mappings = {}
        for dependency, material in session.exec(
            select(ExecutionStep, MaterialFile)
            .join(
                MaterialFile,
                (col(MaterialFile.tenant_id) == ExecutionStep.tenant_id)
                & (col(MaterialFile.id) == ExecutionStep.material_id),
            )
            .where(
                ExecutionStep.tenant_id == step.tenant_id,
                ExecutionStep.submission_id == step.submission_id,
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind == "MATERIAL",
                ExecutionStep.status == "SUCCEEDED",
            )
        ).all():
            mapping = dependency.resolved.get("mapping", {})
            if (
                mapping.get("advertiser_id") == unit.advertiser_id
                and mapping.get("connection_id") == str(route.connection_id)
                and mapping.get("bc_id") == step.bc_id
            ):
                asset_id = mapping.get(
                    "image_id" if material.media_type == "IMAGE" else "video_id"
                )
                mappings.setdefault((material.media_type, asset_id), set()).add(
                    (material.id, material.file_name)
                )
        # 创建回执不提供广告内素材/创意ID；保留真实VID/图片ID，不能伪造独立启停身份。
        values = []
        for use in uses:
            matches = mappings.get(
                (use.use_ref.material_type, use.use_ref.platform_material_id), set()
            )
            local_id, name = (
                next(iter(matches)) if len(matches) == 1 else (None, use.name)
            )
            values.append(
                {
                    "tenant_id": step.tenant_id,
                    "advertiser_id": unit.advertiser_id,
                    "ad_remote_id": step.remote_id,
                    "platform_material_id": use.use_ref.platform_material_id,
                    "ad_material_id": use.use_ref.ad_material_id,
                    "material_type": use.use_ref.material_type,
                    "name": name,
                    "local_material_id": local_id,
                    "creative_ids": list(use.creative_ids),
                    "operation_status": use.operation_status,
                    "complete": use.complete,
                    "published_version": version,
                }
            )
        if values:
            # 同一广告的素材一次写入，避免每条素材各增加一次数据库往返。
            session.connection().execute(
                insert(AdMaterialReference).values(values).on_conflict_do_nothing()
            )
