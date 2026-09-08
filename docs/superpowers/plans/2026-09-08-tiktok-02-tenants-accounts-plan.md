# 租户、权限与 TikTok 广告账户 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 执行方式沿用用户按需使用 Superpowers 的偏好，不要求额外启动整套流程。

**Goal:** 实现可审计的租户切换、固定角色、独立 TikTok 授权、完整账户目录、批量账户解析和系统上传账户分配。

**Architecture:** 沿用基础计划建立的 FastAPI、SQLModel/SQLAlchemy、Alembic 与模板登录。租户业务接口校验路径归属，后台从数据库重建权限；TikTok 通信直接调用固定版本官方 Python SDK。账户目录、访问权限和外部资产归属分别记录，OAuth 使用一次性 state 与候选凭据，避免错误租户绑定和重新授权覆盖仍可用凭据。

**Tech Stack:** Python 3.14、FastAPI、PostgreSQL 18、SQLModel、SQLAlchemy、Alembic、Fernet、Celery、官方 `python_sdk` 发行包／`business_api_client` 导入、React、TypeScript、shadcn/ui、TanStack Query/Router、Bun、Playwright。

**Spec:** [租户与账户设计](../specs/2026-09-08-tiktok-01-tenants-accounts-design.md)、[整体设计](../specs/2026-09-08-tiktok-00-overall-design.md)。本计划在六份设计获得批准后编写，草案中的具体建议已作为本轮执行依据。

## Global Constraints

- “平台管理员切换租户后复用投放页面；租户内部不按投手划分账户范围；单次搭建限定一个租户和一个 BC。”
- “TikTok ID 按字符串处理，避免 JavaScript 数值精度丢失。”
- “账户可用范围是‘当前租户当前 BC 资产’与‘该租户有效连接授权账户’的交集。”
- “目录仍保留可见账户及冲突状态”；冲突只阻止业务操作，不能从目录静默略过。
- “暂停仅影响尚未执行的后台步骤，不自动暂停或删除已在 TikTok 创建并启用的广告。”
- “用户无需配置固定素材账户”；每次上传记录实际账户，不引入租户素材账户设置表单。
- “资产发现使用分页任务和持久化进度，支持恢复中断”；局部发现失败不能将未扫描到的账户标为移除。
- 统一业务前缀 `/api/tenants/{tenant_id}/`，平台 `/api/platform/`，回调 `/api/integrations/tiktok/callback`；不新建 `/api/v1` 路由。
- 未来 `APP_ROOT=/Users/yaotingfeng/Documents/ytf/tiktok-ad-automation`；本计划所有代码路径相对 APP_ROOT。本轮只写计划，不创建 APP_ROOT、不运行计划中的实现或迁移命令。
- 先完成 `01-foundation`：提供模板登录、专用本地测试 PostgreSQL、SDK 锁定、上下文、错误、分页和事务 outbox。固定模板 revision `cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7`，固定 SDK revision `f809c396520df2d7b201a9ccc5378d822b728ed3`。
- 保留模板 `app.models.User` 的 `is_active/is_superuser` 与 `app.api.deps.CurrentUser/SessionDep`。运行命令从 APP_ROOT 开始，后端用 `uv`，前端用 `bun`。
- 本计划测试只用本地数据库与 SDK fake，不发起真实 TikTok 请求。SDK 与 Python 3.14 兼容性由基础计划实际验证；授权门户及真实资产字段由应用获批后的联调验收确认。
- 每次 TikTok SDK 外部调用均先消费 P01 Task 7 的 `app/jobs/admission.py` 共享准入；应用范围用实际 `TIKTOK_APP_ID`，不能用 connection_id 将平台额度拆散。Redis 不可用时不放行，拒绝准入不算远端调用失败。
- 报表同步、收入分析、Minis/Identity/CTA 广告产品场景解析不归本模块；后者由广告执行计划通过同一 SDK 和账户权限契约完成。

---

## 文件分工与公共依赖

| 路径 | 职责 |
| --- | --- |
| `backend/app/modules/tenants/{models,permissions,service,router}.py` | 租户、成员、审计、固定角色与管理接口 |
| `backend/app/modules/accounts/{models,schemas,discovery,access,resolver,router,tasks}.py` | 连接、BC、账户目录、发现步骤、权限、解析和任务入口 |
| `backend/app/integrations/tiktok/{sdk,auth,accounts}.py` | 官方 SDK 请求域、授权交换、账户读取；不建 HTTP 网关 |
| `backend/app/core/credentials.py` | 按租户绑定的凭据加解密，供版权方模块复用 |
| `backend/app/alembic/versions/` | 租户与账户结构迁移；执行时以前一个计划的实际 head 为父版本 |
| `frontend/src/features/{tenants,accounts}/` | 管理页、租户上下文、连接状态、目录与批量解析组件 |
| `frontend/src/routes/_layout/` | 租户及账户页面入口，复用模板认证布局 |
| `backend/tests/modules/{tenants,accounts}/`、`frontend/tests/tenants-accounts.spec.ts` | 隔离、回调、目录恢复、权限和交互回归 |

基础计划已提供以下定义，禁止在本计划重新声明同名公共类型：

```text
# app.core.context
@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    actor_id: UUID
    role: str

# app.core.errors
DomainError(code: str, message: str, retryable: bool = False)
# app.core.pagination
Page[T](items: list[T], next_cursor: str | None)
# app.jobs.outbox：只加入当前事务，不自行提交、不直接投递 Celery
enqueue_after_commit(session: sqlmodel.Session, *, context: TenantContext,
                     task_name: str, task_key: str, payload: dict) -> UUID
# app.jobs.admission：公共实现由 P01 Task 7 提供，02 只消费
admit_call(redis_client, *, app_scope: str, endpoint: str, tenant_id: UUID,
           advertiser_id: str, lease_id: UUID, policy: AdmissionPolicy) -> Admission
release_call(redis_client, *, app_scope: str, endpoint: str, tenant_id: UUID,
             advertiser_id: str, lease_id: UUID) -> None
Admission(granted: bool, retry_after_ms: int)
AdmissionPolicy(app_max_inflight: int, endpoint_max_inflight: int,
                tenant_max_inflight: int, advertiser_max_inflight: int,
                app_calls_per_window: int, endpoint_calls_per_window: int,
                window_ms: int, lease_ms: int)
```

本计划导出到后续计划的精确契约：

```text
# app.modules.tenants.permissions
Action = Literal["read", "manage", "build", "upload", "strategy_write", "provider_write"]
Role = Literal["platform_admin", "tenant_admin", "operator", "viewer"]
require_tenant(session: Session, *, actor_id: UUID, tenant_id: UUID,
               action: str) -> TenantContext

# app.modules.accounts.schemas / access
class AccountAccess(BaseModel):
    advertiser_id: str
    bc_id: str
    connection_id: UUID
    currency: str
    timezone: str

resolve_account_access(session: Session, *, context: TenantContext,
                       bc_id: str, advertiser_id: str, action: str) -> AccountAccess
assign_upload_account(session: Session, *, context: TenantContext,
                      bc_id: str) -> AccountAccess

# app.integrations.tiktok.sdk
sdk_client(session: Session, *, context: TenantContext,
           connection_id: UUID) -> ContextManager[business_api_client.ApiClient]

# app.core.credentials
encrypt_credentials(*, tenant_id: UUID, value: dict[str, str]) -> str
decrypt_credentials(*, tenant_id: UUID, ciphertext: str) -> dict[str, str]
```

上述代码块是契约索引；可执行实现和测试在各 Task 中。所有 fixture、函数与状态必须采用下文给出的名字。

### Task 1: 建立租户模型与数据库驱动的固定权限

**Files:**
- Create: `backend/app/modules/tenants/models.py`
- Create: `backend/app/modules/tenants/permissions.py`
- Create: `backend/app/alembic/versions/02a_tenants.py`
- Create: `backend/tests/modules/conftest.py`
- Test: `backend/tests/modules/tenants/test_permissions.py`
- Modify: `backend/app/alembic/env.py`，导入本模块模型元数据。

**Interfaces:**
- Consumes: `TenantContext`、`DomainError`、模板 `User`、基础计划 `session` fixture。
- Produces: `Tenant`、`TenantMembership`、`AuditEvent`；公共 `Action/Role/require_tenant`。
- Produces test fixtures: `context: TenantContext`、`other_context: TenantContext`；插入真实租户、用户与 operator 成员；复用基础计划 session，不重新定义 session。

- [ ] **Step 1: 写角色撤销、跨租户和平台代投回归。** 将下面的测试及 fixture 加入指定文件。

