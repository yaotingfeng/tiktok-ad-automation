"""创意素材查询的唯一事实入口：先筛选每条经济事实，再跨广告合计。"""

from sqlalchemy import text

from app.modules.reporting.store_models import ReportSyncState

# 派生覆盖必须同时确认两份源报表及身份修订；未重建的旧投影不能标完整。
PERIOD_SQL = """SELECT b.tenant_id,b.advertiser_id,b.bucket AS stat_date,b.derived_revision,
    b.derived_observed_at,
    CASE WHEN b.derived_revision>0 AND b.derived_revision=b.source_revision
        AND b.derived_status='COMPLETE' AND a.status='COMPLETE' AND c.status='COMPLETE'
        AND a.observed_at IS NOT NULL AND c.observed_at IS NOT NULL
        AND abs(extract(epoch FROM a.observed_at-c.observed_at))<=1800
        THEN 'COMPLETE' ELSE 'STALE' END AS status
    FROM read_bucket_state b
    LEFT JOIN report_sync_state a ON a.tenant_id=b.tenant_id AND a.advertiser_id=b.advertiser_id
        AND a.stat_date=b.bucket AND a.report_kind='ad_day'
    LEFT JOIN report_sync_state c ON c.tenant_id=b.tenant_id AND c.advertiser_id=b.advertiser_id
        AND c.stat_date=b.bucket AND c.report_kind='creative_day'
    WHERE b.tenant_id=:tenant AND b.advertiser_id=ANY(:advs)
        AND b.kind='creative_material' AND b.bucket BETWEEN :start AND :end"""


def creative_states(session, *, tenant_id, advertisers, start, end):
    rows = (
        session.connection()
        .execute(
            text(PERIOD_SQL),
            {
                "tenant": tenant_id,
                "advs": list(advertisers),
                "start": start,
                "end": end,
            },
        )
        .mappings()
    )
    return [
        ReportSyncState(
            tenant_id=tenant_id,
            advertiser_id=r["advertiser_id"],
            report_kind="creative_material",
            stat_date=r["stat_date"],
            status=r["status"],
            publication_version=r["derived_revision"],
            observed_at=r["derived_observed_at"],
            last_success_at=r["derived_observed_at"],
        )
        for r in rows
    ]


