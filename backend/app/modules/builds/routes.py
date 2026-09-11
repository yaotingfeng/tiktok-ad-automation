"""父路由只保存一次；延迟子任务只读取同一行，不能解析今天的默认连接。"""

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import Capability, verify_route
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.builds.route_models import BuildAttemptContext, BuildRouteContext
from app.modules.tenants.permissions import require_tenant


def inherit_route(
    parent: FrozenTikTokRoute, *, tenant_id: UUID, bc_id: str
) -> FrozenTikTokRoute:
    if (parent.tenant_id, parent.bc_id) != (tenant_id, bc_id):
        raise DomainError("frozen_route_scope_mismatch", "冻结执行连接范围不匹配")
    return parent


def _preview(
    session: Session, context: TenantContext, preview_id: UUID, *, lock: bool = False
) -> BuildPreview:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    query = select(BuildPreview).where(
        BuildPreview.tenant_id == context.tenant_id, BuildPreview.id == preview_id
    )
    if lock:
        query = query.with_for_update()
    # 只读父范围时不能刷新同事务正在推进的 JSON 游标；锁定写入时才重新装载。
    row = session.exec(query.execution_options(populate_existing=lock)).one_or_none()
    if row is None:
        raise DomainError("preview_not_found", "预览不存在")
    return row


def _route(row: BuildRouteContext) -> FrozenTikTokRoute:
    return FrozenTikTokRoute.model_validate(row.model_dump(exclude={"preview_id"}))


def save_preview_route(
    session: Session,
    *,
    context: TenantContext,
    preview_id: UUID,
    route: FrozenTikTokRoute,
) -> None:
    preview = _preview(session, context, preview_id, lock=True)
    inherit_route(route, tenant_id=context.tenant_id, bc_id=preview.bc_id)
    existing = session.get(
        BuildRouteContext, (context.tenant_id, preview_id), populate_existing=True
    )
    if existing is not None:
        if _route(existing) != route:
            raise DomainError("frozen_route_changed", "已冻结执行连接不能改变")
        return
    if preview.status != "BUILDING":
        raise DomainError("frozen_route_changed", "已完成预览不能补写执行连接")
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    session.add(BuildRouteContext(**route.model_dump(), preview_id=preview_id))
    session.flush()


def load_preview_route(
    session: Session, *, context: TenantContext, preview_id: UUID
) -> FrozenTikTokRoute:
    preview = _preview(session, context, preview_id)
    row = session.get(
        BuildRouteContext, (context.tenant_id, preview_id), populate_existing=True
    )
    if row is None:
        raise DomainError(
            "legacy_route_unverifiable", "旧预览缺少可核实的执行连接，请重新准备"
        )
    return inherit_route(_route(row), tenant_id=context.tenant_id, bc_id=preview.bc_id)


def stable_attempt_id(*, tenant_id: UUID, step_id: UUID, attempt: int) -> UUID:
    # 历史计数与新计数使用同一命名空间；不是上游幂等键，不能据此重发。
    if type(attempt) is not int or attempt < 0:
        raise ValueError("invalid attempt count")
    return uuid5(NAMESPACE_URL, f"tiktok-build-attempt:{tenant_id}:{step_id}:{attempt}")


def save_attempt_context(
    session: Session,
    *,
    step: ExecutionStep,
    attempt: int | None = None,
    expected_id: UUID | None = None,
) -> UUID:
    count = step.attempt if attempt is None else attempt
    identity = stable_attempt_id(
        tenant_id=step.tenant_id, step_id=step.id, attempt=count
    )
    if expected_id is not None and expected_id != identity:
        raise DomainError("execution_attempt_changed", "执行尝试身份不匹配")
    with session.no_autoflush:
        row = session.get(BuildAttemptContext, (step.tenant_id, step.id, count))
    if row is None:
        session.add(
            BuildAttemptContext(
                tenant_id=step.tenant_id,
                submission_id=step.submission_id,
                step_id=step.id,
                attempt=count,
                attempt_id=identity,
            )
        )
    elif row.submission_id != step.submission_id or row.attempt_id != identity:
        raise DomainError("execution_attempt_changed", "执行尝试身份不匹配")
    if count == step.attempt:
        step.attempt_id = identity
        session.add(step)
    return identity


def verify_unit_route(
    session: Session, *, context: TenantContext, unit: BuildUnit, capability: Capability
) -> FrozenTikTokRoute:
    route = load_preview_route(session, context=context, preview_id=unit.preview_id)
    inherit_route(route, tenant_id=unit.tenant_id, bc_id=unit.bc_id)
    if unit.connection_id != route.connection_id:
        raise DomainError("frozen_route_changed", "执行组合不属于冻结连接")
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=unit.advertiser_id,
        capability=capability,
    )
    return route
