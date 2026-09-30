"""操作者私有报表视图。

视图只保存筛选和列配置，不保存查询结果。每次读写都重新解析租户、BC
和账户授权，避免切换 BC 或撤权后复用旧偏好。
"""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.accounts.models import TenantBC
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.query_models import SavedReportView
from app.modules.reporting.schemas import ReportingFilter, SavedViewPublic
from app.modules.tenants.permissions import require_tenant

# 这些列均来自冻结快照的稳定显示/指标字段；服务端拒绝任意 JSON 键，避免
# 保存一个前端自定义字段后在导出或工作台中产生不一致的口径。
SUPPORTED_VIEW_COLUMNS = frozenset(
    {
        "row_key",
        "name",
        "advertiser_id",
        "status",
        "review_status",
        "ad_type",
        "currency",
        "timezone",
        "coverage",
        "spend",
        "native_growth_ad_revenue_value_d0",
        "d0_roas",
        "impressions",
        "clicks",
    }
)


def _authorize(session: Session, *, context: TenantContext, bc_id: str) -> None:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise HTTPException(403, detail="report_scope_forbidden")
    # authorized_grants 同时校验 BC 所属租户和当前授权状态。没有账户时也
    # 允许保存空报表的视图，但仍必须经过 BC 作用域检查。
    authorized_grants(session, context=context, bc_id=bc_id)


def validate_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    if type(columns) is not tuple:
        columns = tuple(columns)
    if not columns or len(set(columns)) != len(columns):
        raise HTTPException(422, detail="invalid_report_view_columns")
    if any(type(column) is not str or column not in SUPPORTED_VIEW_COLUMNS for column in columns):
        raise HTTPException(422, detail="unsupported_report_view_column")
    return columns


def _public(row: SavedReportView) -> SavedViewPublic:
    return SavedViewPublic(
        id=row.id,
        name=row.name,
        filters=ReportingFilter.model_validate(row.filters),
        columns=tuple(row.columns),
        created_at=row.created_at,
    )


def save_view(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    name: str,
    filters: ReportingFilter,
    columns: tuple[str, ...],
) -> SavedViewPublic:
    _authorize(session, context=context, bc_id=bc_id)
    if type(name) is not str or not name.strip():
        raise HTTPException(422, detail="invalid_report_view_name")
    try:
        compile_filter(filters)
    except ValueError as exc:
        raise HTTPException(422, detail="invalid_reporting_filter") from exc
    columns = validate_columns(columns)
    row = SavedReportView(
        id=uuid4(),
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        name=name.strip(),
        filters=filters.model_dump(mode="json"),
        columns=list(columns),
    )
    session.add(row)
    session.flush()
    return _public(row)


def list_views(
    session: Session, *, context: TenantContext, bc_id: str
) -> tuple[SavedViewPublic, ...]:
    _authorize(session, context=context, bc_id=bc_id)
    rows = session.exec(
        select(SavedReportView)
        .where(
            SavedReportView.tenant_id == context.tenant_id,
            SavedReportView.bc_id == bc_id,
            SavedReportView.actor_id == context.actor_id,
        )
        .order_by(col(SavedReportView.created_at), col(SavedReportView.id))
    ).all()
    return tuple(_public(row) for row in rows)


def get_view(
    session: Session, *, context: TenantContext, bc_id: str, view_id: UUID
) -> SavedReportView:
    _authorize(session, context=context, bc_id=bc_id)
    row = session.get(SavedReportView, view_id, populate_existing=True)
    if row is None or (
        row.tenant_id != context.tenant_id
        or row.bc_id != bc_id
        or row.actor_id != context.actor_id
    ):
        raise HTTPException(404, detail="report_view_not_found")
    return row


def update_view(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    view_id: UUID,
    name: str | None = None,
    filters: ReportingFilter | None = None,
    columns: tuple[str, ...] | None = None,
) -> SavedViewPublic:
    row = get_view(session, context=context, bc_id=bc_id, view_id=view_id)
    if name is not None:
        if type(name) is not str or not name.strip():
            raise HTTPException(422, detail="invalid_report_view_name")
        row.name = name.strip()
    if filters is not None:
        try:
            compile_filter(filters)
        except ValueError as exc:
            raise HTTPException(422, detail="invalid_reporting_filter") from exc
        row.filters = filters.model_dump(mode="json")
    if columns is not None:
        row.columns = list(validate_columns(columns))
    session.add(row)
    session.flush()
    return _public(row)


def delete_view(
    session: Session, *, context: TenantContext, bc_id: str, view_id: UUID
) -> None:
    row = get_view(session, context=context, bc_id=bc_id, view_id=view_id)
    session.delete(row)
    session.flush()


__all__ = [
    "SUPPORTED_VIEW_COLUMNS",
    "delete_view",
    "get_view",
    "list_views",
    "save_view",
    "update_view",
    "validate_columns",
]
