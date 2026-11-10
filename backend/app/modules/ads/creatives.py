"""创意身份发布和本地回填；目录缺页与缺项均不构成删除证据。"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, text
from sqlmodel import Session

from app.modules.ads.creative_models import AdCreative

_BATCH_SIZE = 500
_COLUMNS = "tenant_id,advertiser_id,creative_id,ad_remote_id,ad_material_id,material_type,platform_material_id,assets,name,operation_status,deleted,identity_status,first_observed_at,observed_at,published_version,evidence"
_SAME_IDENTITY = """jsonb_build_object('ad_remote_id',ad_creative.ad_remote_id,
    'ad_material_id',ad_creative.ad_material_id,'assets',ad_creative.assets)
    = EXCLUDED.evidence->'original_identity'"""
_ENRICHMENT = """ad_creative.identity_status='UNRESOLVED' AND ad_creative.assets='[]'::jsonb
    AND ad_creative.ad_remote_id=EXCLUDED.ad_remote_id
    AND (ad_creative.ad_material_id IS NULL OR ad_creative.ad_material_id=EXCLUDED.ad_material_id)
    AND EXCLUDED.identity_status IN ('RESOLVED','COMPOSITE')"""


def _value(
    row: dict[str, Any],
    *,
    tenant_id: UUID,
    advertiser_id: str,
    observed_at: datetime,
    published_version: int,
) -> dict[str, Any]:
    creative_id, parent = row["creative_id"], row["ad_remote_id"]
    if (
        not isinstance(creative_id, str)
        or not creative_id
        or not isinstance(parent, str)
        or not parent
    ):
        raise ValueError("creative and parent identities must be nonempty strings")
    assets = row["assets"]
    if not isinstance(assets, list):
        raise ValueError("creative assets must be an ordered list")
    normalized = []
    for asset in assets:
        if not isinstance(asset, dict) or any(
            not isinstance(asset.get(key), str) or not asset[key]
            for key in ("platform_material_id", "material_type")
        ):
            raise ValueError("creative asset identity must be nonempty strings")
        normalized.append(
            {key: asset[key] for key in ("platform_material_id", "material_type")}
        )
    kind, platform, status = None, None, "UNRESOLVED"
    if len(normalized) == 1 and normalized[0]["material_type"] in {
        "VIDEO",
        "IMAGE",
        "TIKTOK_POST",
    }:
        kind, platform, status = (
            normalized[0]["material_type"],
            normalized[0]["platform_material_id"],
            "RESOLVED",
        )
    elif len(normalized) > 1 and all(
        asset["material_type"] == "IMAGE" for asset in normalized
    ):
        kind, status = "CAROUSEL", "COMPOSITE"
    identity = {
        "ad_remote_id": parent,
        "ad_material_id": row.get("ad_material_id"),
        "assets": normalized,
    }
    return {
        "tenant_id": tenant_id,
        "advertiser_id": advertiser_id,
        "creative_id": creative_id,
        **identity,
        "material_type": kind,
        "platform_material_id": platform,
        "name": row.get("name") or "",
        "operation_status": row.get("operation_status"),
        "deleted": bool(row.get("deleted", False)),
        "identity_status": status,
        "first_observed_at": observed_at,
        "observed_at": observed_at,
        "published_version": published_version,
        "evidence": {
            "original_identity": identity,
            "conflicts": [],
            "asset_order_source": row.get("asset_order_source", "OBSERVED"),
        },
    }


def upsert_creatives(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    rows: list[dict[str, Any]],
    observed_at: datetime,
    published_version: int,
) -> int:
    """按固定键序原子写入；旧观察无效，冲突只追加证据，不改写原身份。"""
    if published_version < 1 or observed_at.tzinfo is None:
        raise ValueError(
            "creative publication requires a positive version and aware timestamp"
        )
    if not rows:
        return 0
    values = [
        _value(
            row,
            tenant_id=tenant_id,
            advertiser_id=advertiser_id,
            observed_at=observed_at,
            published_version=published_version,
        )
        for row in rows
    ]
    return _upsert_values(
        session, tenant_id=tenant_id, advertiser_id=advertiser_id, values=values
    )


def _upsert_values(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    values: list[dict[str, Any]],
) -> int:
    if not values:
        return 0
    # 同账户发布与回填共享锁，覆盖尚不存在的身份；不能只锁现有行留下首次写竞态。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"directory-publish:{tenant_id}:{advertiser_id}"},
    )
    total = 0
    # 同一批有冲突身份时分轮提交给UPSERT，避免一条SQL多次影响同一个目标行。
    pending = sorted(values, key=lambda value: value["creative_id"])
    while pending:
        unique: dict[str, dict[str, Any]] = {}
        remaining = []
        for value in pending:
            if value["creative_id"] in unique:
                remaining.append(value)
            else:
                unique[value["creative_id"]] = value
        ordered = list(unique.values())
        for start in range(0, len(ordered), _BATCH_SIZE):
            result = session.connection().execute(
                text(f"""
                INSERT INTO ad_creative ({_COLUMNS})
                SELECT {_COLUMNS} FROM jsonb_populate_recordset(NULL::ad_creative,CAST(:rows AS jsonb))
                ORDER BY tenant_id,advertiser_id,creative_id
                ON CONFLICT (tenant_id,advertiser_id,creative_id) DO UPDATE SET
                  name=CASE WHEN {_SAME_IDENTITY} OR {_ENRICHMENT} THEN EXCLUDED.name ELSE ad_creative.name END,
                  operation_status=CASE WHEN {_SAME_IDENTITY} OR {_ENRICHMENT} THEN EXCLUDED.operation_status ELSE ad_creative.operation_status END,
                  deleted=CASE WHEN {_SAME_IDENTITY} OR {_ENRICHMENT} THEN EXCLUDED.deleted ELSE ad_creative.deleted END,
                  observed_at=EXCLUDED.observed_at,
                  published_version=EXCLUDED.published_version,
                  ad_material_id=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.ad_material_id ELSE ad_creative.ad_material_id END,
                  assets=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.assets ELSE ad_creative.assets END,
                  material_type=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.material_type ELSE ad_creative.material_type END,
                  platform_material_id=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.platform_material_id ELSE ad_creative.platform_material_id END,
                  identity_status=CASE WHEN {_ENRICHMENT} THEN EXCLUDED.identity_status
                    WHEN {_SAME_IDENTITY} THEN ad_creative.identity_status ELSE 'CONFLICT' END,
                  evidence=CASE WHEN {_ENRICHMENT} THEN
                    jsonb_set(ad_creative.evidence,'{{enrichments}}',
                      COALESCE(ad_creative.evidence->'enrichments','[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                        'identity',EXCLUDED.evidence->'original_identity',
                        'observed_at',EXCLUDED.observed_at,'published_version',EXCLUDED.published_version)))
                    WHEN {_SAME_IDENTITY} THEN ad_creative.evidence ELSE
                    jsonb_set(ad_creative.evidence,'{{conflicts}}',
                      (ad_creative.evidence->'conflicts') || jsonb_build_array(jsonb_build_object(
                        'identity',EXCLUDED.evidence->'original_identity',
                        'observed_at',EXCLUDED.observed_at,'published_version',EXCLUDED.published_version))) END
                WHERE ad_creative.published_version<=EXCLUDED.published_version
                  AND ad_creative.observed_at<=EXCLUDED.observed_at
                RETURNING creative_id
            """),
                {
                    "rows": json.dumps(
                        ordered[start : start + _BATCH_SIZE],
                        default=str,
                        ensure_ascii=False,
                    )
                },
            )
            total += len(result.fetchall())
        pending = remaining
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AdCreative):
            state = inspect(obj)
            assert state is not None
            identity = state.identity
            if identity and identity[:2] == (tenant_id, advertiser_id):
                session.expire(obj)
    return total


def _group_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for ref in references:
        # Smart+没有native creative id时保持未知，不用ad_material_id或父广告伪造。
        creative_ids = ref["creative_ids"] or (
            [ref["ad_remote_id"]]
            if ref["ad_type"] is not None and ref["ad_type"] != "SMART_PLUS"
            else []
        )
        for creative_id in creative_ids:
            key = (creative_id, ref["ad_remote_id"], ref["ad_material_id"])
            if key not in groups:
                groups[key] = {
                    "creative_id": creative_id,
                    "ad_remote_id": ref["ad_remote_id"],
                    "ad_material_id": ref["ad_material_id"],
                    "assets": [],
                    "name": ref["name"],
                    "operation_status": ref["operation_status"],
                    "deleted": ref["parent_status"] in {"DELETE", "DELETED"},
                    "observed_at": ref["observed_at"],
                    "published_version": ref["published_version"],
                }
            row = groups[key]
            asset = {
                "platform_material_id": ref["platform_material_id"],
                "material_type": ref["material_type"],
            }
            if asset not in row["assets"]:
                row["assets"].append(asset)
            row["published_version"] = min(
                row["published_version"], ref["published_version"]
            )
    return list(groups.values())


def _write_groups(
    session: Session, *, tenant_id: UUID, advertiser_id: str, rows: list[dict[str, Any]]
) -> int:
    values = [
        _value(
            row,
            tenant_id=tenant_id,
            advertiser_id=advertiser_id,
            observed_at=row["observed_at"],
            published_version=row["published_version"],
        )
        for row in rows
    ]
    return _upsert_values(
        session, tenant_id=tenant_id, advertiser_id=advertiser_id, values=values
    )


def backfill_creatives(
    session: Session, *, tenant_id: UUID | None = None, advertiser_id: str | None = None
) -> int:
    """只从本地历史引用回填，按账户/父广告分页；不扫描平台、不推断缺失删除。"""
    filters, params = [], {}
    if tenant_id is not None:
        filters.append("tenant_id=:tenant")
        params["tenant"] = tenant_id
    if advertiser_id is not None:
        filters.append("advertiser_id=:advertiser")
        params["advertiser"] = advertiser_id
    where = " WHERE " + " AND ".join(filters) if filters else ""
    accounts = (
        session.connection()
        .execute(
            text(
                "SELECT DISTINCT tenant_id,advertiser_id FROM ad_material_reference"
                + where
                + " ORDER BY tenant_id,advertiser_id"
            ),
            params,
        )
        .all()
    )
    count = 0
    for tenant, account in accounts:
        # 读取已有顺序前先与目录发布互斥，避免等待UPSERT时使用过时的映射快照。
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"directory-publish:{tenant}:{account}"},
        )
        after = ""
        while True:
            scope = {
                "tenant": tenant,
                "account": account,
                "after": after,
                "limit": _BATCH_SIZE,
            }
            parents = (
                session.connection()
                .execute(
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
                break
            refs = (
                session.connection()
                .execute(
                    text("""
                SELECT m.*,p.ad_type,p.operation_status AS parent_status,
                    COALESCE(p.observed_at,'1970-01-01 UTC'::timestamptz) AS observed_at
                FROM ad_material_reference m LEFT JOIN ad_object p
                  ON p.tenant_id=m.tenant_id AND p.advertiser_id=m.advertiser_id
                    AND p.kind='ad' AND p.remote_id=m.ad_remote_id
                WHERE m.tenant_id=:tenant AND m.advertiser_id=:account AND m.ad_remote_id=ANY(:parents)
                ORDER BY m.ad_remote_id,m.platform_material_id,m.material_type,m.ad_material_id
            """),
                    {**scope, "parents": parents},
                )
                .mappings()
                .all()
            )
            rows = _group_references([dict(ref) for ref in refs])
            if rows:
                existing = (
                    session.connection()
                    .execute(
                        text("""
                    SELECT creative_id,ad_remote_id,ad_material_id,assets,published_version FROM ad_creative
                    WHERE tenant_id=:tenant AND advertiser_id=:account AND creative_id=ANY(:ids)
                """),
                        {**scope, "ids": sorted({row["creative_id"] for row in rows})},
                    )
                    .mappings()
                    .all()
                )
                by_creative = {row["creative_id"]: row for row in existing}
                accepted = []
                for row in rows:
                    original = by_creative.get(row["creative_id"])
                    # 历史引用可能丢失共享素材成员且没有位置列：同身份的资产子集
                    # 沿用完整已知资产/顺序，不能以缺失推断删除或制造冲突。
                    if (
                        original
                        and original["ad_remote_id"] == row["ad_remote_id"]
                        and original["ad_material_id"] == row["ad_material_id"]
                        and {
                            (asset["material_type"], asset["platform_material_id"])
                            for asset in original["assets"]
                        }
                        >= {
                            (asset["material_type"], asset["platform_material_id"])
                            for asset in row["assets"]
                        }
                    ):
                        row["assets"] = original["assets"]
                        # 父目录可能更晚同步，但它的时间不能提升旧素材引用的权威性。
                        # 相同或较旧版本的弱回填仅补不存在的身份，不覆盖live状态。
                        if row["published_version"] <= original["published_version"]:
                            continue
                    accepted.append(row)
                rows = accepted
                if not rows:
                    after = parents[-1]
                    continue
                # 创意实体明确删除优先；父广告未删不能反推已删除创意仍有效。
                statuses = (
                    session.connection()
                    .execute(
                        text("""
                    SELECT remote_id,operation_status,observed_at FROM ad_object
                    WHERE tenant_id=:tenant AND advertiser_id=:account AND kind='creative' AND remote_id=ANY(:ids)
                """),
                        {**scope, "ids": sorted({row["creative_id"] for row in rows})},
                    )
                    .mappings()
                    .all()
                )
                by_id = {row["remote_id"]: row for row in statuses}
                for row in rows:
                    row["asset_order_source"] = "CANONICAL_REFERENCE_SORT"
                    child = by_id.get(row["creative_id"])
                    if child:
                        row["deleted"] = row["deleted"] or child[
                            "operation_status"
                        ] in {"DELETE", "DELETED"}
                        row["operation_status"] = child["operation_status"]
                count += _write_groups(
                    session, tenant_id=tenant, advertiser_id=account, rows=rows
                )
            after = parents[-1]
    return count


def publish_staged_creatives(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    materials_table: str,
    entities_table: str,
    published_version: int,
    observed_at: datetime,
) -> int:
    """只接收目录发布器已胜出且完整的素材；同创意跨页图片合并后一次发布。"""
    # 临时表名只由sync内部生成；完整观察流保留跨页共享素材的每个创意成员，
    # 按观察顺序分别组装各创意轮播，不使用canonical去重后的行或物理ctid。
    references = (
        session.connection()
        .execute(
            text(f"""
        SELECT m.*,p.ad_type,p.operation_status AS parent_status,COALESCE(p.observed_at,:observed) AS observed_at
        FROM {materials_table} m LEFT JOIN ad_object p
          ON p.tenant_id=m.tenant_id AND p.advertiser_id=m.advertiser_id
            AND p.kind='ad' AND p.remote_id=m.ad_remote_id
        WHERE m.complete ORDER BY m.creative_asset_ordinal
    """),
            {"observed": observed_at},
        )
        .mappings()
        .all()
    )
    rows = _group_references([dict(ref) for ref in references])
    # entity-only观察只能更新已有映射，或建立明确普通广告/创意的未知身份。
    # 不要求configuration.creative_list存在，普通creative子读取也可补齐身份。
    entities = (
        session.connection()
        .execute(
            text(f"""
        SELECT o.kind,o.remote_id,o.parent_kind,o.parent_remote_id,o.ad_type,
               o.name,o.operation_status,o.observed_at,p.operation_status AS parent_status
        FROM {entities_table} s JOIN ad_object o
          ON o.tenant_id=:tenant AND o.advertiser_id=:account AND o.kind=s.kind AND o.remote_id=s.remote_id
        LEFT JOIN ad_object p ON p.tenant_id=o.tenant_id AND p.advertiser_id=o.advertiser_id
          AND p.kind='ad' AND p.remote_id=o.parent_remote_id AND o.parent_kind='ad'
        WHERE o.published_version=:version AND o.kind IN ('ad','creative')
        ORDER BY o.kind,o.remote_id
    """),
            {
                "tenant": tenant_id,
                "account": advertiser_id,
                "version": published_version,
            },
        )
        .mappings()
        .all()
    )
    entities_by_id = {
        entity["remote_id"]: entity
        for entity in entities
        if entity["kind"] == "creative"
    }
    for row in rows:
        child = entities_by_id.get(row["creative_id"])
        if (
            child
            and child["parent_kind"] == "ad"
            and child["parent_remote_id"] == row["ad_remote_id"]
        ):
            row["observed_at"] = child["observed_at"]
            row["operation_status"] = child["operation_status"]
            row["deleted"] = row["deleted"] or child["operation_status"] in {
                "DELETE",
                "DELETED",
            }
    mapped = {row["creative_id"] for row in rows}
    candidates = [
        entity
        for entity in entities
        if entity["remote_id"] not in mapped
        and (
            entity["kind"] == "creative"
            and entity["parent_kind"] == "ad"
            or entity["kind"] == "ad"
            and entity["ad_type"] != "SMART_PLUS"
        )
    ]
    deleted_parents = {
        entity["remote_id"]: entity
        for entity in entities
        if entity["kind"] == "ad"
        and entity["operation_status"] in {"DELETE", "DELETED"}
    }
    if candidates or deleted_parents:
        known = (
            session.connection()
            .execute(
                text("""
            SELECT * FROM ad_creative WHERE tenant_id=:tenant AND advertiser_id=:account AND (creative_id=ANY(:ids) OR ad_remote_id=ANY(:parents))
        """),
                {
                    "tenant": tenant_id,
                    "account": advertiser_id,
                    "ids": [entity["remote_id"] for entity in candidates],
                    "parents": list(deleted_parents),
                },
            )
            .mappings()
            .all()
        )
        existing = {row["creative_id"]: row for row in known}
        for entity in candidates:
            original = existing.get(entity["remote_id"])
            # 空资产只代表未知；后续同父广告完整证据可显式补齐并留下enrichment证据。
            rows.append(
                {
                    "creative_id": entity["remote_id"],
                    "ad_remote_id": entity["parent_remote_id"]
                    if entity["kind"] == "creative"
                    else entity["remote_id"],
                    "ad_material_id": original["ad_material_id"] if original else None,
                    "assets": original["assets"] if original else [],
                    "name": entity["name"],
                    "operation_status": entity["operation_status"],
                    # 子对象ENABLE不能推翻父广告的明确删除；父状态在账户发布锁下
                    # 从canonical读取，不要求父广告也出现在本轮部分子目录中。
                    "deleted": entity["operation_status"] in {"DELETE", "DELETED"}
                    or entity["parent_status"] in {"DELETE", "DELETED"},
                    "observed_at": entity["observed_at"],
                    "published_version": published_version,
                }
            )
        observed_ids = {row["creative_id"] for row in rows}
        for original in known:
            parent = deleted_parents.get(original["ad_remote_id"])
            if parent and original["creative_id"] not in observed_ids:
                rows.append(
                    {
                        **dict(original),
                        "deleted": True,
                        "operation_status": "DELETE",
                        "observed_at": parent["observed_at"],
                        "published_version": published_version,
                    }
                )
    return _write_groups(
        session, tenant_id=tenant_id, advertiser_id=advertiser_id, rows=rows
    )
