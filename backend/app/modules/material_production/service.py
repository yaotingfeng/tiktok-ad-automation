"""联合开户事务与幂等外部执行；请求/密钥始终在租户加密信封内。"""

import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.core.security import get_password_hash, verify_password
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.models import User
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from app.modules.tenants.permissions import require_tenant
from app.modules.tenants.service import create_tenant, require_platform

from .client import KNOWN_ERRORS, MaterialToolClient, ToolError
from .models import MaterialProductionBinding
from .schemas import ProductionLoginLink, ProductionStatus

register_dispatch_task("material_production.provision", "control")
MAX_ATTEMPTS = 5


def provisioning_key(ref: str, fingerprint: str | None = None) -> str:
    key = settings.MATERIAL_TOOL_PROVISIONING_KEYS.get(ref, "")
    if not re.fullmatch(r"owp_[A-Za-z0-9_-]+", key) or (
        fingerprint is not None
        and not hmac.compare_digest(sha256(key.encode()).hexdigest(), fingerprint)
    ):
        raise DomainError(
            "material_tool_not_configured",
            "素材工具开户密钥未配置或已变更，请联系平台管理员",
        )
    return key


def _enqueue(session: Session, binding: MaterialProductionBinding) -> None:
    binding.dispatch_sequence += 1
    # 重投有独立代数；业务执行受绑定行锁与 READY 状态保护，不依赖 Celery 去重。
    enqueue_after_commit(
        session,
        context=TenantContext(binding.tenant_id, binding.actor_id, "platform_admin"),
        task_name="material_production.provision",
        task_key=f"material-production:{binding.tenant_id}:{binding.dispatch_sequence}",
        payload={
            "binding_id": str(binding.tenant_id),
            "sequence": binding.dispatch_sequence,
        },
    )
    binding.next_attempt_at = datetime.now(UTC) + timedelta(seconds=60)


def _onboarding_payload(name: str, username: str, password: str) -> tuple[str, str]:
    name = name.strip()
    if not name or len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise DomainError(
            "material_onboarding_invalid",
            "租户名称需为 1 至 100 个字符，不能包含控制字符",
        )
    if (
        not 8 <= len(password) <= 128
        or not re.search(r"[A-Za-z]", password)
        or not re.search(r"[0-9]", password)
    ):
        raise DomainError(
            "material_onboarding_invalid",
            "初始密码需为 8 至 128 位，同时包含字母和数字",
        )
    body = {
        "companyName": name,
        "username": username,
        "password": password,
        "uploadPlatform": "r2",
        "pushAdPlatform": True,
    }
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hmac.new(
        settings.SECRET_KEY.encode(), encoded.encode(), sha256
    ).hexdigest()
    return encoded, digest


