"""0115 前置身份保全：每次只处理一个账户的有界父广告页，不访问平台。"""

import argparse
import json
import sys
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
    预回填期间仍可能有目录写入，冻结后的 0115 必须重新验证全局覆盖。
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

    def identities():
        return {
            row["creative_id"]: dict(row)
            for row in connection.execute(
                text("""
            SELECT * FROM ad_creative WHERE tenant_id=:tenant AND advertiser_id=:account
            AND creative_id=ANY(CAST(:ids AS text[]))
        """),
                scope | {"ids": sorted(wanted)},
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
        for row in rows:
            original = known.get(row["creative_id"])
            if _covered(original, row):
                continue
            row["asset_order_source"] = "CANONICAL_REFERENCE_SORT"
            child = by_id.get(row["creative_id"])
            if child:
                row["deleted"] |= child["operation_status"] in {"DELETE", "DELETED"}
                row["operation_status"] = child["operation_status"]
            identity = {
                key: row[key] for key in ("ad_remote_id", "ad_material_id", "assets")
            }
            proof = {
                "identity": identity,
                "published_version": row["published_version"],
                "observed_at": row["observed_at"].isoformat(),
                "asset_order_source": "CANONICAL_REFERENCE_SORT",
            }
            conflict = False
            if original is None:
                written += _write_groups(
                    session,
                    tenant_id=tenant_id,
                    advertiser_id=advertiser_id,
                    rows=[row],
                )
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
                written += _write_groups(
                    session,
                    tenant_id=tenant_id,
                    advertiser_id=advertiser_id,
                    rows=[enriched],
                )
            else:
                conflict = True
            # 真实不相容观察进入标准冲突合同；后续弱 MID 恢复也必须看见它。
            # 所有未承接旧观察均追加精确证据，包括冲突、旧版本和原 UNRESOLVED。
            # 不用旧观察覆盖已有完整身份，且相同页重试不会重复追加证据。
            result = connection.execute(
                text("""
                UPDATE ad_creative SET evidence=jsonb_set(evidence,'{legacy_identities}',
                    COALESCE(evidence->'legacy_identities','[]'::jsonb) || CASE WHEN
                    COALESCE(evidence->'legacy_identities','[]'::jsonb) @> jsonb_build_array(CAST(:proof AS jsonb))
                    THEN '[]'::jsonb ELSE jsonb_build_array(CAST(:proof AS jsonb)) END)
                    || CASE WHEN :conflict THEN jsonb_build_object('conflicts',
                       COALESCE(evidence->'conflicts','[]'::jsonb) || CASE WHEN
                       COALESCE(evidence->'conflicts','[]'::jsonb) @> jsonb_build_array(CAST(:proof AS jsonb))
                       THEN '[]'::jsonb ELSE jsonb_build_array(CAST(:proof AS jsonb)) END)
                       ELSE '{}'::jsonb END,
                    identity_status=CASE WHEN :conflict THEN 'CONFLICT' ELSE identity_status END
                WHERE tenant_id=:tenant AND advertiser_id=:account AND creative_id=:cid
                  AND (NOT COALESCE(evidence->'legacy_identities','[]'::jsonb) @> jsonb_build_array(CAST(:proof AS jsonb))
                    OR (:conflict AND (identity_status<>'CONFLICT' OR NOT
                      COALESCE(evidence->'conflicts','[]'::jsonb) @> jsonb_build_array(CAST(:proof AS jsonb)))))
            """),
                scope
                | {
                    "cid": row["creative_id"],
                    "parent": row["ad_remote_id"],
                    "mid": row["ad_material_id"],
                    "conflict": conflict,
                    "proof": json.dumps(proof),
                },
            )
            written += result.rowcount
            # 同 CID 可能在同一父广告页内有多组不相容观察，下一组必须读到新证据。
            known = identities()

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