```python
# tests/modules/conftest.py
import pytest
from uuid import uuid4
from app.models import User
from app.core.context import TenantContext
from app.modules.tenants.models import Tenant, TenantMembership

def create_context(session, *, role="operator"):
    user = User(email=f"{uuid4()}@example.test", hashed_password="unused")
    tenant = Tenant(name=f"test-{uuid4()}")
    session.add_all([user, tenant]); session.flush()
    session.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role=role))
    session.flush()
    return TenantContext(tenant_id=tenant.id, actor_id=user.id, role=role)

@pytest.fixture
def context(session):
    return create_context(session)

@pytest.fixture
def other_context(session):
    return create_context(session)

# tests/modules/tenants/test_permissions.py
from uuid import uuid4
import pytest
from app.models import User
from app.core.errors import DomainError
from app.modules.tenants.models import Tenant, TenantMembership
from app.modules.tenants.permissions import require_tenant

def test_scope_and_role_are_reloaded(session):
    actor = User(id=uuid4(), email=f"{uuid4()}@example.test", hashed_password="unused")
    own, other = Tenant(name="own"), Tenant(name="other")
    session.add_all([actor, own, other])
    session.flush()
    membership = TenantMembership(tenant_id=own.id, user_id=actor.id, role="operator")
    session.add(membership)
    session.flush()
    assert require_tenant(session, actor_id=actor.id, tenant_id=own.id, action="build").role == "operator"
    with pytest.raises(DomainError) as cross:
        require_tenant(session, actor_id=actor.id, tenant_id=other.id, action="read")
    assert cross.value.code == "tenant_forbidden"
    membership.active = False
    session.flush()
    with pytest.raises(DomainError):
        require_tenant(session, actor_id=actor.id, tenant_id=own.id, action="build")

@pytest.mark.parametrize("action", ["build", "upload", "manage", "provider_write", "strategy_write"])
def test_viewer_cannot_write(session, action):
    actor = User(email=f"{uuid4()}@example.test", hashed_password="unused")
    tenant = Tenant(name="viewer-tenant")
    session.add_all([actor, tenant]); session.flush()
    session.add(TenantMembership(tenant_id=tenant.id, user_id=actor.id, role="viewer"))
    session.flush()
    with pytest.raises(DomainError) as error:
        require_tenant(session, actor_id=actor.id, tenant_id=tenant.id, action=action)
    assert error.value.code == "action_forbidden"
```

- [ ] **Step 2: 在专用测试库迁移后运行失败测试。**

```bash
cd backend
uv run pytest tests/modules/tenants/test_permissions.py -q
```

预期：首次因新模块未存在而失败；不能连接生产数据库，不能调用 TikTok。

- [ ] **Step 3: 实现模型与权限表，生成并检查迁移。**

```python
# modules/tenants/models.py
from uuid import UUID, uuid4
from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, Column, JSON
from sqlmodel import SQLModel, Field

class Tenant(SQLModel, table=True):
    __tablename__ = "tenant"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=120)
    active: bool = True

class TenantMembership(SQLModel, table=True):
    __tablename__ = "tenant_membership"
    __table_args__ = (CheckConstraint("role IN ('tenant_admin','operator','viewer')"),)
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", primary_key=True)
    role: str
    active: bool = True

class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_event"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_id: UUID = Field(foreign_key="user.id")
    action: str
    target_id: str
    details: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

```python
# modules/tenants/permissions.py
from typing import Literal
from uuid import UUID
from sqlmodel import Session
from app.models import User
from app.core.context import TenantContext
from app.core.errors import DomainError
from .models import Tenant, TenantMembership

Action = Literal["read", "manage", "build", "upload", "strategy_write", "provider_write"]
Role = Literal["platform_admin", "tenant_admin", "operator", "viewer"]
ALL_ACTIONS = {"read", "manage", "build", "upload", "strategy_write", "provider_write"}
PERMISSIONS = {
    "platform_admin": ALL_ACTIONS,
    "tenant_admin": ALL_ACTIONS,
    "operator": ALL_ACTIONS - {"manage"},
    "viewer": {"read"},
}

def require_tenant(session: Session, *, actor_id: UUID, tenant_id: UUID, action: str) -> TenantContext:
    if action not in ALL_ACTIONS:
        raise DomainError("unknown_action", "未定义的业务动作")
    user = session.get(User, actor_id, populate_existing=True)
    tenant = session.get(Tenant, tenant_id, populate_existing=True)
    if not user or not user.is_active or not tenant or not tenant.active:
        raise DomainError("tenant_forbidden", "无法进入该租户")
    member = session.get(TenantMembership, (tenant_id, actor_id), populate_existing=True)
    if user.is_superuser:
        role = "platform_admin"
    elif member and member.active:
        role = member.role
    else:
        raise DomainError("tenant_forbidden", "无法进入该租户")
    if action not in PERMISSIONS.get(role, set()):
        raise DomainError("action_forbidden", "当前角色不能执行此操作")
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, role=role)
```

迁移包含上述三个表、组合主键和角色 CHECK；引用模板 `user` 表。连接和账户表在 Task 3/4 添加，不把所有模型挤入一个文件。

- [ ] **Step 4: 运行迁移、上述回归并加一条平台管理员不需要成员记录的测试。** 测试将 User 的 `is_superuser=True`，断言返回 context.actor_id 仍为该真实管理员；再将 user.is_active=False，断言拒绝。

```bash
uv run alembic upgrade head
uv run pytest tests/modules/tenants/test_permissions.py -q
```

预期：所有角色测试通过；迁移只有新增表，无删除模板用户数据。

- [ ] **Step 5: 提交独立交付。** `git add backend/app/modules/tenants backend/app/alembic backend/tests/modules`，`git commit -m "tenants: enforce tenant roles and live permission checks"`。命令从 APP_ROOT 执行，仅在基础计划已建立的新应用仓库提交。

### Task 2: 完成租户、成员管理和审计接口

**Files:**
- Create: `backend/app/modules/tenants/service.py`
- Create: `backend/app/modules/tenants/router.py`
- Modify: `backend/app/api/main.py`
- Test: `backend/tests/modules/tenants/test_management.py`

**Interfaces:**
- Consumes: `require_tenant`、`CurrentUser/SessionDep`、Task 1 模型。
- Produces: `create_tenant(session, *, actor_id: UUID, name: str, administrator_id: UUID) -> Tenant`。
- Produces: `set_member(session, *, context: TenantContext, user_id: UUID, role: Role, active: bool) -> TenantMembership`。
- Produces HTTP: `GET /api/me/tenants`、`POST /api/platform/tenants`、`PATCH /api/platform/tenants/{tenant_id}`、`GET/PUT /api/tenants/{tenant_id}/members`；返回模型排除密码与凭据。

- [ ] **Step 1: 写平台开通和真实操作人审计测试。**

```python
from uuid import uuid4
import pytest
from sqlmodel import select
from app.models import User
from app.modules.tenants.models import AuditEvent, TenantMembership
from app.modules.tenants.service import create_tenant, set_member
from app.modules.tenants.permissions import require_tenant
from app.core.errors import DomainError

def test_platform_creates_tenant_and_keeps_actor(session):
    platform = User(email=f"{uuid4()}@example.test", hashed_password="unused", is_superuser=True)
    admin = User(email=f"{uuid4()}@example.test", hashed_password="unused")
    session.add_all([platform, admin]); session.flush()
    tenant = create_tenant(session, actor_id=platform.id, name="demo", administrator_id=admin.id)
    assert session.get(TenantMembership, (tenant.id, admin.id)).role == "tenant_admin"
    event = session.exec(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)).one()
    assert event.actor_id == platform.id
    context = require_tenant(session, actor_id=admin.id, tenant_id=tenant.id, action="manage")
    with pytest.raises(DomainError) as error:
        set_member(session, context=context, user_id=admin.id, role="viewer", active=True)
    assert error.value.code == "last_tenant_admin"
```

- [ ] **Step 2: 执行 `uv run pytest tests/modules/tenants/test_management.py -q`。** 预期 service 不存在导致失败。

- [ ] **Step 3: 实现事务内管理与最后管理员约束。** 核心服务如下；调用者提交事务，函数本身只 flush。

```python
# modules/tenants/service.py
from uuid import UUID
from sqlmodel import Session, select
from app.models import User
from app.core.context import TenantContext
from app.core.errors import DomainError
from .models import Tenant, TenantMembership, AuditEvent
from .permissions import Role, require_tenant

def create_tenant(session: Session, *, actor_id: UUID, name: str, administrator_id: UUID) -> Tenant:
    actor, admin = session.get(User, actor_id), session.get(User, administrator_id)
    if not actor or not actor.is_active or not actor.is_superuser:
        raise DomainError("platform_forbidden", "仅平台管理员可创建租户")
    if not admin or not admin.is_active or not name.strip():
        raise DomainError("invalid_tenant", "名称与管理员必须有效")
    tenant = Tenant(name=name.strip())
    session.add(tenant); session.flush()
    session.add(TenantMembership(tenant_id=tenant.id, user_id=administrator_id, role="tenant_admin"))
    session.add(AuditEvent(tenant_id=tenant.id, actor_id=actor_id, action="tenant.create", target_id=str(tenant.id)))
    session.flush()
    return tenant

def set_member(session: Session, *, context: TenantContext, user_id: UUID, role: Role, active: bool) -> TenantMembership:
    context = require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage")
    tenant = session.exec(select(Tenant).where(Tenant.id == context.tenant_id).with_for_update()).one()
    user = session.get(User, user_id)
    if role not in {"tenant_admin", "operator", "viewer"} or not user or not user.is_active:
        raise DomainError("invalid_member", "成员和租户角色必须有效")
    member = session.get(TenantMembership, (tenant.id, user_id))
    if member and member.active and member.role == "tenant_admin" and (not active or role != "tenant_admin"):
        admins = session.exec(select(TenantMembership).where(TenantMembership.tenant_id == tenant.id,
            TenantMembership.active == True, TenantMembership.role == "tenant_admin")).all()
        if len(admins) == 1:
            raise DomainError("last_tenant_admin", "先设置其他租户管理员")
    member = member or TenantMembership(tenant_id=tenant.id, user_id=user_id, role=role)
    member.role, member.active = role, active
    session.add(member)
    session.add(AuditEvent(tenant_id=tenant.id, actor_id=context.actor_id, action="member.set",
                          target_id=str(user_id), details={"role": role, "active": active}))
    session.flush()
    return member
