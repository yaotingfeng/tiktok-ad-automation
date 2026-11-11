"""当前授权目录是复制来源事实；不要求本系统的历史搭建或版权方关联。"""

import json
from hashlib import sha256
from uuid import uuid5

from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.ads.models import AdObject
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.tenants.permissions import require_tenant

from .copy_compile import group_setting, observed_body


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def fingerprint(source):
    """来源有效性只看远端设置及冻结权限，不纳入地区展示和本地素材别名。"""
    snapshot = source.get("snapshot") or {}
    if not snapshot:
        return digest(
            {
                key: source.get(key)
                for key in ("advertiser_id", "campaign_id", "blockers")
            }
        )
    return digest(
        {
            key: snapshot[key]
            for key in ("tenant_id", "bc_id", "source_route", "source_digest")
        }
    )


def _regions(session, context, bc_id, advertiser_id):
    from .scene_job_models import SceneJob

    rows = session.exec(
        select(SceneJob)
        .where(
            SceneJob.tenant_id == context.tenant_id,
            SceneJob.bc_id == bc_id,
            SceneJob.advertiser_id == advertiser_id,
            SceneJob.status == "COMPLETE",
        )
        .order_by(col(SceneJob.created_at).desc())
        .limit(20)
    ).all()
    for row in rows:
        locations = row.facts.get("regions", {}).get("locations", [])
        if locations:
            return locations
    return []


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
                    images = info.get("image_info", [])
                    if len(images) != 1:
                        raise DomainError(
                            "copy_cover_unavailable",
                            "来源视频缺少当前封面，请刷新广告目录",
                        )
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
                            AccountMaterial.image_id == images[0]["web_uri"],
                            AccountMaterial.status == "available",
                            col(AccountMaterial.verified_at).is_not(None),
                        )
                        .order_by(col(MaterialFile.id))
                        .limit(1)
                    ).all()
                    material = matches[0][0] if matches else None
                    if material is not None and material.media_type != "VIDEO":
                        raise DomainError(
                            "copy_material_invalid", "来源视频的素材类型不一致"
                        )
                    mid = (
                        str(material.id)
                        if material
                        else str(
                            uuid5(
                                context.tenant_id,
                                f"copy-video:{bc_id}:{advertiser_id}:{video_id}:{images[0]['web_uri']}",
                            )
                        )
                    )
                    images = info.get("image_info", [])
                    if len(images) != 1:
                        raise DomainError(
                            "copy_cover_unavailable",
                            "来源视频缺少当前封面，请刷新广告目录",
                        )
                    if mid in mids:
                        raise DomainError(
                            "copy_material_duplicate",
                            "来源广告重复使用同一素材，暂不能无损复制",
                        )
                    mids.append(mid)
                    materials[mid] = {
                        "material_id": mid,
                        "file_name": material.file_name
                        if material
                        else info["video_info"].get("file_name", video_id),
                        "media_type": "VIDEO",
                        "video_id": video_id,
                        "image_id": images[0]["web_uri"],
                    }
                urls.add(ad_body["landing_page_url_list"][0]["landing_page_url"])
                portfolios.add(ad_body["ad_configuration"]["call_to_action_id"])
                copied_ads.append(
                    {
                        "remote_id": ad.remote_id,
                        "body": ad_body,
                        "copy_id": None,
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
        regions = _regions(session, context, bc_id, advertiser_id)
        settings = [
            group_setting(
                group["body"],
                regions,
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
        result["snapshot"] = {
            "version": 2,
            "tenant_id": str(context.tenant_id),
            "bc_id": bc_id,
            "source_route": route.model_dump(mode="json"),
            "campaign": campaign,
            "groups": frozen_groups,
            "identity": json.loads(next(iter(identities))),
            "cta_portfolio_id": next(iter(portfolios)),
            "minis_id": result["minis_id"],
            "drama_id": str(
                uuid5(
                    context.tenant_id,
                    f"copy-source:{bc_id}:{advertiser_id}:{campaign_id}",
                )
            ),
            "title": root.name,
            "url": next(iter(urls)),
            "protected_base": root.name,
            "provider_pinyin": "",
            "external_drama_id": "",
            "display_drama_id": "",
            # 正常目录刷新会推进观察时间/发布版本；只有规范化业务设置、
            # 层级和已核实的素材/文案绑定改变，才使复制来源失效。
            "source_digest": digest(
                {
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                    "campaign": campaign,
                    "groups": [
                        {
                            "remote_id": g["remote_id"],
                            "body": g["body"],
                            "ads": [
                                {"remote_id": ad["remote_id"], "body": ad["body"]}
                                for ad in g["ads"]
                            ],
                        }
                        for g in frozen_groups
                    ],
                }
            ),
        }
    except DomainError as error:
        result["blockers"] = [error.message]
    return result
