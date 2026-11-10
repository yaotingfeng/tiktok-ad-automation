"""创意级止损的财务完整性门控，使用原始事实而非延迟的页面投影。"""

from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS


def group_issues(session, *, tenant_id, accounts, definition, now):
    from app.modules.automation_rules.inputs import local_window

    ratios = {key: (num, den) for key, num, den, _ in DERIVED_RATIOS}
    requirements = {}
    for condition in definition.conditions:
        if condition.range:
            metrics = ratios.get(condition.metric, (condition.metric,))
            requirements.setdefault(str(condition.range), set()).update(
                set(metrics) & set(STORED_KEYS)
            )
    if not requirements or not accounts:
        return {}
    # 只信官方账户创建时刻；最早事实/水位不能证明更早日期没有投放。
    created_at = dict(
        session.connection()
        .execute(
            text("""SELECT advertiser_id,remote_created_at FROM advertiser_account
            WHERE tenant_id=:t AND advertiser_id=ANY(CAST(:advs AS text[]))"""),
            {"t": tenant_id, "advs": list(accounts)},
        )
        .all()
    )
    issues = {}
    for window, metrics in requirements.items():
        if not metrics:
            continue
        metrics = sorted(metrics)
        bounds, history_known = [], []
        for adv, info in accounts.items():
            start, end = local_window(window, info.timezone, now)
            created = created_at.get(adv)
            known = window != "LIFETIME" or created is not None
            if created is not None:
                if created > now:
                    known = False
                else:
                    creation_day = created.astimezone(ZoneInfo(info.timezone)).date()
                    start = (
                        creation_day
                        if window == "LIFETIME"
                        else max(start, creation_day)
                    )
            bounds.append((start, end))
            history_known.append(known)
        sums = ",".join(
            f"CASE WHEN count({m})=count(*) THEN sum({m}) END AS {m}" for m in metrics
        )
        unknown = " OR ".join(
            f"(a.n IS NOT NULL AND a.{m} IS NULL) OR (c.n IS NOT NULL AND c.{m} IS NULL)"
            for m in metrics
        )
        difference = " OR ".join(
            f"COALESCE(a.{m},0)<>COALESCE(c.{m},0)" for m in metrics
        )
        valuable = " OR ".join(f"f.{m} IS NULL OR f.{m}<>0" for m in metrics)
        # 来源只在同一次完整覆盖、近邻观察时可比较；未关联创意独立成因，不能吞金额。
        rows = (
            session.connection()
            .execute(
                text(f"""
            WITH w AS (SELECT * FROM unnest(CAST(:advs AS text[]),CAST(:starts AS date[]),CAST(:ends AS date[]),CAST(:known AS boolean[])) AS x(adv,s,e,known)),
            coverage AS (
              -- 每日状态主键唯一，成对完整日期数必须等于所需日数；不展开账户×生命周期日期。
              SELECT w.adv,NULL::text AS gid,'MATERIAL_SOURCE_INCOMPLETE' AS issue
              FROM w LEFT JOIN LATERAL (
                SELECT count(*) AS days FROM report_sync_state sa
                JOIN report_sync_state sc ON sc.tenant_id=sa.tenant_id AND sc.advertiser_id=sa.advertiser_id
                  AND sc.stat_date=sa.stat_date AND sc.report_kind='creative_day'
                WHERE w.known AND sa.tenant_id=:t AND sa.advertiser_id=w.adv
                  AND sa.report_kind='ad_day' AND sa.stat_date BETWEEN w.s AND w.e
                  AND sa.status='COMPLETE' AND sc.status='COMPLETE'
                  AND sa.observed_at IS NOT NULL AND sc.observed_at IS NOT NULL
                  AND abs(extract(epoch from sa.observed_at-sc.observed_at))<=1800
              ) paired ON true
              WHERE NOT w.known OR paired.days<>GREATEST(w.e-w.s+1,0)),
            a AS (SELECT f.advertiser_id,f.stat_date,f.adgroup_id,f.currency,f.timezone,count(*) AS n,{sums}
              FROM ad_report_day f JOIN w ON w.adv=f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e
              WHERE f.tenant_id=:t GROUP BY f.advertiser_id,f.stat_date,f.adgroup_id,f.currency,f.timezone),
            c AS (SELECT f.advertiser_id,f.stat_date,f.adgroup_id,f.currency,f.timezone,count(*) AS n,{sums}
              FROM creative_report_day f JOIN w ON w.adv=f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e
              WHERE f.tenant_id=:t GROUP BY f.advertiser_id,f.stat_date,f.adgroup_id,f.currency,f.timezone),
            checked AS (SELECT COALESCE(a.advertiser_id,c.advertiser_id) AS adv,
              COALESCE(a.adgroup_id,c.adgroup_id) AS gid,
              CASE WHEN sa.status IS DISTINCT FROM 'COMPLETE' OR sc.status IS DISTINCT FROM 'COMPLETE'
                OR sa.observed_at IS NULL OR sc.observed_at IS NULL
                OR abs(extract(epoch from sa.observed_at-sc.observed_at))>1800 THEN 'MATERIAL_SOURCE_INCOMPLETE'
                WHEN {unknown} THEN 'MATERIAL_METRIC_MISSING'
                WHEN {difference} THEN 'MATERIAL_RECONCILIATION_GAP' END AS issue
              FROM a FULL JOIN c USING(advertiser_id,stat_date,adgroup_id,currency,timezone)
              LEFT JOIN report_sync_state sa ON sa.tenant_id=:t AND sa.advertiser_id=COALESCE(a.advertiser_id,c.advertiser_id)
                AND sa.stat_date=COALESCE(a.stat_date,c.stat_date) AND sa.report_kind='ad_day'
              LEFT JOIN report_sync_state sc ON sc.tenant_id=:t AND sc.advertiser_id=COALESCE(a.advertiser_id,c.advertiser_id)
                AND sc.stat_date=COALESCE(a.stat_date,c.stat_date) AND sc.report_kind='creative_day'),
            unmapped AS (SELECT DISTINCT f.advertiser_id AS adv,f.adgroup_id AS gid,'MATERIAL_CREATIVE_UNRESOLVED' AS issue
              FROM creative_report_day f JOIN w ON w.adv=f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e
              LEFT JOIN ad_creative m ON m.tenant_id=f.tenant_id AND m.advertiser_id=f.advertiser_id AND m.creative_id=f.creative_id
              WHERE f.tenant_id=:t AND COALESCE(m.identity_status,'UNRESOLVED') NOT IN ('RESOLVED','COMPOSITE') AND ({valuable}))
            SELECT adv,gid,issue FROM checked WHERE issue IS NOT NULL
            UNION ALL SELECT adv,gid,issue FROM unmapped
            UNION ALL SELECT adv,gid,issue FROM coverage
            ORDER BY issue
        """),
                {
                    "t": tenant_id,
                    "advs": list(accounts),
                    "starts": [b[0] for b in bounds],
                    "ends": [b[1] for b in bounds],
                    "known": history_known,
                },
            )
            .all()
        )
        for row in rows:
            # 未关联是具体的身份缺数，优先于其可能同时造成的财务差额。
            key = (row.adv, row.gid)
            if row.issue == "MATERIAL_CREATIVE_UNRESOLVED" or key not in issues:
                issues[key] = row.issue
    return issues
