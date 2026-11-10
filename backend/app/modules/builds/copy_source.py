"""复制来源：当前目录决定设置，历史搭建仅证明推广链路与素材的归属。"""

import json
from hashlib import sha256

from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.ads.models import AdObject
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.providers.models import (
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
)
from app.modules.tenants.permissions import require_tenant

from .copy_compile import group_setting, observed_body
from .execution_models import ExecutionStep
from .mini_targets import link_url
from .models import BuildDraft
from .preview_models import BuildPreview, BuildUnit, PlannedAd, PreviewDrama


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _provenance(session, context, bc_id, advertiser_id, remote_id, kind):
    rows = session.exec(
        select(ExecutionStep)
        .join(BuildUnit, col(BuildUnit.id) == ExecutionStep.unit_id)
        .where(
            ExecutionStep.tenant_id == context.tenant_id,
            ExecutionStep.bc_id == bc_id,
            BuildUnit.tenant_id == context.tenant_id,
            BuildUnit.advertiser_id == advertiser_id,
            ExecutionStep.kind == kind,
            ExecutionStep.remote_id == remote_id,
            ExecutionStep.status == "SUCCEEDED",
            col(ExecutionStep.mismatch).is_(False),
        )
        .order_by(col(ExecutionStep.updated_at).desc())
        .limit(2)
    ).all()
    # 续建可能保留同一远端对象，只有完全相同的冻结来源才可消除重复。
    if (
        not rows
        or len({(row.preview_id, row.unit_id, row.planned_ad_id) for row in rows}) != 1
    ):
        raise DomainError(
            "copy_provenance_missing", "来源缺少唯一可核实的搭建关联，暂不能复制"
        )
    return rows[0]