def onboard_tenant(
    session: Session,
    *,
    actor_id: UUID,
    name: str,
    username: str,
    password: str,
    request_id: UUID,
) -> Tenant:
    require_platform(session, actor_id=actor_id)
    encoded, digest = _onboarding_payload(name, username, password)
    name = name.strip()
    # 同一提交可能因响应丢失重发；串行查验，先返回原租户，不重复创建本地用户。
    from sqlalchemy import func

    lock = int.from_bytes(
        sha256(f"material-onboarding:{request_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    session.exec(select(func.pg_advisory_xact_lock(lock))).one()
    existing = session.exec(
        select(MaterialProductionBinding).where(
            MaterialProductionBinding.request_id == request_id
        )
    ).first()
    if existing:
        if existing.actor_id != actor_id or not hmac.compare_digest(
            existing.request_digest, digest
        ):
            raise DomainError(
                "material_onboarding_conflict",
                "该操作编号已用于其他开户内容，请重新发起",
            )
        tenant = session.get(Tenant, existing.tenant_id)
        assert tenant is not None
        return tenant
    ref = settings.MATERIAL_TOOL_DEFAULT_KEY_REF
    key = provisioning_key(ref)
    settings.require_connection_encryption()
    if session.exec(select(User.id).where(User.username == username)).first():
        raise DomainError("username_exists", "登录账号已存在，请使用新的专属账号")
    admin = User(
        username=username, full_name=name, hashed_password=get_password_hash(password)
    )
    try:
        with session.begin_nested():
            session.add(admin)
            session.flush()
    except IntegrityError:
        raise DomainError(
            "username_exists", "登录账号已存在，请使用新的专属账号"
        ) from None
    tenant = create_tenant(
        session, actor_id=actor_id, name=name, administrator_id=admin.id
    )
    _save_binding(
        session,
        tenant=tenant,
        actor_id=actor_id,
        administrator_id=admin.id,
        request_id=request_id,
        username=username,
        ref=ref,
        key=key,
        encoded=encoded,
        digest=digest,
    )
    return tenant


def _save_binding(
    session: Session,
    *,
    tenant: Tenant,
    actor_id: UUID,
    administrator_id: UUID,
    request_id: UUID,
    username: str,
    ref: str,
    key: str,
    encoded: str,
    digest: str,
) -> MaterialProductionBinding:
    binding = MaterialProductionBinding(
        tenant_id=tenant.id,
        actor_id=actor_id,
        administrator_id=administrator_id,
        request_id=request_id,
        request_digest=digest,
        username=username,
        key_ref=ref,
        key_fingerprint=sha256(key.encode()).hexdigest(),
        request_ciphertext=encrypt_credentials(
            tenant_id=tenant.id, value={"request": encoded}
        ),
    )
    session.add(binding)
    session.flush()
    _enqueue(session, binding)
    session.flush()
    return binding


def onboard_existing_tenant(
    session: Session,
    *,
    actor_id: UUID,
    tenant_id: UUID,
    administrator_id: UUID,
    expected_name: str,
    expected_username: str,
    password: str | None = None,
    generate_password: bool = False,
) -> ProductionStatus:
    """仅供受控运维接入已授权历史租户；不修改投放用户、密码或成员关系。"""
    require_platform(session, actor_id=actor_id)
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="manage")
    tenant = session.exec(
        select(Tenant)
        .where(Tenant.id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    admin = session.get(User, administrator_id, populate_existing=True)
    member = session.get(
        TenantMembership, (tenant_id, administrator_id), populate_existing=True
    )
    if (
        tenant.name != expected_name
        or not admin
        or not admin.is_active
        or admin.username != expected_username
        or not member
        or not member.active
        or member.role != "tenant_admin"
    ):
        raise DomainError(
            "material_onboarding_conflict", "历史租户或管理员信息已变化，请重新核对"
        )
    if generate_password == (password is not None):
        raise DomainError(
            "material_onboarding_invalid", "须明确选择提供原密码或生成独立素材密码"
        )
    existing = session.get(MaterialProductionBinding, tenant_id, populate_existing=True)
    if existing:
        if (
            existing.administrator_id != administrator_id
            or existing.username != expected_username
        ):
            raise DomainError(
                "material_onboarding_conflict", "租户已经绑定其他素材账号"
            )
        # 重跑只读原记录，不生成新密码、不重放成功开户或覆盖已有 Key。
        return production_status(session, tenant_id=tenant_id, actor_id=actor_id)
    if password is not None and not verify_password(password, admin.hashed_password)[0]:
        raise DomainError("material_onboarding_invalid", "提供的密码与投放账号不一致")
    initial_password = (
        password if password is not None else secrets.token_urlsafe(32) + "a1"
    )
    encoded, digest = _onboarding_payload(tenant.name, admin.username, initial_password)
    ref = settings.MATERIAL_TOOL_DEFAULT_KEY_REF
    key = provisioning_key(ref)
    settings.require_connection_encryption()
    _save_binding(
        session,
        tenant=tenant,
        actor_id=actor_id,
        administrator_id=administrator_id,
        request_id=uuid5(
            NAMESPACE_URL, f"tkada:material-production:existing:{tenant_id}"
        ),
        username=admin.username,
        ref=ref,
        key=key,
        encoded=encoded,
        digest=digest,
    )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="material_production.onboarding",
            target_id=str(tenant_id),
            details={
                "username": admin.username,
                "password_mode": "generated" if generate_password else "matched",
            },
        )
    )
    session.flush()
    return production_status(session, tenant_id=tenant_id, actor_id=actor_id)


def provision_once(
    session: Session, *, tenant_id: UUID, actor_id: UUID, sequence: int
) -> None:
    binding = session.exec(
        select(MaterialProductionBinding)
        .where(MaterialProductionBinding.tenant_id == tenant_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).first()
    if (
        not binding
        or binding.status != "pending"
        or binding.actor_id != actor_id
        or binding.dispatch_sequence != sequence
        or binding.processed_sequence >= sequence
    ):
        return
    # 网络请求期间持有这一低频开户记录的锁，避免并发重放轮换已保存的 Key。
    # 进程中断时事务回滚，恢复扫描用同一密文和幂等键继续。
    binding.attempts += 1
    binding.processed_sequence = sequence
    binding.updated_at = datetime.now(UTC)
    try:
        require_platform(session, actor_id=actor_id)
        require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="manage")
        admin = session.get(User, binding.administrator_id, populate_existing=True)
        if not admin or not admin.is_active:
            raise DomainError("tenant_forbidden", "开户管理员已停用")
        key = provisioning_key(binding.key_ref, binding.key_fingerprint)
        assert binding.request_ciphertext is not None
        body = json.loads(
            decrypt_credentials(
                tenant_id=tenant_id, ciphertext=binding.request_ciphertext
            )["request"]
        )
        result = MaterialToolClient(key).provision(
            body, idempotency_key=f"tkada:{binding.request_id}"
        )
        binding.api_key_ciphertext = encrypt_credentials(
            tenant_id=tenant_id, value={"api_key": result.api_key}
        )
    except ToolError as error:
        binding.error_code = error.code
        binding.status = (
            "pending"
            if error.retryable and binding.attempts < MAX_ATTEMPTS
            else "failed"
        )
        binding.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=min(300, 30 * 2 ** min(binding.attempts, 4))
        )
    except DomainError as error:
        binding.status = "failed"
        binding.error_code = error.code
    else:
        binding.company_id = result.company_id
        binding.external_user_id = result.user_id
        binding.api_key_id = result.api_key_id
        binding.status = "ready"
        binding.error_code = None
        binding.request_ciphertext = None
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="material_production.provisioned",
                target_id=result.company_id,
                details={"username": binding.username},
            )
        )
    session.flush()


