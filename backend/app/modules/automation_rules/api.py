"""自动规则 HTTP 接口。"""

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.modules.automation_rules import settings_service
from app.modules.automation_rules.schemas import (
    AutomationSettingsPublic,
    AutomationSettingsUpdate,
    FeishuTestRequest,
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
