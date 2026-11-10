"""素材规则读取前后校验来源；不改变已有写事务/保存点的隔离级别。"""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.modules.automation_rules.inputs import AccountInfo, local_window


def source_proof(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    ranges: list[str],
    now: datetime,
) -> dict[str, list[tuple[str, str, str]]]:
    """一条语句取得全部窗口的修订与目录 MVCC 版本，含无事实的身份/空桶。

    财务桶修订由同事务触发器单调推进，删除后仍保留，因此可发现空桶首次发布。
    目录 xmin 同时涵盖语义变更与当前操作证据，不依赖有历史事实才产生的脏桶。
    仅比较本轮账户/日期，不拿发布锁；变化时整账户停止判断并留审计原因。
    """
    bounds = []
    for info in accounts.values():
        windows = [local_window(r, info.timezone, now) for r in ranges]
        # LIFETIME 完整性以官方创建日为界，可能早于金额读取的默认历史下界。
        bounds.append(
            (min(w[0] for w in windows), max(w[1] for w in windows))
            if windows
            else (now.date(), now.date())
        )
    rows = (
        session.connection()
        .execute(
            text("""
        WITH w AS (SELECT * FROM unnest(CAST(:advs AS text[]),CAST(:starts AS date[]),CAST(:ends AS date[])) AS x(adv,s,e)),
        versions AS (
          SELECT b.advertiser_id,'bucket' AS source,b.bucket::text AS key,b.source_revision::text AS version
          FROM read_bucket_state b JOIN w ON w.adv=b.advertiser_id
          JOIN advertiser_account a ON a.tenant_id=b.tenant_id AND a.advertiser_id=b.advertiser_id
          WHERE b.tenant_id=:t AND b.kind='creative_material'
            AND b.bucket BETWEEN CASE WHEN :lifetime THEN LEAST(w.s,(a.remote_created_at AT TIME ZONE a.timezone)::date) ELSE w.s END AND w.e
          UNION ALL SELECT c.advertiser_id,'identity',c.creative_id,c.xmin::text
            FROM ad_creative c JOIN w ON w.adv=c.advertiser_id WHERE c.tenant_id=:t
          UNION ALL SELECT u.advertiser_id,'usage',u.id::text,u.xmin::text
            FROM ad_material_reference u JOIN w ON w.adv=u.advertiser_id WHERE u.tenant_id=:t
          UNION ALL SELECT a.advertiser_id,'account',a.advertiser_id,a.xmin::text
            FROM advertiser_account a JOIN w ON w.adv=a.advertiser_id WHERE a.tenant_id=:t
          UNION ALL SELECT o.advertiser_id,'object',o.kind||':'||o.remote_id,o.xmin::text
            FROM ad_object o JOIN w ON w.adv=o.advertiser_id WHERE o.tenant_id=:t
        ) SELECT * FROM versions ORDER BY advertiser_id,source,key
        """),
            {
                "t": tenant_id,
                "advs": list(accounts),
                "starts": [b[0] for b in bounds],
                "ends": [b[1] for b in bounds],
                "lifetime": "LIFETIME" in ranges,
            },
        )
        .all()
    )
    result: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in rows:
        result[row.advertiser_id].append((row.source, row.key, row.version))
    return dict(result)