def recover_due(session: Session, *, limit: int = 50) -> int:
    bindings = session.exec(
        select(MaterialProductionBinding)
        .where(
            MaterialProductionBinding.status == "pending",
            col(MaterialProductionBinding.next_attempt_at) <= datetime.now(UTC),
        )
        .order_by(col(MaterialProductionBinding.next_attempt_at))
        .limit(min(max(limit, 1), 100))
        .with_for_update(skip_locked=True)
    ).all()
    for binding in bindings:
        _enqueue(session, binding)
    session.flush()
    return len(bindings)


def production_status(
    session: Session, *, tenant_id: UUID, actor_id: UUID
) -> ProductionStatus:
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="upload")
    binding = session.get(MaterialProductionBinding, tenant_id, populate_existing=True)
    if not binding:
        return ProductionStatus(status="not_configured")
    return ProductionStatus(
        status=cast(Literal["pending", "ready", "failed"], binding.status),
        username=binding.username,
        company_id=binding.company_id,
        attempts=binding.attempts,
        error_code=binding.error_code,
        message=KNOWN_ERRORS.get(
            binding.error_code or "", "开户未完成，请联系平台管理员"
        )
        if binding.error_code
        else None,
    )


def retry_provisioning(
    session: Session, *, tenant_id: UUID, actor_id: UUID
) -> ProductionStatus:
    require_platform(session, actor_id=actor_id)
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="manage")
    binding = session.exec(
        select(MaterialProductionBinding)
        .where(MaterialProductionBinding.tenant_id == tenant_id)
        .with_for_update()
    ).first()
    if not binding:
        raise DomainError("material_production_not_ready", "该租户尚未开通素材制作")
    if binding.status == "failed":
        provisioning_key(binding.key_ref, binding.key_fingerprint)
        binding.status, binding.error_code, binding.attempts = "pending", None, 0
        binding.actor_id = actor_id
        _enqueue(session, binding)
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="material_production.retry",
                target_id=str(tenant_id),
            )
        )
        session.flush()
    return production_status(session, tenant_id=tenant_id, actor_id=actor_id)


def create_login_link(
    session: Session, *, tenant_id: UUID, actor_id: UUID
) -> ProductionLoginLink:
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="upload")
    binding = session.get(MaterialProductionBinding, tenant_id, populate_existing=True)
    if not binding or binding.status != "ready":
        raise DomainError(
            "material_production_not_ready", "素材制作尚未开通，请联系平台管理员"
        )
    key = provisioning_key(binding.key_ref, binding.key_fingerprint)
    try:
        link = MaterialToolClient(key).login_link(binding.username)
    except ToolError as error:
        raise DomainError("material_tool_request_failed", str(error)) from None
    # 上游请求期间用户可能被撤权；返回凭证前重新核对，不把远端凭证记入审计。
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="upload")
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="material_production.login",
            target_id=binding.external_user_id or "",
            details={},
        )
    )
    session.flush()
    return ProductionLoginLink(login_url=link.login_url, expires_at=link.expires_at)