```

- [ ] **Step 4: 接入薄路由和列表分页。** 路由在 `/api` router 下挂载相对路径；以下是一条真实路由实现模式，不把前端 tenant_id 当成授权。

```python
from uuid import UUID
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.api.deps import CurrentUser, SessionDep
from .models import Tenant
from .service import create_tenant

router = APIRouter()

class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    administrator_id: UUID

@router.post("/platform/tenants", response_model=Tenant, status_code=201)
def post_tenant(body: TenantCreate, session: SessionDep, user: CurrentUser):
    result = create_tenant(session, actor_id=user.id, **body.model_dump())
    session.commit()
    return result
```

其余三条管理路由按 Interfaces 明确实现：租户改名/停用仅平台管理员；成员列表/修改仅当前租户 `manage`；`/me/tenants` 只列有效成员关系，平台管理员列所有租户。列表都接受 `after_id: UUID | None, limit: int=50`，服务端限制 1～200，按 UUID 升序 seek，返回 `Page`。停用不调用任何 TikTok 接口。

- [ ] **Step 5: 运行管理回归及 OpenAPI 检查后提交。** 增加 API 测试：普通成员创建租户返回 403，平台成功为 201；请求及响应中不存在 `hashed_password`；成员修改产生同租户 AuditEvent。运行 `uv run pytest tests/modules/tenants -q`，预期全部通过。提交主题 `tenants: add platform and member administration`。

### Task 3: 完成租户绑定的 OAuth、凭据存储与 SDK 请求域

**Files:**
- Create: `backend/app/core/credentials.py`
- Create: `backend/app/modules/accounts/models.py`
- Create: `backend/app/integrations/tiktok/sdk.py`
- Create: `backend/app/integrations/tiktok/auth.py`
- Create: `backend/app/alembic/versions/02b_tiktok_authorization.py`
- Modify: `backend/app/core/config.py`、`backend/pyproject.toml`、`backend/uv.lock`
- Test: `backend/tests/modules/accounts/test_oauth.py`、`backend/tests/modules/accounts/test_credentials.py`、`backend/tests/modules/accounts/test_admission.py`

**Interfaces:**
- Consumes: `settings.CONNECTION_ENCRYPTION_KEY`、`require_tenant`、`enqueue_after_commit`；P01 `AdmissionPolicy/Admission/admit_call/release_call` 和同一 Redis 服务的客户端。
- Produces: `encrypt_credentials/decrypt_credentials`、`sdk_client`、`checked_data(response: object) -> dict`。
- Produces: `admitted_account_call(redis_client, *, context:TenantContext, endpoint:str, advertiser_id:str, policy:AdmissionPolicy) -> ContextManager[None]`；只消费 P01，不实现 Redis 限流算法。
- Produces: `start_authorization(session, *, context: TenantContext, connection_id: UUID | None) -> str`，返回授权 URL。
- Produces: `claim_authorization(session, *, state: str, now: datetime) -> AuthorizationAttempt`，原子消耗 state。
- Produces: `finish_authorization(session, *, state: str, auth_code: str, now: datetime, redis_client, policy:AdmissionPolicy) -> UUID`，返回明确归属的连接 ID。

**新增模型：** `TikTokConnection(id UUID, tenant_id UUID, status str, credential_ciphertext str|null, credential_version int=0)`；`AuthorizationAttempt(id UUID, tenant_id UUID, actor_id UUID, connection_id UUID, state_hash str UNIQUE, expires_at timestamptz, claimed_at timestamptz|null, status str, candidate_ciphertext str|null)`。连接设置 `(tenant_id,id)` 唯一约束，Attempt 使用 `(tenant_id,connection_id)` 组合外键；重新授权保留原连接版本。连接列表响应绝不暴露两个 ciphertext 字段。

- [ ] **Step 1: 写跨租户密文与重复回调回归。**

```python
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4
import pytest
from app.core.credentials import encrypt_credentials, decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.auth import claim_authorization
from app.modules.accounts.models import AuthorizationAttempt

def test_credentials_cannot_cross_tenant():
    owner = uuid4()
    encoded = encrypt_credentials(tenant_id=owner, value={"access_token": "fake-only"})
    assert "fake-only" not in encoded
    assert decrypt_credentials(tenant_id=owner, ciphertext=encoded)["access_token"] == "fake-only"
    with pytest.raises(DomainError) as error:
        decrypt_credentials(tenant_id=uuid4(), ciphertext=encoded)
    assert error.value.code == "credential_tenant_mismatch"

def test_state_is_claimed_once(session, auth_attempt):
    now = datetime.now(timezone.utc)
    first = claim_authorization(session, state="fake-state", now=now)
    assert first.id == auth_attempt.id
    with pytest.raises(DomainError) as error:
        claim_authorization(session, state="fake-state", now=now)
    assert error.value.code == "invalid_oauth_state"
```

本文件的 `auth_attempt` fixture 创建有效 User、Tenant、管理员成员、PENDING_AUTH 连接，再添加 `AuthorizationAttempt(state_hash=sha256(b"fake-state").hexdigest(), expires_at=now+timedelta(minutes=10), status="PENDING")` 并 flush；这些字段均在本任务定义。另参数化过期时间、错误 state 和管理员被停用场景。

```python
@pytest.fixture
def auth_attempt(session, context):
    from app.modules.tenants.models import TenantMembership
    from app.modules.accounts.models import TikTokConnection
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    connection = TikTokConnection(tenant_id=context.tenant_id, status="PENDING_AUTH")
    session.add(connection); session.flush()
    attempt = AuthorizationAttempt(tenant_id=context.tenant_id, actor_id=context.actor_id,
        connection_id=connection.id, state_hash=sha256(b"fake-state").hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), status="PENDING")
    session.add(attempt); session.flush()
    return attempt
```

- [ ] **Step 2: 运行 `uv run pytest tests/modules/accounts/test_credentials.py tests/modules/accounts/test_oauth.py -q`。** 预期缺少新模块而失败。Fernet 与 SDK 已由基础计划安装并锁定，不重复替换依赖。

- [ ] **Step 3: 实现租户密文与独立 SDK 客户端。**

```python
# core/credentials.py
import json
from uuid import UUID
from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings
from app.core.errors import DomainError

def encrypt_credentials(*, tenant_id: UUID, value: dict[str, str]) -> str:
    body = json.dumps({"tenant_id": str(tenant_id), "value": value}).encode()
    return Fernet(settings.CONNECTION_ENCRYPTION_KEY.encode()).encrypt(body).decode()

