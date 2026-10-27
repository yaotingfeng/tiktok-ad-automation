"""目录请求的覆盖判定：对象版本更新不能替代另一冻结查询范围。"""

from typing import Any

from sqlalchemy import and_, func, literal, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY


def _query(run: Any):
    query = run.query
    if isinstance(query, dict):
        return literal(query, type_=JSONB)
    return query if isinstance(query, ColumnElement) else col(query)


def directory_query_covers(older: Any, newer: Any):
    """接受运行实例或 ORM alias；供执行/发布和清理的同一 EXISTS 查询复用。

    tenant+advertiser 保留现有目录分区索引前缀。新查询的每个筛选维度必须
    覆盖旧查询；空数组表示全范围，不能以部分刷新淘汰完整请求。
    """
    old_query, new_query = _query(older), _query(newer)
    empty = literal([], type_=JSONB)
    coverage = []
    for key in ("ids", "parent_ids"):
        old_ids = func.coalesce(func.jsonb_extract_path(old_query, key), empty)
        new_ids = func.coalesce(func.jsonb_extract_path(new_query, key), empty)
        coverage.append(
            or_(new_ids == empty, and_(old_ids != empty, new_ids.op("@>")(old_ids)))
        )
    return and_(
        newer.tenant_id == older.tenant_id,
        newer.advertiser_id == older.advertiser_id,
        newer.bc_id == older.bc_id,
        newer.actor_id == older.actor_id,
        newer.frozen_route == older.frozen_route,
        newer.kind == older.kind,
        newer.ad_type == older.ad_type,
        ~old_query.op("?")(MATERIAL_CHILD_KEY),
        ~new_query.op("?")(MATERIAL_CHILD_KEY),
        or_(
            func.coalesce(
                func.jsonb_extract_path_text(old_query, "include_deleted"), "false"
            )
            != "true",
            func.jsonb_extract_path_text(new_query, "include_deleted") == "true",
        ),
        *coverage,
    )


def superseding_directory_predicate(older: Any, newer: Any):
    """只有较新且已发布的完整覆盖请求可以淘汰旧请求。"""
    return and_(
        directory_query_covers(older, newer),
        col(newer.request_sequence) > older.request_sequence,
        col(newer.published_version).is_not(None),
    )