def filtered_ctes(filters, params, *, exact_ad=None):
    """目录只补筛选字段；不以当前目录连接扩增或丢弃历史事实。"""
    from app.modules.reporting.row_set import _entity_conditions, _text_conditions

    def conditions(alias):
        checks = _entity_conditions(filters, params, e="ae", p="p")
        if exact_ad is not None:
            checks.append(f"{alias}.ad_remote_id={params.add('exact_ad', exact_ad)}")
        checks += _text_conditions(
            filters,
            params,
            ids=f"array_remove(ARRAY[{alias}.row_key,{alias}.platform_material_id,{alias}.creative_id,{alias}.advertiser_id,{alias}.ad_remote_id,{alias}.adgroup_id,{alias}.campaign_id],NULL)",
            haystack=f"concat_ws(' ',{alias}.row_key,{alias}.platform_material_id,{alias}.creative_id,{alias}.advertiser_id,{alias}.material_name,{alias}.ad_remote_id,{alias}.adgroup_id,{alias}.campaign_id,a.name,ae.name,g.name,c.name,p.drama_name,p.provider_label)",
        )
        return " AND ".join(checks) or "TRUE"

    def joins(alias):
        return f"""LEFT JOIN advertiser_account a ON a.tenant_id={alias}.tenant_id AND a.advertiser_id={alias}.advertiser_id
        LEFT JOIN ad_entity_read ae ON ae.tenant_id={alias}.tenant_id AND ae.advertiser_id={alias}.advertiser_id AND ae.kind='ad' AND ae.remote_id={alias}.ad_remote_id
        LEFT JOIN ad_entity_read g ON g.tenant_id={alias}.tenant_id AND g.advertiser_id={alias}.advertiser_id AND g.kind='adgroup' AND g.remote_id={alias}.adgroup_id
        LEFT JOIN ad_entity_read c ON c.tenant_id={alias}.tenant_id AND c.advertiser_id={alias}.advertiser_id AND c.kind='campaign' AND c.remote_id={alias}.campaign_id
        LEFT JOIN campaign_name_current p ON p.tenant_id={alias}.tenant_id AND p.advertiser_id={alias}.advertiser_id AND p.campaign_remote_id={alias}.campaign_id"""

    return f"""creative_selected AS NOT MATERIALIZED (
        SELECT f.* FROM creative_material_entity_day f {joins("f")}
        WHERE f.tenant_id=:tenant AND f.advertiser_id=ANY(:advs)
            AND f.stat_date BETWEEN :start AND :end AND {conditions("f")}
    ), creative_filtered AS NOT MATERIALIZED (
        SELECT f.*,CASE WHEN {"FALSE" if conditions("f") == "TRUE" else "TRUE"} THEN EXISTS (
            SELECT 1 FROM creative_material_entity_day residual
            WHERE residual.tenant_id=f.tenant_id AND residual.advertiser_id=f.advertiser_id
                AND residual.stat_date=f.stat_date AND residual.adgroup_id=f.adgroup_id
                AND residual.currency=f.currency AND residual.attribution_status='RESIDUAL'
                AND NOT EXISTS (SELECT 1 FROM creative_selected included
                    WHERE included.advertiser_id=residual.advertiser_id AND included.stat_date=residual.stat_date
                        AND included.fact_key=residual.fact_key AND included.currency=residual.currency)
        ) ELSE false END AS unallocated
        FROM creative_selected f
    ), directory_material AS (
        SELECT u.*,jsonb_build_array('material',u.material_type,u.platform_material_id)::text AS row_key,
            u.name AS material_name,NULL::text AS creative_id,ae.parent_remote_id AS adgroup_id,g.parent_remote_id AS campaign_id
        FROM material_ad_member_current u
        LEFT JOIN ad_entity_read ae ON ae.tenant_id=u.tenant_id AND ae.advertiser_id=u.advertiser_id AND ae.kind='ad' AND ae.remote_id=u.ad_remote_id
        LEFT JOIN ad_entity_read g ON g.tenant_id=u.tenant_id AND g.advertiser_id=u.advertiser_id AND g.kind='adgroup' AND g.remote_id=ae.parent_remote_id
        WHERE u.tenant_id=:tenant AND u.advertiser_id=ANY(:advs) AND u.material_type='VIDEO'
    ), creative_directory AS (
        SELECT u.* FROM directory_material u {joins("u")}
        WHERE {conditions("u")} AND ({"TRUE" if filters.include_empty else "FALSE"} OR EXISTS (
            SELECT 1 FROM creative_filtered f WHERE f.row_key=u.row_key AND f.advertiser_id=u.advertiser_id
                AND f.ad_remote_id=u.ad_remote_id AND f.ad_material_id=u.ad_material_id
                AND f.attribution_status='RESOLVED'
        ))
    ), creative_uses AS (
        SELECT * FROM creative_directory WHERE ad_material_id IS NOT NULL
    )"""


def read_facts(
    session,
    *,
    tenant_id,
    advertisers,
    filters,
    row_keys=None,
    directory=False,
    exact_ad=None,
):
    from app.modules.reporting.row_set import _Params

    params = _Params()
    params.values.update(
        tenant=tenant_id,
        advs=list(advertisers),
        start=filters.start_date,
        end=filters.end_date,
    )
    ctes = filtered_ctes(filters, params, exact_ad=exact_ad)
    where = ""
    if row_keys is not None:
        params.values["row_keys"] = list(row_keys)
        where = "WHERE row_key=ANY(:row_keys)"
    return (
        session.connection()
        .execute(
            text(
                f"WITH {ctes} SELECT * FROM {'creative_directory' if directory else 'creative_filtered'} {where}"
            ),
            params.values,
        )
        .mappings()
        .all()
    )


def trend_rows(session, *, tenant_id, advertisers, filters, row_keys):
    """趋势也在合计前应用快照筛选，按原始行键保留素材类型隔离。"""
    from app.modules.reporting.row_set import _Params
    from app.modules.reporting.store_models import METRIC_COLUMNS

    params = _Params()
    params.values.update(
        tenant=tenant_id,
        advs=list(advertisers),
        start=filters.start_date,
        end=filters.end_date,
        row_keys=list(row_keys),
    )
    ctes = filtered_ctes(filters, params)
    columns = ",".join(
        f"sum({m}) AS {m},count({m}) AS n_{m},bool_or({m}_has_null) AS null_{m}"
        for m in METRIC_COLUMNS
    )
    return (
        session.connection()
        .execute(
            text(f"""WITH {ctes}
        SELECT stat_date AS at,currency,timezone,count(*) AS n_rows,
            bool_or(source_status<>'COMPLETE' OR unallocated) AS source_incomplete,{columns}
        FROM creative_filtered WHERE {"row_key=ANY(:row_keys)" if row_keys else "TRUE"}
        GROUP BY stat_date,currency,timezone ORDER BY stat_date
    """),
            params.values,
        )
        .mappings()
        .all()
    )
