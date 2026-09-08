# 工程基础与授权回调入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 执行方式沿用用户按需使用 Superpowers 的偏好，不要求额外启动整套流程。

**Goal:** 建立可独立运行的应用工程、公共契约、持久化任务投递和可部署的登录及回调入口。

**Architecture:** 从官方 FastAPI 全栈模板的固定提交建立独立应用，保留认证、SQLModel、Alembic 和前端客户端生成。API、Celery Worker 与 Beat 分进程运行，PostgreSQL 保存业务事实，Redis 传递任务；本阶段不创建广告。

**Tech Stack:** Python、FastAPI、SQLModel、PostgreSQL、Celery/Redis、React、TypeScript、Bun、shadcn/ui、Playwright、官方 TikTok Python SDK。

**Spec:** [整体设计](../specs/2026-09-08-tiktok-00-overall-design.md)、[租户账户设计](../specs/2026-09-08-tiktok-01-tenants-accounts-design.md)。

## Global Constraints

- 直接使用官方 SDK；后端路线采用 FastAPI，使用官方 Python SDK。
- 普通业务表、文件访问、任务、缓存和日志均携带租户范围。
- Campaign、Ad Group、Ad 创建时直接使用 ENABLE；该规则由后续搭建计划实现，本阶段没有广告写入口。
- 投放报表的数据同步、刷新频率、收入核算、分析数据库选型和自动调价暂时后置。
- 前端使用 shadcn/ui；租户业务接口前缀为 `/api/tenants/{tenant_id}/`。

---

## 执行位置与工程基线

本计划中的代码路径均相对未来应用根目录 `APP_ROOT=/Users/yaotingfeng/Documents/ytf/tiktok-ad-automation`。本轮仅编写文档；该目录当前不存在。实际执行时若目录已有内容，先检查已有工程并复用，不能覆盖。

当前资料目录不是应用 Git 仓库。以下 Git 命令仅在新应用仓库执行；不初始化当前资料目录，不复制广告 outputs、凭据或历史固定账户。

固定初始基线：

| 项目 | 基线与使用方式 |
| --- | --- |
| FastAPI 模板 | `cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7` |
| TikTok 官方 SDK | `f809c396520df2d7b201a9ccc5378d822b728ed3` 的 `python_sdk/` |
| SDK 安装名 / 导入名 | 官方源码安装名 `python_sdk`；Python 导入 `business_api_client` |
| Python 与前端工具 | 遵循上述模板的 Python 版本约束及 Bun 锁文件；当前模板要求 Python `>=3.14` |
| 版本记录 | `docs/engineering-baseline.md`、`backend/uv.lock`、`frontend/bun.lock`；记录实际验证版本 |

