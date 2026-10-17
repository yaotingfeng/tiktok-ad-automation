"""租户自动规则设置与飞书配置;Webhook 与密钥只以加密信封落库。"""

from datetime import UTC, datetime
from uuid import UUID

import httpx
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.modules.automation_rules import feishu
from app.modules.automation_rules.inputs import plan_next_run
from app.modules.automation_rules.models import (
    AutomationRule,
    AutomationTenantSetting,
    TenantFeishuSetting,
)
from app.modules.automation_rules.schemas import (
    AutomationSettingsPublic,
    AutomationSettingsUpdate,
    FeishuTestRequest,
    RuleDefinition,
)
from app.modules.tenants.permissions import require_tenant


def load_tenant_setting(session: Session, tenant_id: UUID) -> AutomationTenantSetting:
    return session.get(AutomationTenantSetting, tenant_id) or AutomationTenantSetting(
        tenant_id=tenant_id
    )


def load_feishu(session: Session, tenant_id: UUID) -> tuple[str, str] | None:
    row = session.get(TenantFeishuSetting, tenant_id)
    if row is None or not row.enabled:
        return None
    value = decrypt_credentials(tenant_id=tenant_id, ciphertext=row.ciphertext)
    return value["url"], value.get("secret", "")


def _public(session: Session, tenant_id: UUID) -> AutomationSettingsPublic:
    setting = load_tenant_setting(session, tenant_id)
    row = session.get(TenantFeishuSetting, tenant_id)
    return AutomationSettingsPublic(
        schedule_timezone=setting.schedule_timezone,
        paused=setting.paused,
        feishu_configured=row is not None,
        feishu_enabled=bool(row and row.enabled),
        feishu_url_hint=row.url_hint if row else None,
        feishu_has_secret=bool(row and row.has_secret),
    )


def get_settings(
    session: Session, *, context: TenantContext
) -> AutomationSettingsPublic:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    return _public(session, context.tenant_id)


def _reschedule_tenant(
    session: Session, tenant_id: UUID, timezone: str, now: datetime
) -> None:
    """运行时区变更后，按新时区重算所有启用规则的下一次运行时刻。"""
    rules = session.exec(
        select(AutomationRule).where(
            AutomationRule.tenant_id == tenant_id,
            col(AutomationRule.enabled).is_(True),
        )
    ).all()
    for rule in rules:
        rule.next_run_at = plan_next_run(
            session,
            tenant_id=tenant_id,
            definition=RuleDefinition.model_validate(rule.definition),
            rule_timezone=timezone,
            after=now,
        )


def update_settings(
    session: Session, *, context: TenantContext, body: AutomationSettingsUpdate
) -> AutomationSettingsPublic:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    now = datetime.now(UTC)
    setting = session.get(AutomationTenantSetting, context.tenant_id)
    if setting is None:
        setting = AutomationTenantSetting(tenant_id=context.tenant_id)
        session.add(setting)
    zone_changed = setting.schedule_timezone != body.schedule_timezone
    setting.schedule_timezone, setting.paused, setting.updated_at = (
        body.schedule_timezone,
        body.paused,
        now,
    )
    if zone_changed:
        _reschedule_tenant(session, context.tenant_id, body.schedule_timezone, now)
    row = session.get(TenantFeishuSetting, context.tenant_id)
    if body.clear_feishu:
        if row is not None:
            session.delete(row)
    elif body.webhook_url is not None:
        url = feishu.validate_webhook(body.webhook_url)
        secret = (body.secret or "").strip()
        ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value={"url": url, "secret": secret}
        )
        if row is None:
            row = TenantFeishuSetting(
                tenant_id=context.tenant_id,
                ciphertext=ciphertext,
                url_hint=feishu.url_hint(url),
                updated_by=context.actor_id,
            )
            session.add(row)
        row.ciphertext, row.url_hint, row.has_secret = (
            ciphertext,
            feishu.url_hint(url),
            bool(secret),
        )
    if row is not None and not body.clear_feishu:
        row.enabled, row.updated_by, row.updated_at = (
            body.feishu_enabled,
            context.actor_id,
            now,
        )
    session.flush()
    return _public(session, context.tenant_id)


def send_test(
    session: Session,
    *,
    context: TenantContext,
    body: FeishuTestRequest,
    transport: httpx.BaseTransport | None = None,
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if body.webhook_url:
        url, secret = (
            feishu.validate_webhook(body.webhook_url),
            (body.secret or "").strip(),
        )
    else:
        row = session.get(TenantFeishuSetting, context.tenant_id)
        if row is None:
            raise DomainError("feishu_not_configured", "尚未配置飞书 Webhook")
        value = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=row.ciphertext
        )
        url, secret = value["url"], value.get("secret", "")
    card = feishu.build_card(
        title="TK-ADA 自动规则测试消息",
        text="飞书通知已连通",
        link=_site_link(context.tenant_id),
        template="green",
    )
    feishu.send(url, secret, card, transport=transport)


def _site_link(tenant_id: UUID) -> str:
    from app.core.config import settings

    return f"{settings.FRONTEND_HOST.rstrip('/')}/tenants/{tenant_id}/automation-rules"