def decrypt_credentials(*, tenant_id: UUID, ciphertext: str) -> dict[str, str]:
    try:
        data = json.loads(Fernet(settings.CONNECTION_ENCRYPTION_KEY.encode()).decrypt(ciphertext.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise DomainError("credential_invalid", "凭据无法解密") from None
    if not isinstance(data, dict):
        raise DomainError("credential_invalid", "凭据格式无效")
    if data.get("tenant_id") != str(tenant_id):
        raise DomainError("credential_tenant_mismatch", "凭据不属于当前租户")
    value = data.get("value")
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise DomainError("credential_invalid", "凭据格式无效")
    return value
```

```python
# integrations/tiktok/sdk.py
from contextlib import contextmanager
from typing import Iterator
from uuid import UUID
import business_api_client
from sqlmodel import Session
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.credentials import decrypt_credentials
from app.modules.accounts.models import TikTokConnection
from app.modules.tenants.permissions import require_tenant

def checked_data(response: object) -> dict:
    raw = response.to_dict()
    if raw.get("code") != 0 or not isinstance(raw.get("data"), dict):
        raise DomainError("tiktok_response_error", "TikTok 返回错误或不支持的数据结构")
    return raw["data"]

@contextmanager
def sdk_client(session: Session, *, context: TenantContext, connection_id: UUID) -> Iterator[business_api_client.ApiClient]:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if not connection or connection.tenant_id != context.tenant_id or connection.status != "ACTIVE":
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    token = decrypt_credentials(tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext)["access_token"]
    client = business_api_client.ApiClient(header_name="Access-Token", header_value=token)
    client.configuration.debug = False
    try:
        yield client
    finally:
        client.default_headers.pop("Access-Token", None)
        client.rest_client.pool_manager.clear()
        client.pool.close()
        client.pool.join()
```

固定 SDK 的 ApiClient 没有原生 context manager 或 `close()`；以上资源字段在官方源码存在，基础兼容测试还需实际断言。请求函数仍显式传 `client.default_headers["Access-Token"]`，不虚构 `configuration.access_token`。SDK 的异常转换只记录错误码/request_id；禁止记录完整 response、URL 查询串、异常 body 或 client repr。

所有真实外部调用单独进入以下准入范围；`sdk_client` 仅构造请求域，不能一次准入后在域内无限调用。`AdmissionPolicy` 从 P01 的运行配置取已验证值，字段全部为正整数；本计划不提供平台固定 QPS。

```python
# integrations/tiktok/sdk.py 追加；限流实现始终位于 P01 的 app.jobs.admission。
import logging
from uuid import uuid4
from redis.exceptions import RedisError
from app.core.config import settings
from app.jobs.admission import AdmissionPolicy, admit_call, release_call

class AccountAdmissionDeferred(DomainError):
    def __init__(self, retry_after_ms: int):
        super().__init__("admission_deferred", "调用额度暂不可用", retryable=True)
        self.retry_after_ms = retry_after_ms

@contextmanager
def admitted_account_call(redis_client, *, context: TenantContext, endpoint: str,
                          advertiser_id: str, policy: AdmissionPolicy) -> Iterator[None]:
    lease_id = uuid4()
    app_scope = settings.TIKTOK_APP_ID
    admission = admit_call(redis_client, app_scope=app_scope, endpoint=endpoint,
        tenant_id=context.tenant_id, advertiser_id=advertiser_id,
        lease_id=lease_id, policy=policy)
    if not admission.granted:
        raise AccountAdmissionDeferred(admission.retry_after_ms)
    try:
        yield
    finally:
        try:
            release_call(redis_client, app_scope=app_scope, endpoint=endpoint,
                tenant_id=context.tenant_id, advertiser_id=advertiser_id, lease_id=lease_id)
        except (RedisError, DomainError):
            logging.getLogger(__name__).warning("admission_release_failed", extra={
                "tenant_id": str(context.tenant_id), "endpoint": endpoint, "lease_id": str(lease_id)
            })
```

`admit_call` 抛出 Redis 不可用错误时不会执行到 yield；不能捕获后直接调用 SDK。释放失败不能覆盖已经收到的远端结果，记录脱敏事件并依赖 P01 的租约到期回收；release 仅释放并发槽，不返还速率额度。调用的最大执行时间及处理余量必须小于 policy.lease_ms，按已验证 SDK 重试设置和任务硬截止配置，不能把一个 connect/read 超时参数当成整次请求的时限。

- [ ] **Step 4: 实现 state 消耗和官方授权交换。** 沿用 config 的 `TIKTOK_APP_ID/TIKTOK_APP_SECRET/TIKTOK_REDIRECT_URI`，新增 `TIKTOK_AUTHORIZATION_URL`，缺配置时返回 `app_not_configured`。授权 URL 从开发者后台提供的官方 Advertiser Authorization URL 配置读取，校验 HTTPS 与 TikTok 官方 host；只替换其 state 和已注册 redirect_uri，不使用 TikTok Login Kit 地址。

```python
# integrations/tiktok/auth.py 的原子核心
from datetime import datetime
from hashlib import sha256
from sqlalchemy import update
from sqlmodel import Session
from app.core.errors import DomainError
from app.modules.accounts.models import AuthorizationAttempt
from app.modules.tenants.permissions import require_tenant

def claim_authorization(session: Session, *, state: str, now: datetime) -> AuthorizationAttempt:
    digest = sha256(state.encode()).hexdigest()
    statement = update(AuthorizationAttempt).where(
        AuthorizationAttempt.state_hash == digest,
        AuthorizationAttempt.claimed_at.is_(None),
        AuthorizationAttempt.expires_at > now,
        AuthorizationAttempt.status == "PENDING",
    ).values(claimed_at=now, status="CLAIMED").returning(AuthorizationAttempt.id)
    attempt_id = session.execute(statement).scalar_one_or_none()
    if attempt_id is None:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    attempt = session.get(AuthorizationAttempt, attempt_id, populate_existing=True)
    require_tenant(session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id, action="manage")
    return attempt
```

`start_authorization` 使用 `secrets.token_urlsafe(32)`，有效期 10 分钟，保存摘要、真实 actor 和明确连接；已有连接必须属于租户。URL 用 `urllib.parse.urlsplit/parse_qsl/urlencode/urlunsplit` 构建，返回路径固定为该租户连接页，拒绝请求参数指定外域重定向。

```python
import secrets
from datetime import timedelta, timezone
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
from uuid import UUID
from app.core.context import TenantContext
from app.core.config import settings
from app.modules.accounts.models import TikTokConnection

def start_authorization(session: Session, *, context: TenantContext, connection_id: UUID | None) -> str:
    context = require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage")
    if not all([settings.TIKTOK_APP_ID, settings.TIKTOK_APP_SECRET,
                settings.TIKTOK_AUTHORIZATION_URL, settings.TIKTOK_REDIRECT_URI]):
        raise DomainError("app_not_configured", "等待配置开发者应用")
    base = urlsplit(settings.TIKTOK_AUTHORIZATION_URL)
    if base.scheme != "https" or base.hostname not in {"business-api.tiktok.com", "ads.tiktok.com"} or base.username:
        raise DomainError("invalid_authorization_url", "授权地址必须来自 TikTok 开发者后台")
    connection = session.get(TikTokConnection, connection_id) if connection_id else None
    if connection_id and (not connection or connection.tenant_id != context.tenant_id):
        raise DomainError("connection_not_found", "当前租户连接不存在")
    if connection is None:
        connection = TikTokConnection(tenant_id=context.tenant_id, status="PENDING_AUTH")
        session.add(connection); session.flush()
    state = secrets.token_urlsafe(32)
    attempt = AuthorizationAttempt(tenant_id=context.tenant_id, actor_id=context.actor_id,
        connection_id=connection.id, state_hash=sha256(state.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), status="PENDING")
    session.add(attempt); session.flush()
    query = dict(parse_qsl(base.query))
    query.update(app_id=settings.TIKTOK_APP_ID, redirect_uri=settings.TIKTOK_REDIRECT_URI, state=state)
    return urlunsplit((base.scheme, base.netloc, base.path, urlencode(query), ""))
```

`finish_authorization` 先只读核对 state 对应 Attempt 的租户、操作人、有效期，再用该记录恢复 context；这一步不消耗 state。获得共享准入后才原子 claim 并提交，让并发回调无法兑换同一码，再调用已核实的官方方法。未获准入时 state 保持 PENDING，HTTP 返回 retryable 错误和 Retry-After；有效期内允许重新请求同一回调，已经过期则重新发起授权。不要把准入延期标成 RESULT_UNKNOWN，也不要把授权码放入普通 outbox payload。

```python
import business_api_client
from app.integrations.tiktok.sdk import checked_data, admitted_account_call

client = business_api_client.ApiClient()
try:
    with admitted_account_call(redis_client, context=context,
            endpoint="/open_api/v1.3/oauth2/access_token/", advertiser_id="", policy=policy):
        attempt = claim_authorization(session, state=state, now=now)
        session.commit()
        response = business_api_client.AuthenticationApi(client).oauth2_access_token(
            body=business_api_client.Oauth2AccessTokenBody(
                app_id=settings.TIKTOK_APP_ID, secret=settings.TIKTOK_APP_SECRET, auth_code=auth_code
            ), _request_timeout=(5, 30)
        )
        data = checked_data(response)
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise DomainError("invalid_token_response", "授权结果缺少访问凭据")
finally:
    client.rest_client.pool_manager.clear()
    client.pool.close()
    client.pool.join()
```

兑换成功后再重查发起人权限，在新的事务将候选令牌加密写入 Attempt，设 `CANDIDATE_READY`，并通过 outbox 发布 `accounts.discover`，payload 只有 attempt_id；context 从记录恢复。未有旧凭据的新连接设 DISCOVERING；已有 ACTIVE 连接不改凭据。Task 4 完整资产发现成功后再原子提升候选版本。超时设 Attempt `RESULT_UNKNOWN`，明确提示重新发起授权，不自动重放授权码；取消为 `CANCELLED`，均不破坏旧 ACTIVE 连接。

- [ ] **Step 5: 增加 SDK fake 回归并运行。** 用 `monkeypatch` 替换 `AuthenticationApi.oauth2_access_token` 返回 `business_api_client.InlineResponse200(code=0, data={"access_token":"fake-only"})`；断言同 state 第二次调用的 SDK 次数仍为 1，候选密文不出现在 API JSON，outbox 与候选同事务落库，兑换失败保留旧凭据版本。运行 `uv run pytest tests/modules/accounts/test_oauth.py tests/modules/accounts/test_credentials.py -q`，预期全部通过。

共享准入增加以下测试；测试内策略数字只用于构造小规模 fixture，不是平台配额。

```python
# tests/modules/accounts/test_admission.py
from unittest.mock import Mock
import pytest
from redis.exceptions import ConnectionError
from app.jobs.admission import Admission, AdmissionPolicy
from app.integrations.tiktok import sdk as account_sdk

def test_rejected_admission_does_not_call_sdk_or_release(session, context, monkeypatch):
    policy = AdmissionPolicy(app_max_inflight=2, endpoint_max_inflight=2,
        tenant_max_inflight=1, advertiser_max_inflight=1, app_calls_per_window=10,
        endpoint_calls_per_window=10, window_ms=1000, lease_ms=60000)
    monkeypatch.setattr(account_sdk, "admit_call", lambda *args, **kwargs:
        Admission(granted=False, retry_after_ms=800))
    release, remote_call = Mock(), Mock()
    monkeypatch.setattr(account_sdk, "release_call", release)
    with pytest.raises(account_sdk.AccountAdmissionDeferred) as error:
        with account_sdk.admitted_account_call(Mock(), context=context,
                endpoint="/open_api/v1.3/bc/get/", advertiser_id="", policy=policy):
            remote_call()
    assert error.value.retry_after_ms == 800
    remote_call.assert_not_called()
    release.assert_not_called()

def test_redis_unavailable_does_not_enter_sdk(context, monkeypatch):
    remote_call = Mock()
    monkeypatch.setattr(account_sdk, "admit_call", Mock(side_effect=ConnectionError("test Redis unavailable")))
    with pytest.raises(ConnectionError):
        with account_sdk.admitted_account_call(Mock(), context=context,
                endpoint="/open_api/v1.3/oauth2/access_token/", advertiser_id="", policy=Mock()):
            remote_call()
    remote_call.assert_not_called()
```

另断言授权准入拒绝后 Attempt.claimed_at 仍为空；放行后正确释放相同 app_scope/endpoint/tenant/advertiser/lease_id；释放 Redis 失败不掩盖已收到的 SDK 结果。运行 `uv run pytest tests/modules/accounts/test_admission.py tests/modules/accounts/test_oauth.py -q`，预期全部通过。

- [ ] **Step 6: 提交。** 提交主题 `accounts: bind OAuth callbacks and credentials to tenants`；真实 HTTPS 回调验证归基础交付与联调计划，不在此任务发送真实授权请求。

### Task 4: 建立全量账户目录、外部资产归属与可恢复发现

**Files:**
- Modify: `backend/app/modules/accounts/models.py`
- Create: `backend/app/modules/accounts/discovery.py`
- Create: `backend/app/modules/accounts/tasks.py`
- Create: `backend/app/integrations/tiktok/accounts.py`
- Create: `backend/app/alembic/versions/02c_account_directory.py`
- Test: `backend/tests/modules/accounts/test_discovery.py`

**Interfaces:**
- Consumes: `checked_data`、官方 SDK、Task 3 候选凭据、`celery_app`、outbox；P01 `AdmissionPolicy/Admission/admit_call/release_call`，不依赖 P06。
- Produces: `DiscoveryRun`、`DiscoverySeen`、`TenantBC`、`AdvertiserAccount`、`BCAccountAccess`、`ExternalAssetOwner`。
- Produces: `save_directory_page(session, *, run_id: UUID, bc_id: str, page: int, rows: list[dict], authorized_ids: set[str], last_page: bool) -> None`。
- Produces: `finalize_directory(session, *, run_id: UUID) -> None`；只允许所有分页完成的 run 执行失效标记和候选提升。
- Produces: `read_bc_assets(client: business_api_client.ApiClient, *, bc_id: str, page: int, page_size: int) -> dict`。

**模型和迁移清单：**

| 模型 | 必需字段与约束 |
| --- | --- |
| TenantBC | tenant_id、bc_id 字符串组合主键；name、ownership_conflict |
| AdvertiserAccount | tenant_id、advertiser_id 组合主键；name、currency、timezone、remote_status、ownership_conflict |
| BCAccountAccess | tenant_id、bc_id、advertiser_id、connection_id 组合主键；in_bc、authorized、active、can_upload、can_build、permission_state、last_seen_run_id、checked_at |
| ExternalAssetOwner | kind、external_id 组合主键；owner_tenant_id；kind 只允许 BC/ADVERTISER |
| DiscoveryRun | id、tenant_id、actor_id、connection_id、candidate_attempt_id、credential_version、status、bc_cursor、next_attempt_at、error_code |
| DiscoverySeen | run_id、bc_id、page 组合主键；last_page、处理数量；完整事务落库后才记录该页 |

外键使用 tenant_id 与资源 ID 的组合；`BCAccountAccess` 分别引用账户、BC、连接的同租户组合键。建 `(tenant_id,name,advertiser_id)`、`(tenant_id,bc_id,active,advertiser_id)` 索引；单个租户连接只允许运行一个 discovery generation，重试使用同一 run_id。

- [ ] **Step 1: 写中断不误撤权和跨租户冲突测试。**

```python
from app.modules.accounts.discovery import save_directory_page, finalize_directory
from app.modules.accounts.models import BCAccountAccess
from app.core.errors import DomainError
import pytest

def test_partial_run_does_not_remove_old_access(session, discovery_run):
    run, old = discovery_run
    save_directory_page(session, run_id=run.id, bc_id=old.bc_id, page=1,
        rows=[{"advertiser_id":"90071992547409931", "name":"A", "currency":"USD", "timezone":"UTC"}],
        authorized_ids={"90071992547409931"}, last_page=False)
    with pytest.raises(DomainError) as error:
        finalize_directory(session, run_id=run.id)
    assert error.value.code == "discovery_incomplete"
    session.refresh(old)
    assert old.active is True
```

`discovery_run` fixture 在本文件创建 Task 1 的有效管理员/租户、ACTIVE 连接、一个 BC、一个旧的 active 访问关系、RUNNING DiscoveryRun，返回 `(run,old_access)`。另外创建两个租户读取同一个外部账户，断言两个目录均存在，第二个为 ownership_conflict 且不可写。

```python
@pytest.fixture
def discovery_run(session, context):
    from app.modules.accounts.models import TikTokConnection, TenantBC, AdvertiserAccount, DiscoveryRun
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(connection); session.flush()
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="1234567890123456789", name="BC")
    account = AdvertiserAccount(tenant_id=context.tenant_id, advertiser_id="old-account",
        name="old", currency="USD", timezone="UTC", remote_status="TEST_ENABLED")
    session.add_all([bc, account]); session.flush()
    old = BCAccountAccess(tenant_id=context.tenant_id, bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id, connection_id=connection.id,
        in_bc=True, authorized=True, active=True, can_upload=False, can_build=False, permission_state="UNKNOWN")
    run = DiscoveryRun(tenant_id=context.tenant_id, actor_id=context.actor_id,
        connection_id=connection.id, credential_version=0, status="RUNNING")
    session.add_all([old, run]); session.flush()
    return run, old
```

- [ ] **Step 2: 运行 `uv run pytest tests/modules/accounts/test_discovery.py -q`。** 预期新模型/服务缺失而失败。

- [ ] **Step 3: 接通官方读取调用，保持完整响应与业务事实分离。** 方法签名来自固定 SDK 源码；响应 `data` 为通用 object，不能根据 SDK 存在就假定所有字段可用。

```python
# integrations/tiktok/accounts.py
import business_api_client
from .sdk import checked_data

def read_bc_assets(client: business_api_client.ApiClient, *, bc_id: str, page: int, page_size: int) -> dict:
    return checked_data(business_api_client.BCApi(client).bc_asset_get(
        bc_id, "ADVERTISER", client.default_headers["Access-Token"],
        page=page, page_size=page_size, _request_timeout=(5, 30)
    ))

def read_authorized_advertisers(client: business_api_client.ApiClient, *, app_id: str, secret: str) -> dict:
    return checked_data(business_api_client.AuthenticationApi(client).oauth2_advertiser_get(
        app_id, secret, client.default_headers["Access-Token"], _request_timeout=(5, 30)
    ))

def read_business_centers(client: business_api_client.ApiClient, *, page: int, page_size: int) -> dict:
    return checked_data(business_api_client.BCApi(client).bc_get(
        client.default_headers["Access-Token"], page=page, page_size=page_size, _request_timeout=(5, 30)
    ))

def read_advertiser_details(client: business_api_client.ApiClient, *, advertiser_ids: list[str]) -> dict:
    return checked_data(business_api_client.AccountManagementApi(client).advertiser_info(
        advertiser_ids, client.default_headers["Access-Token"], _request_timeout=(5, 30)
    ))
```

上述四个生成方法签名保持不变，但每次调用都在 Task 3 的 `admitted_account_call` 中执行。调用方从持久化 run 恢复真实 context，再从 P01 取得该 endpoint 的 policy；使用的 endpoint 与作用域如下：

| 官方读取方法 | endpoint | advertiser_id 内部准入范围 |
| --- | --- | --- |
| `oauth2_advertiser_get` | `/open_api/v1.3/oauth2/advertiser/get/` | 空字符串 |
| `bc_get` | `/open_api/v1.3/bc/get/` | 空字符串 |
| `bc_asset_get` | `/open_api/v1.3/bc/asset/get/` | 空字符串 |
| `advertiser_info` | `/open_api/v1.3/advertiser/info/` | 仅查询一个账户时为该 ID；多账户合批时为空字符串 |

空字符串只用于内部准入 key，不添加到 TikTok 请求。账户未指定时仍共享 App、endpoint 和租户限制，不能跳过准入。分页每一页、每次重试均单独准入；app_scope 固定实际平台 App ID。

```python
with admitted_account_call(redis_client, context=context,
        endpoint="/open_api/v1.3/bc/asset/get/", advertiser_id="", policy=policy):
    raw_page = read_bc_assets(client, bc_id=bc_id, page=page, page_size=page_size)
```

准入拒绝时将该页标为 ADMISSION_WAIT，保存 `next_attempt_at=now+retry_after_ms`，不递增远端已发送计数、不推进分页、不登记 DiscoverySeen；P01 的到期调度恢复同一页。Redis 不可用时保持未发送状态并用可恢复错误退回队列，不能依靠内存 sleep 或降级放行。重启恢复不能绕过新准入，已经发出的请求仍按原来的结果核查规则处理。

分页归一化只接受经过固定版本样例验证的 `list/page_info` 结构，缺 list 或分页结束证据则 `unsupported_account_schema`，不能当成空目录完成扫描。原始 advertiser_id 保持字符串；账户明细缺币种/时区时目录标记 METADATA_INCOMPLETE，不能进入业务可用池。测试 fake 提供本任务定义的规范化 rows；不将 fake 的 role 字段宣称为官方事实。SDK 响应中未能核实上传/搭建能力时 `permission_state=UNKNOWN`、can_upload/can_build=False；真实能力映射的证据与签收由 07 联调计划提供，不通过真实上传来试探权限。

- [ ] **Step 4: 实现逐页保存、并发归属抢占和完整扫描提升。** 目录页面与 DiscoverySeen 同事务写入；重试看到同一 run/bc/page 已成功时不重复变更。归属认领的 SQL 核心如下：

```python
from sqlalchemy.dialects.postgresql import insert
from app.modules.accounts.models import ExternalAssetOwner

def claim_external_asset(session, *, kind: str, external_id: str, tenant_id):
    statement = insert(ExternalAssetOwner).values(
        kind=kind, external_id=external_id, owner_tenant_id=tenant_id
    ).on_conflict_do_nothing(index_elements=["kind", "external_id"])
    session.execute(statement)
    owner = session.get(ExternalAssetOwner, (kind, external_id), populate_existing=True)
    return owner.owner_tenant_id == tenant_id
```

每条规范化 row 都 upsert 当前租户账户和访问关系；`authorized` 由授权账户集合命中计算；`in_bc` 来自该 BC 成功页面；缺权限或冲突依然保留目录。账户名更新不改变 advertiser_id。

`finalize_directory` 对 run 加行锁，检查 BC 分页结束、每个 BC 的广告账户分页连续且有末页；未完整时抛 `discovery_incomplete`。完整时将该 connection 中未被本 run 看见的访问关系 active=False；其他连接关系不受影响。候选授权成功时核对原 credential_version 未变化，再替换密文、版本加一、连接 ACTIVE、Attempt ACCEPTED。失败只标 run ERROR，保留旧凭据及未扫描账户；失权则停止尚未执行的发现步骤。

任务注册名固定 `accounts.discover`，只从 payload 的 attempt_id/run_id 查询租户和真实操作人，再调用 `require_tenant(...,action="manage")`；禁止信任序列化 role。下一页只通过 outbox 排队，单页事务不持有跨网络调用数据库锁。

- [ ] **Step 5: 扩充并运行回归。** 添加第 2 页失败、重复第 1 页、末页后回收旧关系、较旧 run 无法覆盖新版本、两个租户并发认领同 ID 的 PostgreSQL 测试。为四个读取方法分别注入准入拒绝，断言 SDK 调用次数为零；到期放行后仍恢复同 run/bc/page，首次成功后只保存一份 DiscoverySeen。`uv run pytest tests/modules/accounts/test_discovery.py tests/modules/accounts/test_admission.py -q` 预期全部通过；`uv run alembic upgrade head` 应建立组合键和索引。提交主题 `accounts: persist resumable full account discovery`。

### Task 5: 实现账户访问交集和自动上传账户分配

**Files:**
- Create: `backend/app/modules/accounts/schemas.py`
- Create: `backend/app/modules/accounts/access.py`
- Test: `backend/tests/modules/accounts/test_access.py`

**Interfaces:**
- Consumes: Task 4 账户与权限模型、`require_tenant`。
- Produces: 公共 `AccountAccess/resolve_account_access/assign_upload_account`，按文首精确签名。
- Action 边界：`read` 需要目录访问；`build` 要 can_build，`upload` 要 can_upload；其他动作不接受为账户级动作。

- [ ] **Step 1: 写只满足一个集合时拒绝以及平台不能绕过外部权限的回归。**

```python
import pytest
from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access, assign_upload_account

@pytest.mark.parametrize("field", ["in_bc", "authorized", "active", "can_upload"])
def test_upload_requires_all_access_facts(session, account_access_case, field):
    context, grant = account_access_case
    setattr(grant, field, False)
    session.flush()
    with pytest.raises(DomainError):
        resolve_account_access(session, context=context, bc_id=grant.bc_id,
                               advertiser_id=grant.advertiser_id, action="upload")

def test_upload_account_is_system_assigned(session, account_access_case):
    context, grant = account_access_case
    result = assign_upload_account(session, context=context, bc_id=grant.bc_id)
    assert result.advertiser_id == grant.advertiser_id
    assert result.connection_id == grant.connection_id
    grant.can_upload = False
    session.flush()
    with pytest.raises(DomainError) as error:
        assign_upload_account(session, context=context, bc_id=grant.bc_id)
    assert error.value.code == "no_upload_account"
```

`account_access_case` fixture 在本测试文件创建有效 operator 成员、ACTIVE 连接、无冲突 BC 和账户、币种 USD/时区 UTC、in_bc/authorized/active/can_upload/can_build 全 True 的 VERIFIED Grant。Fixture 的能力是离线测试事实，不是绕开真实发现。

```python
@pytest.fixture
def account_access_case(session, context):
    from app.modules.accounts.models import TikTokConnection, TenantBC, AdvertiserAccount, BCAccountAccess
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(connection); session.flush()
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="1234567890123456789", name="BC", ownership_conflict=False)
    account = AdvertiserAccount(tenant_id=context.tenant_id, advertiser_id="90071992547409931",
        name="Upload", currency="USD", timezone="UTC", remote_status="TEST_ENABLED", ownership_conflict=False)
    session.add_all([bc, account]); session.flush()
    grant = BCAccountAccess(tenant_id=context.tenant_id, bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id, connection_id=connection.id,
        in_bc=True, authorized=True, active=True, can_upload=True, can_build=True, permission_state="VERIFIED")
    session.add(grant); session.flush()
    return context, grant
