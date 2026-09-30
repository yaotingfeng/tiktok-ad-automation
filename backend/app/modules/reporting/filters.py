"""报表过滤器编译。

名称搜索始终作为参数化 ``ILIKE`` 条件生成，并指定反斜杠转义字符；因此
用户输入的 ``%``、``_`` 和反斜杠只表示字面量。账户集合通过当前 BC 的
``usable_grants`` 子查询约束，不能从目录对象或报告来源连接推断 BC。
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_
from sqlalchemy.sql import ColumnElement
from sqlmodel import Session, col

from app.core.context import TenantContext
from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.reporting.schemas import ReportingFilter
from app.modules.tenants.permissions import require_tenant

SUPPORTED_SORT_FIELDS = frozenset({"row_key", "name", "spend"})
UNSUPPORTED_FIELDS = {
    "budget_modes",
    "created_from",
    "created_to",
    "min_target_roas",
    "max_target_roas",
}


def _like_literal(value: str) -> str:
    """Escape SQL LIKE metacharacters while retaining parameter binding."""
    return "".join("\\" + char if char in "%_\\" else char for char in value)


@dataclass(frozen=True)
class CompiledFilter:
    dimension: str
    start_date: Any
    end_date: Any
    advertiser_ids: tuple[str, ...]
    ids: tuple[str, ...]
    keywords: tuple[str, ...]
    like_patterns: tuple[str, ...]
    ad_types: tuple[str, ...]
    operation_statuses: tuple[str, ...]
    review_statuses: tuple[str, ...]
    naming_status: str | None
    min_spend: Any
    max_spend: Any
    sort_by: str
    sort_direction: str

    def name_predicate(self, column: ColumnElement[Any]) -> ColumnElement[bool] | None:
        """Return an AND expression for all keywords, or ``None`` if unfiltered."""
        if not self.like_patterns:
            return None
        return and_(*(
            column.ilike(pattern, escape="\\") for pattern in self.like_patterns
        ))

    def apply(
        self,
        statement: Any,
        *,
        name_column: ColumnElement[Any] | None = None,
        advertiser_column: ColumnElement[Any] | None = None,
        remote_id_column: ColumnElement[Any] | None = None,
        ad_type_column: ColumnElement[Any] | None = None,
        operation_status_column: ColumnElement[Any] | None = None,
        review_status_column: ColumnElement[Any] | None = None,
        spend_column: ColumnElement[Any] | None = None,
    ) -> Any:
        """Apply local literal/id predicates to a SQLAlchemy statement.

        Authorization is intentionally separate: callers must use
        :func:`apply_authorized_scope` on every report or directory query.
        """
        predicates: list[ColumnElement[bool]] = []
        if name_column is not None:
            name_filter = self.name_predicate(name_column)
            if name_filter is not None:
                predicates.append(name_filter)
        if advertiser_column is not None and self.advertiser_ids:
            predicates.append(advertiser_column.in_(self.advertiser_ids))
        if remote_id_column is not None and self.ids:
            predicates.append(remote_id_column.in_(self.ids))
        if ad_type_column is not None and self.ad_types:
            predicates.append(ad_type_column.in_(self.ad_types))
        if operation_status_column is not None and self.operation_statuses:
            predicates.append(operation_status_column.in_(self.operation_statuses))
        if review_status_column is not None and self.review_statuses:
            predicates.append(review_status_column.in_(self.review_statuses))
        if spend_column is not None:
            if self.min_spend is not None:
                predicates.append(spend_column >= self.min_spend)
            if self.max_spend is not None:
                predicates.append(spend_column <= self.max_spend)
        return statement.where(*predicates)


def compile_filter(filters: ReportingFilter) -> CompiledFilter:
    if filters.sort_by not in SUPPORTED_SORT_FIELDS:
        raise ValueError("unsupported sort field")
    if filters.sort_direction not in {"asc", "desc"}:
        raise ValueError("unsupported sort direction")
    # These fields have no A fact/directory coordinate. Rejecting them keeps a
    # future API caller from silently querying fabricated economics.
    for field in UNSUPPORTED_FIELDS:
        value = getattr(filters, field)
        if value not in (None, (), ""):
            raise ValueError(f"unsupported report filter: {field}")
    if filters.min_d0_roas is not None or filters.max_d0_roas is not None:
        raise ValueError("d0 ROAS filtering is not supported by the published facts")
    query = filters.query or ""
    keywords = tuple(query.split())
    return CompiledFilter(
        dimension=filters.dimension,
        start_date=filters.start_date,
        end_date=filters.end_date,
        advertiser_ids=tuple(dict.fromkeys(filters.advertiser_ids)),
        ids=tuple(dict.fromkeys(filters.ids)),
        keywords=keywords,
        like_patterns=tuple(f"%{_like_literal(keyword)}%" for keyword in keywords),
        ad_types=filters.ad_types,
        operation_statuses=filters.operation_statuses,
        review_statuses=filters.review_statuses,
        naming_status=filters.naming_status,
        min_spend=filters.min_spend,
        max_spend=filters.max_spend,
        sort_by=filters.sort_by,
        sort_direction=filters.sort_direction,
    )


def apply_authorized_scope(
    session: Session,
    statement: Any,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_column: ColumnElement[Any],
) -> Any:
    """Constrain a report/directory query to the selected BC's usable grants."""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    grants = usable_grants(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        action="read",
    ).with_only_columns(BCAccountAccess.advertiser_id)
    return statement.where(advertiser_column.in_(grants))


def authorized_grants(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_ids: tuple[str, ...] = (),
) -> tuple[BCAccountAccess, ...]:
    """Read the selected BC grants after rebuilding tenant authority."""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    statement = usable_grants(
        tenant_id=context.tenant_id, bc_id=bc_id, action="read"
    )
    if advertiser_ids:
        statement = statement.where(col(BCAccountAccess.advertiser_id).in_(advertiser_ids))
    return tuple(session.exec(statement).all())
