"""冻结排除与运行期依赖均以原定广告为原子单位。"""

from sqlalchemy import text

from .preview_models import PreviewAdReadiness


def included_ad_sql(alias="a"):
    # VIDEO 及历史记录无逐广告事实；它们继续使用既有整组规则。
    return f"NOT EXISTS (SELECT 1 FROM preview_ad_readiness ar WHERE ar.tenant_id={alias}.tenant_id AND ar.preview_id={alias}.preview_id AND ar.ad_id={alias}.id AND NOT ar.included)"


def included_group_sql(alias="g"):
    return f"EXISTS (SELECT 1 FROM planned_ad a WHERE a.tenant_id={alias}.tenant_id AND a.preview_id={alias}.preview_id AND a.group_id={alias}.id AND {included_ad_sql()})"


def ad_reasons(session, ad):
    row = session.get(PreviewAdReadiness, (ad.tenant_id, ad.preview_id, ad.id))
    return row.reason_codes if row else []


def carousel_material_state(
    session, *, tenant_id, preview_id, submission_id, unit_id, group_id=None, ad_id=None
):
    """广告全员成功才 READY；共享父级只需一条完整广告，全部失败才 FAILED。"""
    value = (
        session.execute(
            text(
                """
    WITH ads AS (
      SELECT a.id, count(m.material_id)>0 AND bool_and(s.status='SUCCEEDED') AND count(m.material_id)=count(s.id) ready,
        coalesce(bool_or(s.status='FAILED'),false) failed
      FROM planned_group g JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id
      LEFT JOIN frozen_ad_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.base_ad_no=a.base_ad_no
      LEFT JOIN execution_step s ON s.tenant_id=g.tenant_id AND s.submission_id=:submission AND s.unit_id=g.unit_id AND s.kind='MATERIAL' AND s.material_id=m.material_id
      WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit
        AND (CAST(:group AS uuid) IS NULL OR g.id=:group)
        AND (CAST(:ad AS uuid) IS NULL OR a.id=:ad) AND """
                + included_ad_sql()
                + """
      GROUP BY a.id
    ) SELECT coalesce(bool_or(ready),false) ready,coalesce(bool_and(failed),true) failed FROM ads
    """
            ),
            {
                "tenant": tenant_id,
                "preview": preview_id,
                "submission": submission_id,
                "unit": unit_id,
                "group": group_id,
                "ad": ad_id,
            },
        )
        .mappings()
        .one()
    )
    return "READY" if value["ready"] else "FAILED" if value["failed"] else "PENDING"