```

- [ ] **Step 2: 运行 `uv run pytest tests/modules/accounts/test_access.py -q`。** 预期公共接口缺失而失败。

- [ ] **Step 3: 实现带租户条件的 JOIN 与权限检查。**

```python
# modules/accounts/access.py
from uuid import UUID
from sqlmodel import Session, select
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant
from .models import AdvertiserAccount, BCAccountAccess, TenantBC, TikTokConnection
from .schemas import AccountAccess

def resolve_account_access(session: Session, *, context: TenantContext, bc_id: str,
                           advertiser_id: str, action: str) -> AccountAccess:
    if action not in {"read", "build", "upload"}:
        raise DomainError("invalid_account_action", "账户动作无效")
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action)
    account = session.get(AdvertiserAccount, (context.tenant_id, advertiser_id), populate_existing=True)
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if not account or not bc:
        raise DomainError("account_not_in_bc", "账户不在当前租户 BC 目录")
    if account.ownership_conflict or bc.ownership_conflict:
        raise DomainError("account_ownership_conflict", "账户或 BC 归属冲突")
    if not account.currency or not account.timezone:
        raise DomainError("account_metadata_incomplete", "账户信息尚未完整")
    grants = session.exec(select(BCAccountAccess).where(
        BCAccountAccess.tenant_id == context.tenant_id,
        BCAccountAccess.bc_id == bc_id, BCAccountAccess.advertiser_id == advertiser_id,
        BCAccountAccess.in_bc == True, BCAccountAccess.authorized == True,
        BCAccountAccess.active == True,
    ).order_by(BCAccountAccess.connection_id)).all()
    for grant in grants:
        connection = session.get(TikTokConnection, grant.connection_id, populate_existing=True)
        if not connection or connection.tenant_id != context.tenant_id or connection.status != "ACTIVE":
            continue
        if action != "read" and grant.permission_state != "VERIFIED":
            continue
        if action == "upload" and not grant.can_upload:
            continue
        if action == "build" and not grant.can_build:
            continue
        return AccountAccess(advertiser_id=advertiser_id, bc_id=bc_id,
                             connection_id=connection.id, currency=account.currency, timezone=account.timezone)
    raise DomainError("account_access_denied", "当前授权不支持该账户操作")

