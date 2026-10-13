"""自动规则 HTTP 接口。"""

from uuid import UUID

from fastapi import APIRouter

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
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["automation_rules"])


def _context(user: CurrentUser, tenant_id: UUID) -> TenantContext:
    # 权限在各服务函数内按动作重建;这里只组装请求范围。
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role="")


@router.get("/automation-settings", response_model=AutomationSettingsPublic)
def get_settings(tenant_id: UUID, session: SessionDep, user: CurrentUser):
    return settings_service.get_settings(session, context=_context(user, tenant_id))


@router.put("/automation-settings", response_model=AutomationSettingsPublic)
def put_settings(
    tenant_id: UUID,
    body: AutomationSettingsUpdate,
    session: SessionDep,
    user: CurrentUser,
):
    result = settings_service.update_settings(
        session, context=_context(user, tenant_id), body=body
    )
    session.commit()
    return result


@router.post("/automation-settings/test-feishu", status_code=204)
def test_feishu(
    tenant_id: UUID, body: FeishuTestRequest, session: SessionDep, user: CurrentUser
):
    settings_service.send_test(session, context=_context(user, tenant_id), body=body)


@router.get("/automation-rules/catalog", response_model=CatalogPublic)
def get_catalog(tenant_id: UUID, session: SessionDep, user: CurrentUser):
    service.require(session, _context(user, tenant_id), "read")
    return service.catalog()


@router.get("/automation-rules", response_model=RulePage)
def list_rules(tenant_id: UUID, session: SessionDep, user: CurrentUser):
    return service.list_rules(session, context=_context(user, tenant_id))


@router.post("/automation-rules", response_model=RulePublic, status_code=201)
def create_rule(
    tenant_id: UUID, body: RuleCreate, session: SessionDep, user: CurrentUser
):
    result = service.create_rule(session, context=_context(user, tenant_id), body=body)
    session.commit()
    return result


@router.post("/automation-rules/dry-run", response_model=DryRunPublic)
def dry_run(
    tenant_id: UUID, body: DryRunRequest, session: SessionDep, user: CurrentUser
):
    return service.dry_run(
        session, context=_context(user, tenant_id), definition=body.definition
    )


@router.get("/automation-rules/{rule_id}", response_model=RulePublic)
def get_rule(tenant_id: UUID, rule_id: UUID, session: SessionDep, user: CurrentUser):
    return service.get_rule(session, context=_context(user, tenant_id), rule_id=rule_id)


@router.put("/automation-rules/{rule_id}", response_model=RulePublic)
def update_rule(
    tenant_id: UUID,
    rule_id: UUID,
    body: RuleUpdate,
    session: SessionDep,
    user: CurrentUser,
):
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
):
    result = service.set_enabled(
        session,
        context=_context(user, tenant_id),
        rule_id=rule_id,
        enabled=body.enabled,
    )
    session.commit()
    return result


@router.delete("/automation-rules/{rule_id}", status_code=204)
def delete_rule(tenant_id: UUID, rule_id: UUID, session: SessionDep, user: CurrentUser):
    service.delete_rule(session, context=_context(user, tenant_id), rule_id=rule_id)
    session.commit()
