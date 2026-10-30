"""报表关联完成条件；图片内容完整性由目录合同单独表达。"""


def complete_material_link(alias: str, *, tenant: str) -> str:
    return f"EXISTS ({material_link_query(alias, tenant=tenant)})"


def material_link_query(alias: str, *, tenant: str) -> str:
    """仅供内部 SQL：同类型广告归属；视频额外需要精确 VID。

    轮播主素材是帖子/轮播 ID，不是视频。不能把当前图片填进 VID 来解除等待，
    也不能把未知类型一概当作轮播放行。
    """
    if alias not in {"v", "d", "r", "l"} or tenant not in {
        ":value_tenant",
        ":tenant",
        ":t",
    }:
        raise ValueError("invalid internal material link scope")
    return f"""
        SELECT true AS found FROM material_post_link linked
        WHERE linked.tenant_id={tenant}
          AND linked.advertiser_id={alias}.advertiser_id
          AND linked.main_material_id={alias}.main_material_id
          AND linked.main_material_type={alias}.main_material_type
          AND NULLIF(linked.ad_remote_id, '') IS NOT NULL
          AND (linked.main_material_type IN ('CAROUSEL_SPARK_ADS','CAROUSEL_NON_SPARK_ADS')
            OR (linked.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
              AND NULLIF(linked.platform_material_id, '') IS NOT NULL))"""
