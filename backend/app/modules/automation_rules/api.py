"""自动规则 HTTP 接口。"""

from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.modules.automation_rules import service, settings_service
from app.modules.automation_rules.schemas import (
    AutomationSettingsPublic,
    AutomationSettingsUpdate,
    CatalogPublic,
    DryRunPublic,
    DryRunRequest,
    FeishuTestRequest,
    RuleCreate,
    RuleEnabled,
    RulePage,
    RulePublic,
    RuleUpdate,
    RunDetail,
    RunPage,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["automation_rules"])


def _context(user: CurrentUser, tenant_id: UUID) -> TenantContext:
    # 权限在各服务函数内按动作重建;这里只组装请求范围。
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role="")


@router.get("/automation-settings", response_model=AutomationSettingsPublic)
def get_settings(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> AutomationSettingsPublic:
    return settings_service.get_settings(session, context=_context(user, tenant_id))


@router.put("/automation-settings", response_model=AutomationSettingsPublic)
def put_settings(
    tenant_id: UUID,
    body: AutomationSettingsUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> AutomationSettingsPublic:
    result = settings_service.update_settings(
        session, context=_context(user, tenant_id), body=body
    )
    session.commit()
    return result


@router.post("/automation-settings/test-feishu", status_code=204)
def test_feishu(
    tenant_id: UUID, body: FeishuTestRequest, session: SessionDep, user: CurrentUser
) -> None:
    settings_service.send_test(session, context=_context(user, tenant_id), body=body)


@router.get("/automation-rules/catalog", response_model=CatalogPublic)
def get_catalog(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> CatalogPublic:
    service.require(session, _context(user, tenant_id), "read")
    return service.catalog()


@router.get("/automation-rules", response_model=RulePage)
def list_rules(tenant_id: UUID, session: SessionDep, user: CurrentUser) -> RulePage:
    return service.list_rules(session, context=_context(user, tenant_id))


@router.post("/automation-rules", response_model=RulePublic, status_code=201)
def create_rule(
    tenant_id: UUID, body: RuleCreate, session: SessionDep, user: CurrentUser
) -> RulePublic:
    result = service.create_rule(session, context=_context(user, tenant_id), body=body)
    session.commit()
    return result


@router.post("/automation-rules/dry-run", response_model=DryRunPublic)
def dry_run(
    tenant_id: UUID, body: DryRunRequest, session: SessionDep, user: CurrentUser
) -> DryRunPublic:
    return service.dry_run(
        session, context=_context(user, tenant_id), definition=body.definition
    )


@router.get("/automation-rules/{rule_id}", response_model=RulePublic)
def get_rule(
    tenant_id: UUID, rule_id: UUID, session: SessionDep, user: CurrentUser
) -> RulePublic:
    return service.get_rule(session, context=_context(user, tenant_id), rule_id=rule_id)


@router.put("/automation-rules/{rule_id}", response_model=RulePublic)
def update_rule(
    tenant_id: UUID,
    rule_id: UUID,
    body: RuleUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> RulePublic:
    result = service.update_rule(
        session, context=_context(user, tenant_id), rule_id=rule_id, body=body
    )
    session.commit()
    return result


@router.post("/automation-rules/{rule_id}/enabled", response_model=RulePublic)
def set_enabled(
    tenant_id: UUID,
    rule_id: UUID,
    body: RuleEnabled,
    session: SessionDep,
    user: CurrentUser,
) -> RulePublic:
    result = service.set_enabled(
        session,
        context=_context(user, tenant_id),
        rule_id=rule_id,
        enabled=body.enabled,
    )
    session.commit()
    return result


@router.delete("/automation-rules/{rule_id}", status_code=204)
def delete_rule(
    tenant_id: UUID, rule_id: UUID, session: SessionDep, user: CurrentUser
) -> None:
    service.delete_rule(session, context=_context(user, tenant_id), rule_id=rule_id)
    session.commit()


@router.get("/automation-runs", response_model=RunPage)
def list_runs(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    rule_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RunPage:
    return service.list_runs(
        session,
        context=_context(user, tenant_id),
        rule_id=rule_id,
        limit=limit,
        offset=offset,
    )


@router.get("/automation-runs/{run_id}", response_model=RunDetail)
def get_run(
    tenant_id: UUID, run_id: UUID, session: SessionDep, user: CurrentUser
) -> RunDetail:
    return service.get_run(session, context=_context(user, tenant_id), run_id=run_id)