本表是已查验的源码基线，不代表 SDK 已在该 Python 运行时通过测试。Task 1 负责验证可导入和所需接口可调用；失败需调整并记录官方依赖的兼容版本，不能改成手写 TikTok HTTP。[模板依赖](https://github.com/fastapi/full-stack-fastapi-template/blob/cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7/backend/pyproject.toml)、[官方 SDK 安装定义](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/setup.py)

## 文件职责

| 路径 | 职责 |
| --- | --- |
| `backend/app/core/context.py` | 不可变租户与真实操作人上下文 |
| `backend/app/core/errors.py` | 业务错误码，API 转换和任务错误分类 |
| `backend/app/core/pagination.py` | 公共分页响应 |
| `backend/app/core/logging.py` | 白名单结构化日志 |
| `backend/app/jobs/{models,outbox,celery_app,tasks}.py` | 持久化投递、Celery 配置、恢复扫描 |
| `backend/app/api/routes/integrations.py` | 回调地址与未配置时的确定响应；02 接管真实 OAuth |
| `frontend/src/features/workspace/WorkspaceShell.tsx` | 页面布局与当前租户区域 |
| `compose.yml`、`compose.override.yml`、`compose.staging.yml` | 本地和预发布进程配置 |
| `docs/runbooks/bootstrap-deployment.md` | 域名、回调、运行与验证记录 |

### Task 1: 建立可复现工程并核验官方 SDK

**Files:**
- Create: `docs/engineering-baseline.md`, `backend/tests/contracts/test_tiktok_sdk_surface.py`。
- Modify: `backend/pyproject.toml`, `backend/uv.lock`, `frontend/bun.lock`。

**Interfaces:**
- Consumes: 固定官方模板提交与 SDK 提交。
- Produces: 保留模板 `app.api.deps.CurrentUser`、`SessionDep`、`app.models.User`；所有业务计划直接引用这些已有认证能力。

- [x] 在空目标目录建立模板分支，记录来源；复制本组 specs/plans 到新仓库 `docs/superpowers/`，保留资料目录原件。复制后将指向原资料目录的参考链接改为其真实绝对路径，检查文档互链；不把运营输出和凭据作为文档依赖一起复制。

```bash
git clone https://github.com/fastapi/full-stack-fastapi-template.git /Users/yaotingfeng/Documents/ytf/tiktok-ad-automation
cd /Users/yaotingfeng/Documents/ytf/tiktok-ad-automation
git checkout -b feat/tiktok-foundation cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7
git remote rename origin template
```

- [x] 写 SDK 离线契约测试。通过 monkeypatch 拦截 SDK 的 `ApiClient.call_api`，不请求外部服务；同时检查 `CampaignCreationApi.smart_plus_campaign_create`、`AdgroupApi.smart_plus_adgroup_create`、`AdApi.smart_plus_ad_create`、`FileApi.ad_video_upload`、`CreativeManagementApi.creative_asset_share` 存在。

```python
import business_api_client as sdk

def test_smart_plus_method_accepts_body_without_network(monkeypatch):
    calls = []
    client = sdk.ApiClient()
    monkeypatch.setattr(client, "call_api", lambda *args, **kwargs: calls.append((args, kwargs)))
    sdk.CampaignCreationApi(client).smart_plus_campaign_create(
        "test-token", body={"advertiser_id": "account-test", "operation_status": "ENABLE"}
    )
    assert len(calls) == 1
```

- [x] 在未安装 SDK 时运行 `uv run pytest tests/contracts/test_tiktok_sdk_surface.py -q`，确认是缺少 SDK 而失败；随后安装固定官方源码并锁依赖。

```bash
cd /Users/yaotingfeng/Documents/ytf/tiktok-ad-automation/backend
uv add 'python_sdk @ git+https://github.com/tiktok/tiktok-business-api-sdk.git@f809c396520df2d7b201a9ccc5378d822b728ed3#subdirectory=python_sdk'
uv add 'celery[redis]' cryptography boto3
uv run pytest tests/contracts/test_tiktok_sdk_surface.py -q
```

- [x] 在 `frontend/` 执行 `bun install --frozen-lockfile`、`bun run build`。SDK 契约和模板构建均须 PASS；报告只声称离线兼容，Minis 字段覆盖见 06/07。
- [x] 提交本任务：`git commit -m "foundation: pin template and official TikTok SDK"`，仅暂存上述工程与契约文件。

### Task 2: 统一租户上下文、业务错误和 API 前缀

**Files:**
- Create: `backend/app/core/context.py`, `backend/app/core/errors.py`, `backend/app/core/pagination.py`, `backend/app/core/logging.py`, `backend/tests/core/test_contracts.py`。
- Modify: `backend/app/core/config.py`, `backend/app/main.py`, `backend/app/api/deps.py`, `backend/app/api/main.py`, `frontend/src/client/`, `frontend/src/main.tsx`, `compose.yml`。

**Interfaces:**
- Produces: `TenantContext(tenant_id: UUID, actor_id: UUID, role: str)`；`DomainError(code: str, message: str, retryable: bool=False)`；`Page[T](items: list[T], next_cursor: str|None)`。
- 租户权限解析由 02 的 `require_tenant` 实现；上下文类型本身不授权。

- [x] 写冻结上下文、错误响应与日志泄漏测试，再执行 `uv run pytest tests/core/test_contracts.py -q` 确认新模块缺失导致失败。

```python
from dataclasses import FrozenInstanceError
from uuid import uuid4
import pytest
from app.core.context import TenantContext
from app.core.logging import log_fields

def test_context_cannot_change_tenant_and_logs_drop_secrets():
    context = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = uuid4()
    assert log_fields({"tenant_id": str(context.tenant_id), "access_token": "secret"}) == {
        "tenant_id": str(context.tenant_id)
    }
```

- [x] 实现公共类型；`Page` 用 Pydantic 泛型响应，日志仅接受白名单字段，不记录任意请求体或异常对象字符串。

```python
from dataclasses import dataclass
from uuid import UUID
from pydantic import BaseModel

@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    actor_id: UUID
    role: str

class DomainError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None

def log_fields(values: dict) -> dict:
    allowed = {"tenant_id", "actor_id", "task_id", "step_id", "request_id", "error_code", "duration_ms"}
    return {key: value for key, value in values.items() if key in allowed}
```

- [x] 将定义分别放入职责对应文件。`DomainError` 转 HTTP 时用固定映射：权限 403、资源不可见 404、版本/幂等冲突 409、配置输入错误 422、外部暂不可用 503；返回 `{code,message,retryable}`，不回传凭据。各模块把其明确错误码注册进 `ERROR_HTTP_STATUS`，未注册业务错误为500，不根据原始外部消息猜状态码。
- [x] 将模板 `API_V1_STR` 值改为 `/api`，同步 OAuth `tokenUrl`、OpenAPI 客户端、健康检查、测试与代理路径；用 `rg '/api/v1' backend frontend compose* scripts` 找出全部旧引用并逐项修正。
- [x] 保留模板生成客户端的 Bearer 配置，所有模块复用 `client.gen` 的认证。`frontend/src/main.tsx` 的全局处理改为仅401清除登录态；403展示当前操作无权限并保留登录，避免访问某租户失败使平台人员退出全部租户。[模板客户端配置](https://github.com/fastapi/full-stack-fastapi-template/blob/cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7/frontend/src/main.tsx)

```typescript
if (error instanceof AxiosError && error.response?.status === 401) {
  localStorage.removeItem("access_token")
  window.location.href = "/login"
}
```
- [x] 后端重跑公共契约测试；按模板 `scripts/generate-client.sh` 生成客户端，再运行前端构建。提交 `foundation: establish tenant contracts and API paths`。

### Task 3: 数据库、配置和后台进程可独立运行

**Files:**
- Create: `backend/app/jobs/__init__.py`, `backend/app/jobs/celery_app.py`, `backend/tests/core/test_runtime_config.py`, `compose.staging.yml`。
- Modify: `backend/app/core/config.py`, `.env.example`, `.gitignore`, `compose.yml`, `compose.override.yml`, `backend/tests/conftest.py`。

**Interfaces:**
- Produces: `app.jobs.celery_app.celery_app`；Settings 字段 `REDIS_URL`、`TIKTOK_APP_ID`、`TIKTOK_APP_SECRET`、`TIKTOK_REDIRECT_URI`、`CONNECTION_ENCRYPTION_KEY`、`S3_ENDPOINT_URL`、`S3_BUCKET`、`S3_REGION`、`S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`。
- SDK App 配置可空，连接页明确待配置；加密密钥和对象存储凭据在使用相关功能前校验，不能用示例值启动真实服务。
- Produces: 后端测试 `session` fixture，绑定测试 PostgreSQL 事务；不同模块共用它，不使用 SQLite 代替锁、唯一约束和分页测试。
- Produces: `redis_client` fixture 使用单独测试 Redis；`context` 是本轮测试租户上下文，`client` 是依赖覆盖到测试 session 的 FastAPI TestClient。业务模块 conftest 负责为 context 建真实租户与成员。

- [x] 加配置测试：没有 TikTok App 时应用仍能启动，连接状态为未配置；提供部分 App 字段时返回完整缺项列表，不进入授权交换。`uv run pytest tests/core/test_runtime_config.py -q` 先失败。

```python
def configured_app_fields(values: dict[str, str | None]) -> list[str]:
    names = ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI")
    return [name for name in names if not values.get(name)]

def test_app_setup_requires_all_fields():
    missing = configured_app_fields({"TIKTOK_APP_ID": "app-test"})
    assert missing == ["TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"]
```

- [x] 将检查函数放入 `app/core/config.py`；与 P02 契约统一，新增密钥字段为 `str`，使用 `Field(default="", repr=False)` 隐藏 repr。禁止对 Settings 整体做日志输出或响应序列化，缺项检查只返回字段名；`.env.example` 只列变量说明，真实值仅写被忽略的运行配置。
- [x] 配置 Redis、Worker、Beat；两者使用后端同一镜像、同一依赖锁与数据库迁移版本。Celery 不使用结果后端作为业务完成依据。

```python
from celery import Celery
from app.core.config import settings

celery_app = Celery("tiktok_ads", broker=str(settings.REDIS_URL))
celery_app.conf.update(
    task_serializer="json", accept_content=["json"],
    task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, task_ignore_result=True,
    imports=("app.jobs.tasks",),
    beat_schedule={"flush-dispatch": {"task": "jobs.flush_dispatch", "schedule": 5.0}},
)
```

- [x] Worker 命令为 `celery -A app.jobs.celery_app:celery_app worker -Q resources,builds,control --loglevel=INFO`，Beat 使用 `celery -A app.jobs.celery_app:celery_app beat --loglevel=INFO`，只部署一个 Beat。后续任务注册及路由由各功能计划补全，未完成 Task 4 前不启动 Beat。
- [x] 测试配置以独立测试数据库运行 Alembic；`session` fixture 使用外层事务和 `join_transaction_mode="create_savepoint"`，测试结束回滚。应用客户端通过 dependency override 使用同一 session；并发测试自行建两个独立连接并清理 fixture 数据。

```python
from uuid import uuid4
import os
import pytest
from redis import Redis
from sqlmodel import Session
from app.core.db import engine
from app.core.context import TenantContext

@pytest.fixture
def session():
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as value:
            yield value
        transaction.rollback()

@pytest.fixture
def context():
    return TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")

@pytest.fixture
def redis_client():
    client = Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
    try:
        client.ping()
        yield client
    finally:
        client.close()
```

测试进程的 `DATABASE_URL` 指向独立测试库；`TEST_REDIS_URL` 指向单独测试 Redis。测试键使用每次运行唯一的 test-run ID，模块自行清理自身键，不清空共享实例。原模板测试 fixture 保留，只对新业务 `client` 的 DB 依赖做覆盖；应用重启型故障演练使用提交后的独立场景数据库，不能借外层测试事务假装进程可见。
- [x] 执行 `docker compose config --quiet`，执行迁移和配置测试；不把包含展开后密码的 Compose 配置打印到报告。提交 `foundation: configure database and worker processes`。

### Task 4: 事务提交后的可靠任务投递

**Files:**
- Create: `backend/app/jobs/models.py`, `backend/app/jobs/outbox.py`, `backend/app/jobs/tasks.py`, `backend/tests/jobs/test_outbox.py`, `backend/app/alembic/versions/p01_dispatch.py`。
- Modify: `backend/app/alembic/env.py`。

**Interfaces:**
- Produces: `enqueue_after_commit(session: Session, *, context: TenantContext, task_name: str, task_key: str, payload: dict) -> UUID`。
- 该函数只在调用者当前事务中写待投递记录，函数内部不 commit、不发消息；名称表达“提交后才能投递”。
- `flush_dispatch(limit: int=100) -> int` 扫描已提交记录发送给 Celery。后台函数入口统一接收 `tenant_id: str, actor_id: str, payload: dict`，入口重新查权限，禁止信任旧 role。

- [x] 写回滚不发送、提交后投递、发送后进程退出导致重复投递三项测试，运行 `uv run pytest tests/jobs/test_outbox.py -q` 确认失败。核心断言：

```python
def test_rollback_never_publishes(session, context, monkeypatch):
    from app.jobs.outbox import enqueue_after_commit, flush_dispatch
    from app.jobs.celery_app import celery_app
    sent = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(k))
    enqueue_after_commit(session, context=context, task_name="jobs.probe", task_key="probe-1", payload={})
    session.rollback()
    flush_dispatch()
    assert sent == []
```

- [x] 建 `PendingDispatch`：UUID 主键、tenant_id、actor_id、task_name、task_key、payload JSONB、available_at、published_at、attempts；唯一约束 `(tenant_id,task_key)`，待投递索引 `(available_at,id) WHERE published_at IS NULL`。重复键配置不同返回 `dispatch_key_conflict`，不能静默替换。
- [x] 同一迁移增加 `DispatchTenantCursor(tenant_id,last_published_at)`；enqueue 时幂等创建游标。投递器先选有到期消息的租户，按 last_published_at 空值优先、旧值优先轮换；每租户每轮最多投递5条、整轮最多100条，更新游标后换租户。这些是调度窗口，不限制租户账户数；T1 大量积压不能将 T2 小任务排在百万条消息后面。
- [x] 用 PostgreSQL INSERT ON CONFLICT 建记录，冲突后查询并比较 task_name/actor_id/payload；`payload` 禁止 token、cookie、文件正文，只携带内部 ID 和必要标量。现有记录返回同一 UUID。

```python
from sqlalchemy.dialects.postgresql import insert

statement = insert(PendingDispatch).values(
    tenant_id=context.tenant_id, actor_id=context.actor_id,
    task_name=task_name, task_key=task_key, payload=payload,
).on_conflict_do_nothing(index_elements=["tenant_id", "task_key"])
session.exec(statement)
```

- [x] `flush_dispatch` 用 `FOR UPDATE SKIP LOCKED` 领取租户游标及本轮对应消息；task_name 注册表选择队列，调用下述发送方式后标记 published_at。Broker 失败写 attempts/下次时间，仍保持未投递；发送成功后进程崩溃会再次发送，消费者按业务稳定键去重。业务大批次仅持续补充有限可执行窗口到 Celery，不预先将百万个步骤全量投进 broker。

```python
celery_app.send_task(
    record.task_name,
    kwargs={"tenant_id": str(record.tenant_id), "actor_id": str(record.actor_id), "payload": record.payload},
    task_id=str(record.id), queue=registered_queue,
)
```

- [x] 定义注册表初始只含 `jobs.probe -> control`；各功能计划新增明确 task_name，不允许请求任意 Celery 函数。`jobs.flush_dispatch` 是 Beat 控制任务，不接收租户业务 payload；扫描公平性和批量业务限流由 06 扩展。
- [x] 新增 conftest `context` fixture，使用测试租户与操作人 UUID；并发测试证明两投递器不同时领取同一行。说明 `task_id` 相同不等于 Celery 自动去重，接收方仍需数据库业务幂等。迁移、测试通过后提交 `jobs: persist dispatch before publishing`。

补充租户轮换测试：T1有10,000条到期消息，T2仅1条；首轮发送包含T2且T1不超过5条。Broker不可用时不推进published_at；恢复后轮换规则仍生效。

### Task 5: 收敛模板页面，交付登录和回调路由

**Files:**
- Create: `backend/app/api/routes/integrations.py`, `backend/tests/api/test_bootstrap_surface.py`, `frontend/src/features/workspace/WorkspaceShell.tsx`, `frontend/tests/workspace-shell.spec.ts`。
- Modify: `backend/app/api/main.py`, `backend/app/api/routes/users.py`, `frontend/src/routes/_layout.tsx`, `frontend/src/routes/login.tsx`, `frontend/src/routes/signup.tsx`, `frontend/src/components/Sidebar/`。

**Interfaces:**
- Produces: `GET /api/integrations/tiktok/callback`，未配置 App 时返回 503 `{code:"tiktok_app_not_configured",message:"等待配置开发者应用",retryable:false}`；02 在同一路由实现 state 校验与授权交换。
- 公开注册接口关闭；保留已有管理员创建用户和登录能力，租户开通交给 02。

- [ ] 先加测试：公开注册返回 403；缺 App 的回调返回明确错误；未登录访问工作台跳登录，已登录看到“尚未接入租户”而非虚构账户数据。执行 `uv run pytest tests/api/test_bootstrap_surface.py -q`。

```python
def test_unconfigured_callback_is_explicit(client):
    response = client.get("/api/integrations/tiktok/callback", params={"auth_code": "sample"})
    assert response.status_code == 503
    assert response.json()["code"] == "tiktok_app_not_configured"
```

- [ ] 移除模板 Items 示例的公开路由和导航引用；保留历史模板迁移链，新增业务表采用新迁移，不重写已经应用的历史迁移。注册入口移除，后端同时拒绝公开注册。
- [ ] 按[前端设计 UI-01 与全局布局](../specs/2026-09-08-tiktok-06-frontend-experience-design.md)组合模板现有 shadcn Sidebar、Field、Button、Card。导航按“投放工作 / 租户管理”分组：广告搭建、搭建任务、素材库、投放策略；账户与授权、版权方连接、成员管理。平台管理为独立入口。顶栏固定租户和 BC；首次无上下文用 S01 接入引导，未实现页面不显示虚构业务数字。保留页面角色和路由插槽，02 接管实际上下文。
- [ ] 验收 UI-01 和工作台布局：1440/1280 宽导航与内容可读，窄屏收起侧栏、表单转单列；键盘可到达导航与主操作，表单 label/错误和 Sheet 标题齐全。401 返回登录，403 保留工作台显示无权限，不误删登录态。
- [ ] 编写 Playwright 测试并运行；测试定义登录态使用模板已有测试账号 setup，业务账号由后续测试数据导入，均不使用真实广告凭据。

```typescript
import { expect, test } from "@playwright/test"

test("workspace shows tenant context", async ({ page }) => {
  await page.goto("/")
  await expect(page.getByText("尚未接入租户")).toBeVisible()
  await expect(page.getByRole("link", { name: "注册" })).toHaveCount(0)
})
```

- [ ] `bunx playwright test tests/workspace-shell.spec.ts`、`bun run build` 通过后提交 `workspace: add login and connection entry points`。

### Task 6: 交付可部署入口及回调地址证据

**Files:**
- Create: `docs/runbooks/bootstrap-deployment.md`, `scripts/check-bootstrap.py`。
- Modify: `compose.staging.yml`, `frontend/.env.example`。

**Interfaces:**
- Produces: 部署地址、HTTPS 回调 URL、运行版本、登录验证结果；没有部署资源时产物为已验证的部署包，状态写“待配置运行环境”，不能填虚构 URL。
- Consumes: 用户提供的实际主机/域名和部署凭据；这属于执行时的外部配置，不影响本轮完整写出计划。

- [ ] Compose 预发布配置复用模板代理，设置实际域名、HTTPS、后端 `/api` 路由、前端静态资源；DB/Redis 不向公网发布端口，生产配置不包含开发环境 private 路由。
- [ ] `scripts/check-bootstrap.py` 读取命令行基础 URL，验证公开健康检查和未配置回调的确定响应，不提交授权码或广告请求。

```python
import argparse
import httpx

parser = argparse.ArgumentParser()
parser.add_argument("base_url")
args = parser.parse_args()
base = args.base_url.rstrip("/")
with httpx.Client(base_url=base, timeout=15, follow_redirects=False) as client:
    assert client.get("/api/utils/health-check/").status_code == 200
    result = client.get("/api/integrations/tiktok/callback")
    assert result.status_code in {400, 422, 503}
    assert result.headers.get("content-type", "").startswith("application/json")
```

- [ ] 本地执行检查脚本并确认成功；部署到已配置环境后，对实际 HTTPS URL 再执行一次并记录结果。回调返回业务错误而非404表明路由存在，不能据此标记 OAuth 已成功。
- [ ] 在部署记录中填写从实际配置读取的 `TIKTOK_REDIRECT_URI`，交给应用申请使用。App 申请完成后由 02/07 进行租户授权及真实 SDK 联调；当前无 App 不阻止部署登录和回调入口。
- [ ] 提交 `foundation: document and verify bootstrap deployment`。本阶段完成条件为工程测试通过、进程可运行、部署包可验证；外部地址交付另列实际完成状态。

### Task 7: 所有 Worker 共用的官方 API 调用准入

**Files:**
- Create: `backend/app/jobs/admission.py`, `backend/app/jobs/admission.lua`, `backend/tests/jobs/test_admission.py`。
- Modify: `backend/app/core/config.py`, `.env.example`。

**Interfaces:**
- Produces: `Admission(granted: bool, retry_after_ms: int)` 与下述 `AdmissionPolicy`。
- `admit_call(redis_client, *, app_scope: str, endpoint: str, tenant_id: UUID, advertiser_id: str, lease_id: UUID, policy: AdmissionPolicy) -> Admission`。
- `release_call(redis_client, *, app_scope: str, endpoint: str, tenant_id: UUID, advertiser_id: str, lease_id: UUID) -> None`；释放不传 policy，不返还已经消耗的调用额度。
- `admission_policy(endpoint: str) -> AdmissionPolicy` 从 Settings 的 `TIKTOK_CALL_POLICIES` 读取配置。应用总量、时间窗口与租户/账户并发是全局一致的基础配置；端点只可覆盖 endpoint_max_inflight/endpoint_calls_per_window/lease_ms。
- P02 账户发现、P04 素材、P06 场景与广告调用均消费本任务；P06 另实现业务任务的租户公平调度，不重新定义限流器。

- [x] 先写真实 Redis 并发回归，使用每次测试唯一的 app_scope。两个 Worker 竞争相同应用总额度，其中一个失败时不能消耗该端点/租户的其它额度；不同应用不相互阻塞。运行 `uv run pytest tests/jobs/test_admission.py -q` 先确认模块缺失失败。

```python
from dataclasses import dataclass
from pydantic import BaseModel, Field

@dataclass(frozen=True)
class Admission:
    granted: bool
    retry_after_ms: int

class AdmissionPolicy(BaseModel):
    app_max_inflight: int = Field(gt=0)
    endpoint_max_inflight: int = Field(gt=0)
    tenant_max_inflight: int = Field(gt=0)
    advertiser_max_inflight: int = Field(gt=0)
    app_calls_per_window: int = Field(gt=0)
    endpoint_calls_per_window: int = Field(gt=0)
    window_ms: int = Field(gt=0)
    lease_ms: int = Field(gt=0)
```

- [x] `admission.py` 使用应用 ID 而不是连接 ID 作为共享范围。生成六个同 Redis hash slot 的键：应用/端点调用窗口、应用/端点/租户/账户在途租约。无具体账户的 BC 或 OAuth 查询传空 advertiser_id，仅用于内部额度分组，不把空 ID 加到 TikTok 请求中。

```python
from hashlib import sha256

def admission_keys(app_scope, endpoint, tenant_id, advertiser_id):
    digest = sha256(app_scope.encode()).hexdigest()
    base = f"tiktok:{{{digest}}}"
    endpoint_key = sha256(endpoint.encode()).hexdigest()
    account_key = sha256(advertiser_id.encode()).hexdigest()
    return [
        f"{base}:rate", f"{base}:endpoint:{endpoint_key}:rate",
        f"{base}:active", f"{base}:endpoint:{endpoint_key}:active",
        f"{base}:tenant:{tenant_id}:active", f"{base}:advertiser:{account_key}:active",
    ]
```

- [x] 用 Lua 一次完成六个条件校验和写入，时间来自 Redis TIME。以下内容保存为 `admission.lua`；argv 为调用唯一 lease_id、窗口毫秒、租约毫秒、两种速率容量及四种在途容量。

```lua
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
local member = ARGV[1]
local window = tonumber(ARGV[2])
local lease = tonumber(ARGV[3])
local wait = 0
for i = 1, 6 do
    local cutoff = now
    if i <= 2 then cutoff = now - window end
    redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
    local capacity = tonumber(ARGV[i + 3])
    local current = redis.call('ZSCORE', KEYS[i], member)
    if current then
        local remaining = tonumber(current) - now
        if i <= 2 then remaining = remaining + window end
        wait = math.max(wait, remaining)
    end
    if redis.call('ZCARD', KEYS[i]) >= capacity then
        local first = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
        local remaining = tonumber(first[2]) - now
        if i <= 2 then remaining = remaining + window end
        wait = math.max(wait, remaining)
    end
end
if wait > 0 then return {0, math.ceil(wait)} end
for i = 1, 6 do
    local score = now + lease
    local ttl = lease + 1000
    if i <= 2 then score = now; ttl = window + 1000 end
    redis.call('ZADD', KEYS[i], score, member)
    if i > 2 then
        local latest = redis.call('ZRANGE', KEYS[i], -1, -1, 'WITHSCORES')
        ttl = math.ceil(tonumber(latest[2]) - now + 1000)
    end
    redis.call('PEXPIRE', KEYS[i], ttl)
end
return {1, 0}
```

- [x] 实现调用与释放。每一次真实 SDK 请求使用新的 lease_id；业务数据库的步骤尝试标识独立处理幂等。准入拒绝时根据 retry_after_ms 重新排队或给 OAuth 请求明确重试响应，不能让 Worker sleep 等额度。

```python
from pathlib import Path
from redis.exceptions import RedisError
from app.core.errors import DomainError

def admit_call(redis_client, *, app_scope, endpoint, tenant_id, advertiser_id, lease_id, policy):
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id)
    args = [str(lease_id), policy.window_ms, policy.lease_ms,
            policy.app_calls_per_window, policy.endpoint_calls_per_window,
            policy.app_max_inflight, policy.endpoint_max_inflight,
            policy.tenant_max_inflight, policy.advertiser_max_inflight]
    try:
        granted, delay = redis_client.eval(Path(__file__).with_suffix(".lua").read_text(), 6, *keys, *args)
    except RedisError as error:
        raise DomainError("admission_unavailable", "调用配额服务暂不可用", True) from error
    return Admission(bool(granted), int(delay))

def release_call(redis_client, *, app_scope, endpoint, tenant_id, advertiser_id, lease_id):
    keys = admission_keys(app_scope, endpoint, tenant_id, advertiser_id)[2:]
    script = "for i=1,#KEYS do redis.call('ZREM',KEYS[i],ARGV[1]) end return 1"
    redis_client.eval(script, len(keys), *keys, str(lease_id))
```

- [x] 释放放在 SDK 调用 finally；释放 Redis 错误由调用方单独记录，不覆盖已经得到的远端结果，也不能把已成功请求转成可盲重试请求。租约长度必须大于该请求连接/读取总超时及处理余量；长视频调用可分配更长租约或独立端点策略。
- [x] `TIKTOK_CALL_POLICIES` 未配置时返回 `admission_unconfigured`，不能把额度无限大作为默认。测试显式配置小容量，例如窗口1000ms、应用2次、端点1次；这些仅是测试参数，不作为 TikTok 的官方限额。

```python
from app.core.config import settings

def admission_policy(endpoint: str) -> AdmissionPolicy:
    config = settings.TIKTOK_CALL_POLICIES
    if not config.get("base"):
        raise DomainError("admission_unconfigured", "请配置应用调用额度")
    override = config.get("endpoints", {}).get(endpoint, {})
    allowed = {"endpoint_max_inflight", "endpoint_calls_per_window", "lease_ms"}
    if set(override) - allowed:
        raise DomainError("admission_policy_invalid", "端点配置不能覆盖应用共享额度")
    return AdmissionPolicy.model_validate({**config["base"], **override})
```

Settings 中 `TIKTOK_CALL_POLICIES` 类型为 `dict`、缺省为空字典；配置错误返回503并阻止外部请求。对较长视频请求，租约配置必须覆盖其最大超时；ZSET TTL 使用已有租约的最晚到期时间，较短新请求不能使长请求租约提前失效。
- [x] 增加以下回归：两端点合计受应用窗口限制；两个租户共用应用；相同连接不同 Worker 不各获一份额度；release 后仍受速率限制；进程丢失最终靠租约到期恢复；Redis不可用零外部调用。全部通过后提交 `jobs: share API admission across workers`。

```python
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from app.jobs.admission import AdmissionPolicy, admit_call

def test_workers_share_application_inflight_limit(redis_client):
    app_scope = f"test-{uuid4()}"
    policy = AdmissionPolicy(
        app_max_inflight=1, endpoint_max_inflight=2, tenant_max_inflight=2,
        advertiser_max_inflight=2, app_calls_per_window=10,
        endpoint_calls_per_window=10, window_ms=1000, lease_ms=60000,
    )
    def try_call(index):
        return admit_call(
            redis_client, app_scope=app_scope, endpoint=f"endpoint-{index}",
            tenant_id=uuid4(), advertiser_id=str(index), lease_id=uuid4(), policy=policy,
        ).granted
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(try_call, [1, 2]))
    assert sorted(results) == [False, True]
```

## 阶段验收与交接

- 固定基线、SDK 离线契约、前端构建和目标测试均有执行记录。
- 回调入口可独立于 App 配置交付；不把模拟认证当成真实授权。
- 事务回滚不会发任务，消息重复投递不被错误描述为“队列自动保证幂等”。
- 02 可以直接复用认证及公共类型；03/04 可以消费 outbox 契约。
- 所有代码和部署检查均在执行计划时运行，本轮编写计划不代表上述验证已经通过。