def assign_upload_account(session: Session, *, context: TenantContext, bc_id: str) -> AccountAccess:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="upload")
    statement = select(BCAccountAccess.advertiser_id).where(
        BCAccountAccess.tenant_id == context.tenant_id, BCAccountAccess.bc_id == bc_id,
        BCAccountAccess.active == True, BCAccountAccess.in_bc == True,
        BCAccountAccess.authorized == True, BCAccountAccess.can_upload == True,
        BCAccountAccess.permission_state == "VERIFIED",
    ).distinct().order_by(BCAccountAccess.advertiser_id)
    for advertiser_id in session.exec(statement.execution_options(yield_per=100)):
        try:
            return resolve_account_access(session, context=context, bc_id=bc_id,
                                          advertiser_id=advertiser_id, action="upload")
        except DomainError as error:
            if error.code not in {"account_ownership_conflict", "account_metadata_incomplete", "account_access_denied"}:
                raise
    raise DomainError("no_upload_account", "当前 BC 没有可上传的授权账户")
```

`AccountAccess` 按文首定义实现为 Pydantic model。查询必须同时核对远端账户可操作状态：发现层把已明确停用/关闭的账户访问关系 active=False；未知远端状态保持 permission_state=UNKNOWN。系统选择不永久绑定账户，素材上传任务保存返回结果的 advertiser_id、connection_id；后续新上传可以分配其他账户。

- [ ] **Step 4: 增加多连接及重查测试后提交。** 测试某连接失效而另一个同租户同 BC 已授权连接有效时只选择有效连接；平台 context 也不能访问冲突账户；修改成员角色后重复调用立即拒绝。运行 `uv run pytest tests/modules/accounts/test_access.py -q`，预期全部通过。提交主题 `accounts: resolve authorized access and choose upload accounts`。

### Task 6: 完成批量粘贴解析、分页目录和连接 API

**Files:**
- Create: `backend/app/modules/accounts/resolver.py`
- Create: `backend/app/modules/accounts/router.py`
- Modify: `backend/app/modules/accounts/schemas.py`、`backend/app/api/main.py`、`backend/app/api/routes/integrations.py`
- Test: `backend/tests/modules/accounts/test_resolver.py`、`backend/tests/modules/accounts/test_router.py`

**Interfaces:**
- Consumes: `resolve_account_access`、Task 3 OAuth、Task 4 目录、模板身份依赖、`Page`。
- Produces: `InputLine(line_no:int, raw:str)`、`ResolvedLine(line_no:int, raw:str, status:str, advertiser_id:str|None, candidates:list[str], duplicate_of:int|None, reason:str|None)`。
- Produces: `resolve_lines(session, *, context:TenantContext, bc_id:str, lines:list[InputLine]) -> list[ResolvedLine]`。
- Produces: `GET /api/tenants/{tenant_id}/accounts?bc_id=&query=&cursor=&limit=`、`POST .../accounts/resolve`；`GET .../bcs`、`GET .../tiktok/connections`、`POST .../tiktok/authorizations`、`PATCH .../tiktok/connections/{id}`；固定回调 GET。

- [ ] **Step 1: 写纯解析用例，不把重复名当成成功。**

```python
from app.modules.accounts.resolver import parse_matching_rows
from app.modules.accounts.schemas import InputLine

