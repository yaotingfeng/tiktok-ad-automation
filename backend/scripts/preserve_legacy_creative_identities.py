"""0116 前置身份保全：每次只处理一个账户的有界父广告页，不访问平台。"""

import argparse
import json
import sys
from collections import deque
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.core.db import engine
from app.modules.ads.creatives import _backfill_cached, _group_references, _write_groups


def _same_assets(first, second):
    return {(a["material_type"], a["platform_material_id"]) for a in first} >= {
        (a["material_type"], a["platform_material_id"]) for a in second
    }


def _covered(original, row):
    if original is None:
        return False
    evidence = original["evidence"]
    identities = [original]
    if original["identity_status"] == "CONFLICT":
        identities += [
            entry.get("identity", {}) for entry in evidence.get("conflicts", [])
        ]
    return any(
        identity.get("ad_remote_id") == row["ad_remote_id"]
        and (
            row["ad_material_id"] is None
            or identity.get("ad_material_id") == row["ad_material_id"]
        )
        and _same_assets(identity.get("assets", []), row["assets"])
        for identity in identities
    )


def _append_proofs(connection, scope, proofs):
    written = 0
    for start in range(0, len(proofs), 500):
        # 同批 CID 唯一，UPDATE ... FROM 不会为一行随机挑选多份冲突证据。
        result = connection.execute(
            text("""
            UPDATE ad_creative AS current SET evidence=jsonb_set(current.evidence,'{legacy_identities}',
                COALESCE(current.evidence->'legacy_identities','[]'::jsonb) || CASE WHEN
                COALESCE(current.evidence->'legacy_identities','[]'::jsonb) @> jsonb_build_array(incoming.proof)
                THEN '[]'::jsonb ELSE jsonb_build_array(incoming.proof) END)
                || CASE WHEN incoming.conflict THEN jsonb_build_object('conflicts',
                   COALESCE(current.evidence->'conflicts','[]'::jsonb) || CASE WHEN
                   COALESCE(current.evidence->'conflicts','[]'::jsonb) @> jsonb_build_array(incoming.proof)
                   THEN '[]'::jsonb ELSE jsonb_build_array(incoming.proof) END)
                   ELSE '{}'::jsonb END,
                identity_status=CASE WHEN incoming.conflict THEN 'CONFLICT' ELSE current.identity_status END
            FROM jsonb_to_recordset(CAST(:proofs AS jsonb)) AS incoming(cid text,proof jsonb,conflict boolean)
            WHERE current.tenant_id=:tenant AND current.advertiser_id=:account AND current.creative_id=incoming.cid
              AND (NOT COALESCE(current.evidence->'legacy_identities','[]'::jsonb) @> jsonb_build_array(incoming.proof)
                OR (incoming.conflict AND (current.identity_status<>'CONFLICT' OR NOT
                  COALESCE(current.evidence->'conflicts','[]'::jsonb) @> jsonb_build_array(incoming.proof))))
        """),
            scope | {"proofs": json.dumps(proofs[start : start + 500])},
        )
        written += result.rowcount
    return written