def load_copy_source(
    session: Session, *, context, bc_id, advertiser_id, campaign_id
) -> dict:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    access = resolve_account_access(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        action="read",
    )
    route = freeze_route(
        session, context=context, bc_id=bc_id, connection_id=access.connection_id
    )
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=advertiser_id,
        capability="read",
    )
    root = session.get(
        AdObject,
        (context.tenant_id, advertiser_id, "campaign", campaign_id),
        populate_existing=True,
    )
    if root is None:
        raise DomainError("copy_source_missing", "来源系列不在当前租户和账户目录中")
    result = {
        "name": root.name,
        "advertiser_id": advertiser_id,
        "campaign_id": campaign_id,
        "currency": access.currency,
        "provider_name": None,
        "application_name": None,
        "minis_id": None,
        "budget": None,
        "target_roas": None,
        "budget_strategy": "SERIES",
        "bid_strategy": "HIGHEST_VALUE",
        "materials": [],
        "targeting": None,
        "targeting_mixed": False,
        "group_settings": [],
        "blockers": [],
        "snapshot": {},
    }
    try:
        if root.ad_type != "SMART_PLUS":
            raise DomainError(
                "copy_type_unsupported",
                "当前仅支持升级 Smart+ 小程序视频系列，普通和旧 Smart+ 暂不支持",
            )
        campaign = observed_body("CAMPAIGN", root.configuration)
        if (
            campaign["advertiser_id"] != advertiser_id
            or root.configuration.get("campaign_id") != campaign_id
            or root.parent_kind is not None
        ):
            raise DomainError("copy_parent_mismatch", "来源系列身份不一致")
        source_step = _provenance(
            session, context, bc_id, advertiser_id, campaign_id, "CAMPAIGN"
        )
        unit = session.get(BuildUnit, source_step.unit_id)
        preview = session.get(BuildPreview, source_step.preview_id)
        draft = session.get(BuildDraft, preview.draft_id)
        drama = session.get(
            PreviewDrama, (context.tenant_id, preview.id, unit.drama_id)
        )
        if not drama or not draft or draft.tenant_id != context.tenant_id:
            raise DomainError("copy_provenance_missing", "来源推广剧目关联不完整")
        # 只查公开名称列，不读取或序列化版权方凭据和应用私有配置。
        provider_name = session.exec(
            select(ProviderConnection.display_name).where(
                ProviderConnection.tenant_id == context.tenant_id,
                ProviderConnection.id == draft.provider_connection_id,
            )
        ).one_or_none()
        application_name = session.exec(
            select(ProviderApplication.name).where(
                ProviderApplication.tenant_id == context.tenant_id,
                ProviderApplication.connection_id == draft.provider_connection_id,
                ProviderApplication.external_id == draft.application_id,
            )
        ).one_or_none()
        result["provider_name"] = provider_name or str(draft.provider_connection_id)
        result["application_name"] = application_name or draft.application_id
        link = session.get(PromotionLink, drama.link_id)
        if (
            not link
            or link.tenant_id != context.tenant_id
            or link_url(link) != drama.url
        ):
            raise DomainError("copy_link_changed", "来源推广链接与当前版权方关联不一致")
        groups = session.exec(
            select(AdObject)
            .where(
                AdObject.tenant_id == context.tenant_id,
                AdObject.advertiser_id == advertiser_id,
                AdObject.kind == "adgroup",
                AdObject.parent_kind == "campaign",
                AdObject.parent_remote_id == campaign_id,
            )
            .order_by(col(AdObject.remote_id))
            .limit(101)
        ).all()
        if not 1 <= len(groups) <= 100:
            raise DomainError(
                "copy_structure_invalid", "来源广告组为空或超过单系列100组复制上限"
            )
        frozen_groups, materials, identities, urls, portfolios = (
            [],
            {},
            set(),
            set(),
            set(),
        )
        ad_count = 0
        for group in groups:
            if group.ad_type != "SMART_PLUS":
                raise DomainError("copy_type_unsupported", "来源子对象广告类型不一致")
            body = observed_body("ADGROUP", group.configuration)
            if (
                body["campaign_id"] != campaign_id
                or body["advertiser_id"] != advertiser_id
            ):
                raise DomainError("copy_parent_mismatch", "来源广告组父身份不一致")
            ads = session.exec(
                select(AdObject)
                .where(
                    AdObject.tenant_id == context.tenant_id,
                    AdObject.advertiser_id == advertiser_id,
                    AdObject.kind == "ad",
                    AdObject.parent_kind == "adgroup",
                    AdObject.parent_remote_id == group.remote_id,
                )
                .order_by(col(AdObject.remote_id))
                .limit(1001)
            ).all()
            if not 1 <= len(ads) <= 1000:
                raise DomainError(
                    "copy_structure_invalid", "来源广告为空或超过单组1000广告复制上限"
                )
            ad_count += len(ads)
            if ad_count > 1000:
                raise DomainError(
                    "copy_structure_invalid", "单系列广告总数超过1000复制上限"
                )
            copied_ads = []
            for ad in ads:
                if ad.ad_type != "SMART_PLUS":
                    raise DomainError("copy_type_unsupported", "来源广告类型不一致")
                ad_body = observed_body("AD", ad.configuration)
                if (
                    ad_body["adgroup_id"] != group.remote_id
                    or ad_body["advertiser_id"] != advertiser_id
                ):
                    raise DomainError("copy_parent_mismatch", "来源广告父身份不一致")
                original = _provenance(
                    session, context, bc_id, advertiser_id, ad.remote_id, "AD"
                )
                planned = session.get(
                    PlannedAd,
                    (context.tenant_id, original.preview_id, original.planned_ad_id),
                )
                if planned is None or planned.tenant_id != context.tenant_id:
                    raise DomainError("copy_provenance_missing", "来源广告缺少文案关联")
                mids = []
                for entry in ad_body["creative_list"]:
                    info = entry["creative_info"]
                    if info.get("ad_format") != "SINGLE_VIDEO":
                        raise DomainError(
                            "copy_media_unsupported", "轮播与帖子素材暂不能复制"
                        )
                    identities.add(
                        json.dumps(
                            {
                                key: info[key]
                                for key in (
                                    "identity_id",
                                    "identity_type",
                                    "identity_authorized_bc_id",
                                )
                                if key in info
                            },
                            sort_keys=True,
                        )
                    )
                    video_id = info["video_info"]["video_id"]
                    matches = session.exec(
                        select(MaterialFile, AccountMaterial)
                        .join(
                            AccountMaterial,
                            (col(AccountMaterial.material_id) == MaterialFile.id)
                            & (
                                col(AccountMaterial.tenant_id) == MaterialFile.tenant_id
                            ),
                        )
                        .where(
                            MaterialFile.tenant_id == context.tenant_id,
                            AccountMaterial.bc_id == bc_id,
                            AccountMaterial.advertiser_id == advertiser_id,
                            AccountMaterial.video_id == video_id,
                            AccountMaterial.status == "available",
                            col(AccountMaterial.verified_at).is_not(None),
                        )
                        .limit(2)
                    ).all()
                    if len(matches) != 1 or matches[0][0].media_type != "VIDEO":
                        raise DomainError(
                            "copy_material_unmapped",
                            "来源视频缺少唯一已核实的本地素材映射，请先导入素材",
                        )
                    material, asset = matches[0]
                    images = info.get("image_info", [])
                    if (
                        len(images) != 1
                        or not asset.image_id
                        or images[0]["web_uri"] != asset.image_id
                    ):
                        raise DomainError(
                            "copy_cover_unverified",
                            "来源当前封面与可信素材封面映射不一致，不能静默替换定制封面",
                        )
                    mid = str(material.id)
                    if mid in mids:
                        raise DomainError(
                            "copy_material_duplicate",
                            "来源广告重复使用同一素材，暂不能无损复制",
                        )
                    mids.append(mid)
                    materials[mid] = {
                        "material_id": mid,
                        "file_name": material.file_name,
                        "media_type": material.media_type,
                    }
                urls.add(ad_body["landing_page_url_list"][0]["landing_page_url"])
                portfolios.add(ad_body["ad_configuration"]["call_to_action_id"])
                copied_ads.append(
                    {
                        "remote_id": ad.remote_id,
                        "body": ad_body,
                        "copy_id": str(planned.copy_id),
                        "material_ids": mids,
                    }
                )
            frozen_groups.append(
                {"remote_id": group.remote_id, "body": body, "ads": copied_ads}
            )
        if len(identities) != 1 or len(urls) != 1 or len(portfolios) != 1:
            raise DomainError(
                "copy_ad_variants_unsupported",
                "来源包含不同投放身份、推广链接或CTA组合，暂不能无损复制",
            )
        if next(iter(urls)) != drama.url:
            raise DomainError(
                "copy_link_changed",
                "来源当前推广链接与已核实剧目关联不一致，请先重新核验链接",
            )
        cta = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.bc_id == bc_id,
                ExecutionStep.unit_id == unit.id,
                ExecutionStep.kind == "CTA",
                ExecutionStep.status == "SUCCEEDED",
                ExecutionStep.remote_id == next(iter(portfolios)),
            )
            .limit(2)
        ).all()
        if (
            len(cta) != 1
            or not cta[0].request_body
            or not cta[0].request_body.get("portfolio_content")
        ):
            raise DomainError(
                "copy_cta_unverified", "来源当前CTA组合缺少可核实的资产关联"
            )
        budgets = {group["body"].get("budget") for group in frozen_groups}
        bids = {group["body"].get("roas_bid") for group in frozen_groups}
        budget_strategy = (
            "ADGROUP" if campaign["budget_mode"] == "BUDGET_MODE_INFINITE" else "SERIES"
        )
        if any(
            (group["body"].get("budget") is not None) != (budget_strategy == "ADGROUP")
            for group in frozen_groups
        ):
            raise DomainError(
                "copy_budget_inconsistent", "来源系列与广告组预算归属不一致"
            )
        if len({g["body"]["minis_id"] for g in frozen_groups}) != 1:
            raise DomainError("copy_minis_inconsistent", "来源组推广小程序不一致")
        result["minis_id"] = frozen_groups[0]["body"]["minis_id"]
        if len({g["body"]["deep_bid_type"] for g in frozen_groups}) != 1:
            raise DomainError(
                "copy_bid_mixed", "来源广告组使用不同竞价策略，暂不支持混合策略复制"
            )
        result.update(
            budget_strategy=budget_strategy,
            budget=campaign["budget"]
            if budget_strategy == "SERIES"
            else next(iter(budgets))
            if len(budgets) == 1
            else None,
            target_roas=next(iter(bids)) if len(bids) == 1 else None,
            bid_strategy="TARGET_ROAS"
            if any(b is not None for b in bids)
            else "HIGHEST_VALUE",
            materials=list(materials.values()),
        )
        settings = [
            group_setting(
                group["body"],
                unit.scene_snapshot.get("field_constraints", {}).get(
                    "target_regions", []
                ),
            )
            for group in frozen_groups
        ]
        choices = {
            json.dumps(setting["targeting"], sort_keys=True) for setting in settings
        }
        result["group_settings"] = settings
        result["targeting_mixed"] = len(choices) != 1 or any(
            setting["targeting"] is None for setting in settings
        )
        result["targeting"] = (
            settings[0]["targeting"] if not result["targeting_mixed"] else None
        )
        if any(setting["targeting"] is None for setting in settings):
            raise DomainError(
                "copy_targeting_unavailable",
                "来源实际地区ID缺少已核实的国家码映射，暂不能完整展示复制定向",
            )
        result["snapshot"] = {
            "version": 1,
            "tenant_id": str(context.tenant_id),
            "bc_id": bc_id,
            "source_route": route.model_dump(mode="json"),
            "campaign": campaign,
            "groups": frozen_groups,
            "identity": json.loads(next(iter(identities))),
            "cta_contents": cta[0].request_body["portfolio_content"],
            "strategy_version_id": str(preview.strategy_version_id),
            "provider_connection_id": str(draft.provider_connection_id),
            "application_id": draft.application_id,
            "drama_id": str(drama.drama_id),
            "link_id": str(drama.link_id),
            "title": drama.title,
            "url": drama.url,
            "protected_base": drama.protected_base,
            "provider_pinyin": drama.provider_pinyin,
            "external_drama_id": drama.external_drama_id,
            "display_drama_id": drama.display_drama_id,
            # 正常目录刷新会推进观察时间/发布版本；只有规范化业务设置、
            # 层级和已核实的素材/文案绑定改变，才使复制来源失效。
            "source_digest": digest(
                {
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                    "campaign": campaign,
                    "groups": frozen_groups,
                }
            ),
        }
    except DomainError as error:
        result["blockers"] = [error.message]
    return result