def test_ids_names_ambiguity_and_original_lines():
    rows = [{"advertiser_id":"90071992547409931", "name":"完整账户A"},
            {"advertiser_id":"2", "name":"重名"}, {"advertiser_id":"3", "name":"重名"}]
    result = parse_matching_rows([
        InputLine(line_no=1, raw="90071992547409931"),
        InputLine(line_no=2, raw=" 完整账户A "),
        InputLine(line_no=3, raw="重名"), InputLine(line_no=4, raw="不存在"),
    ], rows)
    assert [row.status for row in result] == ["MATCHED", "DUPLICATE", "AMBIGUOUS", "NOT_FOUND"]
    assert result[1].duplicate_of == 1
    assert result[2].candidates == ["2", "3"]
    assert result[3].raw == "不存在"
```

- [ ] **Step 2: 运行 `uv run pytest tests/modules/accounts/test_resolver.py -q`，确认因解析器缺失而失败。**

- [ ] **Step 3: 实现具体纯解析器与分块数据库查询。**

```python
# modules/accounts/resolver.py
from collections import defaultdict
from .schemas import InputLine, ResolvedLine

def parse_matching_rows(lines: list[InputLine], rows: list[dict]) -> list[ResolvedLine]:
    by_id = {row["advertiser_id"]: row for row in rows}
    by_name = defaultdict(list)
    for row in rows:
        by_name[row["name"]].append(row["advertiser_id"])
    seen, results = {}, []
    for line in lines:
        text = line.raw.strip()
        candidates = [text] if text in by_id else sorted(set(by_name.get(text, [])))
        result = ResolvedLine(line_no=line.line_no, raw=line.raw, status="NOT_FOUND",
            advertiser_id=None, candidates=candidates, duplicate_of=None, reason=None)
        if not text:
            result.status = "EMPTY"
        elif len(candidates) > 1:
            result.status = "AMBIGUOUS"
        elif len(candidates) == 1:
            account_id = candidates[0]
            result.advertiser_id = account_id
            result.status = "DUPLICATE" if account_id in seen else "MATCHED"
            result.duplicate_of = seen.get(account_id)
            seen.setdefault(account_id, line.line_no)
        results.append(result)
    return results
```

`resolve_lines` 先 `require_tenant(...,"read")`；将本次非空文本以最多 500 项一块进行 SQL IN 查询，只查当前 tenant、当前 BC 关系范围中的精确 ID 或完整 name。禁止为了找不到账户跨租户查询；同名候选也不能混入当前租户其他 BC 的账户。纯解析 MATCHED 项再按 action=`build` 验证；失败保持原始行、advertiser_id 和 DomainError code，状态改 BLOCKED。确切 ID 若只存在同租户其他 BC，可诊断 `account_not_in_bc`；外租户和不存在输入都只返回当前作用域 NOT_FOUND，避免泄漏其他租户目录。重复关系按整个本次请求去重，分块边界不能重置 seen。

目录查询以 advertiser_id 字符串为稳定 seek 值，使用 `limit+1` 判断 next_cursor；BC 条件走 EXISTS 访问关系避免多连接产生重复行。cursor 使用 base64 编码 `{tenant_id,bc_id,query,last_id}`，解析后必须逐项匹配当前查询作用域；格式不符返回 422。限制 `limit=1..200`，每次 resolve 最多 500 行是请求分块上限，不是租户账户或整批搭建的总上限；搭建模块负责保持全批去重与原始行号。

- [ ] **Step 4: 装配连接和账户路由，完成 callback 结果跳转。** 账户列表可供 viewer 读取，resolve 中的写权限结果只报告 BLOCKED 不执行写入。连接新增/重新授权/停用为 manage；callback 用 state 恢复上下文，不依赖浏览器此刻租户。停用只修改连接状态及审计，不调用 TikTok revoke 或广告停用接口。GET connections 返回 `id/status/last_discovery/error_code` 等允许字段，没有凭据。

```python
@router.post("/tenants/{tenant_id}/accounts/resolve", response_model=list[ResolvedLine])
def post_resolve(tenant_id: UUID, body: ResolveRequest, session: SessionDep, user: CurrentUser):
    context = require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    return resolve_lines(session, context=context, bc_id=body.bc_id, lines=body.lines)
```

`ResolveRequest` 为 Pydantic model，字段 `bc_id:str` 与 `lines:list[InputLine]`（1～500），line_no 正整数且本请求唯一。router 文件导入本任务定义类型、Task 1 权限和模板 CurrentUser/SessionDep。callback 的错误跳转只带稳定业务错误码；日志中禁止保留 auth_code/state/token 查询串。
回调替换基础计划 `api/routes/integrations.py` 中同一路径的未配置响应处理，不重复注册第二条 GET 路由；缺 App 配置时继续保留原来的确定提示。

- [ ] **Step 5: 运行回归、边界规模测试并提交。** `test_router.py` 对 501 行返回 422，两个 500 行分块保留行号；10,001 条本地目录 fixture 分页全取恰好一次、同名歧义、游标租户不符、重复连接 JOIN 去重均有断言。运行 `uv run pytest tests/modules/accounts/test_resolver.py tests/modules/accounts/test_router.py -q`，预期全部通过。提交主题 `accounts: support bulk pasted IDs and names with paginated directory`。

### Task 7: 交付 shadcn 租户工作台、连接管理和账户页面

**Files:**
- Create: `frontend/src/features/tenants/{TenantScope,TenantAdminPage,MembersPage}.tsx`
- Create: `frontend/src/features/accounts/{AccountsPage,ConnectionsPage,BulkAccountInput}.tsx`
- Create: `frontend/src/routes/_layout/platform.tenants.tsx`、`tenants.$tenantId.accounts.tsx`、`tenants.$tenantId.members.tsx`
- Modify: `frontend/src/routes/_layout.tsx`、`frontend/src/client/`（通过基础计划的 OpenAPI 生成命令更新）
- Test: `frontend/tests/tenants-accounts.spec.ts`

**Interfaces:**
- Consumes: Task 2/6 API、模板登录、自动生成 API 类型、现有 shadcn Select/Table/Button/Textarea/Badge。
- Produces: `TenantScope={tenantId:string,bcId:string|null,role:Role}`；`BulkAccountInput({value,onChange,onResolve,rows})` 供搭建页面复用。
- Query keys 统一 `['tenant',tenantId,'accounts',bcId,query,cursor]` 等租户前缀；草稿以租户/BC 作为 React key。

**前端交互补充草案，待本轮评审。** 本 Task 按[租户账户功能设计第 10 节](../specs/2026-09-08-tiktok-01-tenants-accounts-design.md#10-前端交互补充草案待本轮评审)和本轮原型评审结果细化实现，保留原 Task 编号及所有后端任务。页面映射如下：

| 页面 ID | 路由与容器 | 本 Task 的交付 |
| --- | --- | --- |
| UI-01 | `/login`，沿用 P01 Task 5 模板登录 | 对齐 401 返回登录、403 保持登录和内部返回地址；不再实现第二份登录 |
| UI-02 | `/platform/tenants` → TenantAdminPage | 独立平台入口、租户表格、创建/详情/编辑 Sheet、进入租户 |
| UI-08 | `/tenants/:tenantId/accounts?tab=accounts`，tab 取 accounts 或 connections | AccountsPage/ConnectionsPage 作为同页 Tabs 内容；详情 Sheet、搜索筛选和服务端分页 |
| UI-10 | `/tenants/:tenantId/members` → MembersPage | 管理员成员表格、固定角色、添加/编辑 Sheet，无账户分配控件 |

侧栏使用统一顺序：广告搭建、搭建任务、素材库、投放策略、账户与授权、版权方连接、成员管理。平台管理保持独立入口；成员管理只向管理员显示。BulkAccountInput 是交给搭建页复用的组件，不在 UI-08 账户目录再做一套广告账户选择或搭建入口。当前租户/BC 固定在顶栏。

- [ ] **Step 1: 写租户切换不泄漏旧页面和批量输入无二次勾选的 Playwright 测试。** 复用基础计划登录 storageState，通过 Playwright route fulfill 提供本地 API fake，所有外部 TikTok 请求均被拦截失败。

```typescript
import { test, expect } from "@playwright/test"