def preserve_creative_identities(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    after_parent: str = "",
    parent_limit: int = 100,
) -> dict[str, Any]:
    """按父广告分页，已承接关系零写入；未承接 CID 的全部素材成员一起保全。

    单个事务只处理一页，调用方提交后持久化返回游标；重试同页幂等。
    预回填期间仍可能有目录写入，冻结后的 0116 必须重新验证全局覆盖。
    """
    if not 1 <= parent_limit <= 500:
        raise ValueError("parent_limit must be between 1 and 500")
    connection = session.connection()
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"directory-publish:{tenant_id}:{advertiser_id}"},
    )
    scope = {
        "tenant": tenant_id,
        "account": advertiser_id,
        "after": after_parent,
        "limit": parent_limit,
    }
    parents = (
        connection.execute(
            text("""
        SELECT DISTINCT ad_remote_id FROM ad_material_reference
        WHERE tenant_id=:tenant AND advertiser_id=:account AND ad_remote_id>:after
        ORDER BY ad_remote_id LIMIT :limit
    """),
            scope,
        )
        .scalars()
        .all()
    )
    if not parents:
        return {"written": 0, "after_parent": after_parent, "done": True}
    refs = (
        connection.execute(
            text("""
        SELECT m.*,p.ad_type,p.operation_status AS parent_status,
            COALESCE(p.observed_at,'1970-01-01 UTC'::timestamptz) AS observed_at
        FROM ad_material_reference m LEFT JOIN ad_object p
          ON p.tenant_id=m.tenant_id AND p.advertiser_id=m.advertiser_id
            AND p.kind='ad' AND p.remote_id=m.ad_remote_id
        WHERE m.tenant_id=:tenant AND m.advertiser_id=:account AND m.ad_remote_id=ANY(:parents)
        ORDER BY m.ad_remote_id,m.platform_material_id,m.material_type,m.ad_material_id
    """),
            scope | {"parents": parents},
        )
        .mappings()
        .all()
    )
    rows = _group_references([dict(ref) for ref in refs])
    wanted = frozenset(row["creative_id"] for row in rows)

    def identities(ids=wanted):
        return {
            row["creative_id"]: dict(row)
            for row in connection.execute(
                text("""
            SELECT * FROM ad_creative WHERE tenant_id=:tenant AND advertiser_id=:account
            AND creative_id=ANY(CAST(:ids AS text[]))
        """),
                scope | {"ids": sorted(ids)},
            ).mappings()
        }

    known = identities()
    needed = frozenset(
        row["creative_id"]
        for row in rows
        if not _covered(known.get(row["creative_id"]), row)
    )
    written = 0
    if needed:
        # 仅未承接关系解析官方缓存，主映射及其原有冲突证据始终优先保留。
        written += _backfill_cached(
            session, tenant_id, advertiser_id, needed, primary=True, parent_ids=parents
        )
        known = identities()
        statuses = (
            connection.execute(
                text("""
            SELECT remote_id,operation_status FROM ad_object
            WHERE tenant_id=:tenant AND advertiser_id=:account AND kind='creative'
              AND remote_id=ANY(CAST(:ids AS text[]))
        """),
                scope | {"ids": sorted(needed)},
            )
            .mappings()
            .all()
        )
        by_id = {row["remote_id"]: row for row in statuses}
        # 同 CID 的多组观察必须顺序判定；不同 CID 则成批写入，避免逐条全页回读。
        # 每轮每 CID 只取一条，下一轮读取上一轮最终状态，保持旧版冲突/弱身份语义。
        pending = {}
        for row in rows:
            pending.setdefault(row["creative_id"], deque()).append(row)
        while pending:
            publications, proofs = [], []
            for observations in pending.values():
                row = observations.popleft()
                original = known.get(row["creative_id"])
                if _covered(original, row):
                    continue
                row["asset_order_source"] = "CANONICAL_REFERENCE_SORT"
                child = by_id.get(row["creative_id"])
                if child:
                    row["deleted"] |= child["operation_status"] in {"DELETE", "DELETED"}
                    row["operation_status"] = child["operation_status"]
                identity = {
                    key: row[key]
                    for key in ("ad_remote_id", "ad_material_id", "assets")
                }
                proof = {
                    "identity": identity,
                    "published_version": row["published_version"],
                    "observed_at": row["observed_at"].isoformat(),
                    "asset_order_source": "CANONICAL_REFERENCE_SORT",
                }
                conflict = False
                if original is None:
                    publications.append(row)
                elif (
                    original["ad_remote_id"] == row["ad_remote_id"]
                    and original["ad_material_id"] in (None, row["ad_material_id"])
                    and (
                        (
                            original["identity_status"] == "UNRESOLVED"
                            and not original["assets"]
                        )
                        or _same_assets(original["assets"], row["assets"])
                    )
                ):
                    # 较新的空观察不能抹掉旧正向资产；补齐时保留其版本、时间和状态围栏。
                    enriched = dict(
                        row,
                        published_version=max(
                            original["published_version"], row["published_version"]
                        ),
                        observed_at=max(original["observed_at"], row["observed_at"]),
                        name=original["name"],
                        operation_status=original["operation_status"],
                        deleted=original["deleted"],
                    )
                    if original["assets"]:
                        enriched["assets"] = original["assets"]
                    publications.append(enriched)
                else:
                    conflict = True
                proofs.append(
                    {"cid": row["creative_id"], "proof": proof, "conflict": conflict}
                )
            if publications:
                written += _write_groups(
                    session,
                    tenant_id=tenant_id,
                    advertiser_id=advertiser_id,
                    rows=publications,
                )
            if proofs:
                written += _append_proofs(connection, scope, proofs)
                # 只回读本轮变动 CID 一次；不随每条观察反复传输整页身份。
                known.update(identities({item["cid"] for item in proofs}))
            pending = {
                cid: observations
                for cid, observations in pending.items()
                if observations
            }

    return {
        "written": written,
        "after_parent": parents[-1],
        "done": len(parents) < parent_limit,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--advertiser", required=True)
    parser.add_argument("--after-parent", default="")
    parser.add_argument("--parent-limit", type=int, default=100)
    args = parser.parse_args()
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '60s'"))
        result = preserve_creative_identities(
            session,
            tenant_id=args.tenant,
            advertiser_id=args.advertiser,
            after_parent=args.after_parent,
            parent_limit=args.parent_limit,
        )
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