test("租户切换不展示旧账户", async ({ page }) => {
  await page.route("**/api/me/tenants*", route => route.fulfill({ json: {
    items: [{ id: "11111111-1111-4111-8111-111111111111", name: "租户甲", role: "platform_admin" },
            { id: "22222222-2222-4222-8222-222222222222", name: "租户乙", role: "platform_admin" }],
    next_cursor: null,
  }}))
  await page.route("**/api/tenants/*/bcs*", route => route.fulfill({ json: {
    items: [{ bc_id: "1234567890123456789", name: "当前BC" }], next_cursor: null,
  }}))
  await page.route("**/api/tenants/*/accounts?*", route => route.fulfill({ json: {
    items: [{ advertiser_id: "90071992547409931", name: route.request().url().includes("11111111-") ? "甲账户" : "乙账户" }],
    next_cursor: null,
  }}))
  await page.goto("/tenants/11111111-1111-4111-8111-111111111111/accounts?tab=accounts")
  await expect(page.getByText("甲账户", { exact: true })).toBeVisible()
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: "租户乙" }).click()
  await expect(page.getByText("甲账户", { exact: true })).toHaveCount(0)
  await expect(page.getByText("乙账户", { exact: true })).toBeVisible()
})
```

- [ ] **Step 2: 运行 `bunx playwright test tests/tenants-accounts.spec.ts`，预期页面/组件未实现而失败。**

- [ ] **Step 3: 实现租户切换及批量粘贴组件的核心行为。**

```tsx
// TenantScope.tsx 中的切换回调：先取消旧请求，再使旧缓存不可见。
const switchTenant = async (tenantId: string) => {
  await queryClient.cancelQueries({ queryKey: ["tenant"] })
  queryClient.removeQueries({ queryKey: ["tenant"] })
  setScope({ tenantId, bcId: null, role: memberships.find(item => item.id === tenantId)!.role })
  setDraftEpoch(value => value + 1)
}
// 根布局使用 <Outlet key={`${scope.tenantId}:${scope.bcId ?? ""}:${draftEpoch}`} />。
// Task 2 返回的 memberships 决定候选，不允许手填任意租户 ID 绕过后端。
```

```tsx
// BulkAccountInput.tsx
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"

type Row = { line_no: number; raw: string; status: string; reason: string | null }
type Props = {
  value: string
  onChange: (value: string) => void
  onResolve: () => void
  rows: Row[]
}
export function BulkAccountInput({ value, onChange, onResolve, rows }: Props) {
  return <section>
    <label htmlFor="bulk-accounts">批量粘贴账户</label>
    <Textarea id="bulk-accounts" value={value} onChange={event => onChange(event.target.value)}
      placeholder="每行一个账户 ID 或完整账户名称" />
    <Button onClick={onResolve}>解析账户</Button>
    <ul>{rows.map(row => <li key={row.line_no}>
      第 {row.line_no} 行：{row.raw} · {row.status}{row.reason ? `：${row.reason}` : ""}
    </li>)}</ul>
  </section>
}
```

TenantScope 组件定义 `memberships` 为 `/api/me/tenants` 结果，`scope` 和 `draftEpoch` 为 React state，QueryClient 来自 `useQueryClient`；异步结果按 tenant query key 归属，不能回写新租户数据。大量解析结果在实际页面用服务端分页或分块分页容器，只将当前页 rows 传给组件。
调用 switchTenant 或对应 switchBC 之前由离页守卫处理未保存表单；用户取消离页时不执行该回调。重挂载只清理当前浏览范围的 UI 状态，不调用删除/更新原草稿 API；返回原租户/BC 后仍能打开已保存草稿。路由中的 tenant_id 与顶栏上下文同步，不能仅改顶栏文字而继续请求旧租户。

- [ ] **Step 4: 接入各管理页表单。** 租户页供平台创建、改名和停用；成员页固定三种租户角色；连接页显示等待配置应用、授权中、发现中、可用、重授权、错误与停用。账户页按 BC/名称/ID/状态查询、显示实际币种与访问冲突，下一页使用 next_cursor。错误行展示原文与原因，成功解析直接可用，无再次勾选；素材账户配置入口不存在。所有写操作依据角色隐藏按钮，并显示后端拒绝结果。

本步骤的布局与状态验收具体化为：

- UI-02 表格列为租户名称/ID、状态、操作；新增 Sheet 只有租户名称、初始管理员，详情按需读取本租户成员，不在列表逐行加载所有账户或成员。
- UI-08 账户列为账户名称/ID、币种与时区、平台状态、可用性、核验时间、查看详情；授权连接列为连接名称/ID、授权状态、关联 BC、最近授权/发现时间、操作。平台状态与连接状态不能合并为一个含糊的“正常”。
- UI-08 账户列表按当前 BC 筛选，授权连接列表按租户展示；尚未发现 BC 的新连接仍可见，不能被顶栏 BC 隐藏。
- UI-10 成员列为姓名/邮箱、角色、状态、操作；角色输入只能是租户管理员/投手/只读成员，最后管理员的服务端拒绝在 Sheet 内显示。
- Sheet 有固定标题和底部动作；字段错误紧邻字段，错误请求不把已填非敏感字段清空；密码/令牌不回显。键盘关闭返回触发按钮，有未保存输入时走同一离页守卫。
- 默认 50 行，可选 100，上一页/下一页读取服务端；BC 筛选使用 URL 参数 bc_id，改变 BC、搜索或状态清空游标。表头固定在表格滚动区域，长 ID 复制使用完整字符串。
- 首次加载显示表头与 Skeleton；无任何资源、筛选无结果、获取失败分别使用不同文案与操作。刷新失败保留旧数据并标记失败，不能显示成正常空表。
- 401 进入 UI-01 重新登录；403 显示当前角色无权访问及返回工作台入口，不注销、不反复重定向登录。原型阶段仅使用 mock 状态，不发授权/成员修改/账户发现请求。

增加具体浏览器验收矩阵，不能只以“找到按钮”代替页面交付：

| 场景 | 可观察断言 |
| --- | --- |
| UI-08 切换授权连接页签后刷新 | URL 保留 tab，仍显示授权连接；账户列表筛选不会套到连接列表 |
| BC-A 已保存草稿后切换 BC-B | 原草稿仍绑定 BC-A；不向草稿发送改绑/删除请求，新页面无 BC-A 账户残留 |
| 页面存在未保存修改 | 取消离页后仍在原租户/BC 且输入保留；确认离开后才更换上下文 |
| 无授权与筛选无结果 | 前者显示接入引导，后者只显示清除筛选；两者没有假成功记录 |
| 权限待核实或归属冲突 | 账户仍在目录及筛选结果中，详情解释原因，不显示“设为素材账户”按钮 |
| 分页读取 10,001 条目录 | 单次表格不超过选择页大小，翻页/返回保留条件，不一次下载全部明细 |
| 401 与 403 | 前者进入登录并保留合法返回位置；后者保留登录，不能将无权限误报成会话过期 |
| UI-02/UI-10 管理 Sheet | 表单、进行中、字段错误、成功关闭与焦点返回均有可见状态；不发送邀请邮件 |

- [ ] **Step 5: 运行页面、类型与构建验证后提交。** 再加入 viewer 不显示“新增授权/修改成员”、空应用配置显示“等待配置开发者应用”、同名行显示 AMBIGUOUS、长 ID 保持文本、冲突账户仍列出但不能操作等断言。

```bash
cd frontend
bunx playwright test tests/tenants-accounts.spec.ts
bun run build
```

预期：测试全部通过，类型与构建成功；网络记录中没有真实 TikTok 调用。提交主题 `accounts: add tenant workspace and bulk account UI`。

## 自审与交付验收

- 权限、平台代投和成员管理：Task 1/2；OAuth 租户绑定和凭据：Task 3；全量目录及冲突：Task 4。
- 账户权限交集和系统上传账户：Task 5；ID/完整名称、歧义及分页：Task 6；实际使用页面：Task 7。
- 接口错误码集中由基础计划 DomainError handler 映射；tenant_forbidden/action_forbidden/platform_forbidden 为 403，未知资源为 404，输入错误为 422，需重新授权或配置冲突为 409。响应不包含 token、密文或认证信息。
- 对各 Task 修改脚本运行 `uv run python -m compileall app/modules/tenants app/modules/accounts app/integrations/tiktok app/core/credentials.py`；后端全模块测试一次通过后，不无理由重复运行。
- 回归通过只证明本地逻辑及 fake 契约；实际应用权限、账户资产响应、上传/搭建能力识别必须在 07 真实联调中获得证据。没有证据时保留 UNKNOWN，不能用管理员身份或历史星屿账号默认值填补。
- 本计划不发送成员邀请邮件，不创建广告、不修改历史 CLI/skill，也不运行真实广告账户请求；广告执行计划继续采用三层创建直接 ENABLE。

## 官方 SDK 核实来源

- [AuthenticationApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/authentication_api.py)：授权交换与授权广告账户读取签名。
- [BCApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/bc_api.py)：BC 与 BC 资产分页读取签名。
- [AccountManagementApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/account_management_api.py)：`advertiser_info` 方法。
- [ApiClient](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api_client.py)：请求头、线程池和 REST 客户端资源管理。
