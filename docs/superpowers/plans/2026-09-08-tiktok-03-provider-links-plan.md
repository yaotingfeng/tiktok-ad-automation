# 版权方、剧目与推广链接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 按用户偏好按需使用技能；本轮仅编写计划。

**Goal:** 在严格租户隔离下接入网眼、嘉书，为批量搭建提供可分页、可恢复、保留归因信息的剧目与推广链接结果。

**Architecture:** FastAPI 模块负责连接、查询和任务入口；每个版权方适配器接受明确的连接上下文。PostgreSQL 保存链接复用约束与外部写步骤，事务 outbox 投递 Celery，远端结果未知时先回查。

**Tech Stack:** Python 3.14、FastAPI、SQLModel、Alembic、PostgreSQL、Celery、httpx；React、TypeScript、shadcn/ui、Bun、Playwright。版本沿用基础计划的锁文件。

**Spec:** [整体设计](../specs/2026-09-08-tiktok-00-overall-design.md)、[版权方设计](../specs/2026-09-08-tiktok-02-provider-links-design.md)。依赖工程基础计划 01 和租户账户计划 02。

## Global Constraints

- 新应用根目录固定为 `/Users/yaotingfeng/Documents/ytf/tiktok-ad-automation`，下文文件路径均相对该目录；本轮不创建该目录。
- 每个租户独立管理版权方连接；相同版权方在不同租户之间不共享凭据、可见应用或推广链接。
- 同一租户、连接、应用、剧目及推广配置，复用已有链接；先查，缺失时才创建。
- 自动解析成功的剧目直接进入预览；重名、多语种等歧义由用户处理，不能自动猜测。
- 适配器接受明确的连接上下文，不读取进程级“当前账号”，不依赖 CLI 的全局登录切换。
- 不得把某个版权方全局绑定到一个 Minis ID。应用与 Minis 信息随租户连接发现，并结合 TikTok 账户权限校验。
- 同一嘉书渠道要求不同起播集数或其他互斥配置时，不承诺能够独立新建多条链接，也不静默覆盖已有配置。
- API 前缀使用 `/api/tenants/{tenant_id}/providers`；角色采用 `platform_admin/tenant_admin/operator/viewer`，动作采用 `read/manage/provider_write`。
- 不复制 `.jiashu-link-accounts.json`、账号文件、会话文件或 `outputs/`；只读取现有 CLI 的协议函数与脱敏说明。
- HTTP 客户端仅用于版权方协议；任何 TikTok 调用必须走基础计划固定的官方 Python SDK。
- 命令中的 pytest 均在新应用 `backend/` 执行；Bun 命令在 `frontend/` 执行。计划中的预期结果不是已经运行通过的声明。

## 文件分工（Files）

| 文件 | 职责 |
| --- | --- |
| `backend/app/modules/providers/models.py` | 连接、应用、剧目、版本化链接、准备任务、外部写步骤 SQLModel 表 |
| `backend/app/modules/providers/schemas.py` | 跨模块 DTO、请求与结果校验 |
| `backend/app/modules/providers/repository.py` | 租户限定查询、键集分页、唯一约束冲突处理 |
| `backend/app/modules/providers/connections.py` | 加密凭据、按连接独立登录与应用发现 |
| `backend/app/modules/providers/adapters/{contract,wangyan,jiashu}.py` | 明确的版权方协议契约及实现 |
| `backend/app/modules/providers/link_steps.py` | 创建、生成、保存、回查步骤与结果未知处理 |
| `backend/app/modules/providers/service.py`、`tasks.py`、`router.py` | 模块入口、Celery 工作单元、HTTP 路由 |
| `frontend/src/features/providers/` | 连接管理、异常行、链接明细组件 |
| `frontend/src/routes/_layout/tenants.$tenantId.providers.tsx` | 租户版权方工作区路由 |
| `backend/tests/modules/providers/`、`frontend/tests/providers.spec.ts` | 离线协议、隔离、重试和页面回归 |

## 跨计划接口（Interfaces）

消费基础接口：`TenantContext(tenant_id: UUID, actor_id: UUID, role: str)`、`DomainError(code, message, retryable=False)`、`Page[T](items: list[T], next_cursor: str | None)`。
消费 `app/core/credentials.py` 的 `encrypt_credentials(*, tenant_id: UUID, value: dict[str, str]) -> str` 与 `decrypt_credentials(*, tenant_id: UUID, ciphertext: str) -> dict[str, str]`。
消费 `enqueue_after_commit(session, *, context: TenantContext, task_name: str, task_key: str, payload: dict) -> UUID`；调用方提交同一事务，禁止直接 `.delay()`。
`service.py` 导出以下接口，所有查询与 Worker 恢复均再次校验租户和连接归属：

```python
def prepare_links(session, *, context: TenantContext, connection_id: UUID,
                  application_id: str, lines: list[str], config: dict,
                  request_id: UUID) -> UUID:
    return create_preparation_request(
        session, context=context, connection_id=connection_id,
        application_id=application_id, lines=lines, config=config,
        request_id=request_id,
    )

def get_link_results(session, *, context: TenantContext, task_id: UUID,
                     cursor: str | None = None, page_size: int = 100) -> Page[ResolvedLink]:
    return read_preparation_results(
        session, context=context, task_id=task_id, cursor=cursor, page_size=page_size,
    )
```

`create_preparation_request` 与 `read_preparation_results` 在任务 4 的 `repository.py` 中实现，签名与上述转发参数一致。`task_id` 是本模块准备任务 ID，不能当作 Redis 中的短期执行记录。
结果字段统一采用 `external_drama_id` 和 `url`；不增加 `provider_drama_id/jump_url` 别名。调用方必须循环读取 `next_cursor`，不能只处理第一页。

---

### 任务 1：固定租户数据模型、结果契约与复用键

**文件（Files）：**

- 创建：`backend/app/modules/providers/models.py`、`schemas.py`、`repository.py`。
- 创建：`backend/app/alembic/versions/0003_provider_links.py`。
- 测试：`backend/tests/modules/providers/test_contracts.py`、`test_tenant_repository.py`。

**接口（Interfaces）：** 消费 `TenantContext`、`Page`、`DomainError`；输出 `ResolvedLink`、`DramaCandidate`、`link_reuse_key` 和下表实体。

| 表 | 必需字段及约束 |
| --- | --- |
| ProviderConnection | UUID、tenant_id、kind、display_name、encrypted_credentials、credential_version、status；所有凭据读取按 tenant_id 校验 |
| ProviderApplication | UUID、tenant_id、connection_id、external_id、name、channel_config JSONB、tiktok_minis_id 可空；唯一连接＋external_id |
| ProviderDrama | UUID、tenant_id、connection_id、application_id、external_drama_id、title、language；唯一连接＋应用＋外部剧 ID |
| PromotionLink | UUID、tenant_id、reuse_key、drama_id、connection_id、application_id、config JSONB、remote_id、url、protected_base、attribution JSONB、version、verified_at、status；唯一 tenant_id＋reuse_key＋version |
| LinkPreparation | UUID、tenant_id、actor_id、request_id、request_digest、connection_id、application_id、config、status；唯一 tenant_id＋request_id |
| LinkPreparationItem | UUID、tenant_id、preparation_id、line_no、raw_input、resolved JSONB、status；唯一 preparation_id＋line_no |
| ProviderEffect | UUID、tenant_id、remote_scope_key、step、request_digest、status、attempt_token、remote_id、result JSONB；唯一 tenant_id＋remote_scope_key＋step＋request_digest |
| ProviderRemoteScope | UUID、tenant_id、scope_key、active_item_id、status；唯一 tenant_id＋scope_key，保护整个远端渠道的多步操作 |

PromotionLink 的当前版本用部分唯一索引 `tenant_id,reuse_key WHERE status='ready'` 保证一个有效结果；旧结果转 `superseded` 后保留。连接、应用、剧目的关联采用带 tenant_id 的复合外键，迁移加入对应索引。

- [ ] **步骤 1：先写约束与复用键回归。**

```python
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.modules.providers.schemas import ResolvedLink, link_reuse_key

def test_link_key_scopes_tenant_and_config():
    tenant, connection = uuid4(), uuid4()
    args = (tenant, connection, "app-a", "drama-1")
    assert link_reuse_key(*args, {"episode": 1, "mode": "iaa"}) == \
        link_reuse_key(*args, {"mode": "iaa", "episode": 1})
    assert link_reuse_key(*args, {"episode": 1}) != \
        link_reuse_key(*args, {"episode": 2})
    assert link_reuse_key(*args, {}) != \
        link_reuse_key(uuid4(), connection, "app-a", "drama-1", {})

def test_ready_link_requires_complete_identity():
    with pytest.raises(ValidationError):
        ResolvedLink(input_id=uuid4(), line_no=1, raw_input="Moon",
                     provider_kind="wangyan", connection_id=uuid4(),
                     application_id="app-a", status="ready")
```

- [ ] **步骤 2：运行失败测试。** `uv run pytest tests/modules/providers/test_contracts.py -q`；预期尚无模块或校验器而失败。

- [ ] **步骤 3：实现 DTO、键与迁移。** DTO 使用以下完整字段，模型表按上表实现；迁移后创建两个租户的同名剧目，另一租户查询必须无结果。

```python
import hashlib
import json
from typing import Literal, Self
from uuid import UUID
from pydantic import BaseModel, Field, model_validator

class DramaCandidate(BaseModel):
    external_drama_id: str
    title: str
    language: str | None = None

class ResolvedLink(BaseModel):
    input_id: UUID
    line_no: int
    raw_input: str
    provider_kind: str
    connection_id: UUID
    application_id: str
    drama_id: UUID | None = None
    external_drama_id: str | None = None
    title: str | None = None
    language: str | None = None
    link_id: UUID | None = None
    url: str | None = None
    protected_base: str | None = None
    tiktok_minis_id: str | None = None
    status: Literal["pending", "needs_resolution", "blocked_auth",
                    "config_conflict", "retryable_error", "result_unknown",
                    "failed", "ready"]
    candidates: list[DramaCandidate] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def require_ready_fields(self) -> Self:
        if self.status == "ready":
            required = (self.drama_id, self.external_drama_id, self.title,
                        self.link_id, self.url, self.protected_base)
            if any(value is None for value in required):
                raise ValueError("ready 链接缺少身份、URL 或归因信息")
            if not self.external_drama_id or not self.title or not self.url:
                raise ValueError("ready 链接身份与 URL 不能为空")
        return self

def link_reuse_key(tenant_id: UUID, connection_id: UUID, application_id: str,
                   external_drama_id: str, config: dict) -> str:
    payload = [str(tenant_id), str(connection_id), application_id,
               external_drama_id, config]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
```

`protected_base=""` 仅在版权方契约明确不存在名称归因要求时使用；未知归因契约不是空串。`tiktok_minis_id` 是已发现关联，可空；`application_id` 是版权方应用 ID，不能代替 TikTok Minis ID。
内部 resolving/checking/creating/verifying 阶段统一对外映射为 pending，具体阶段另供任务详情展示，不能向 ResolvedLink.status 写入未定义枚举。

- [ ] **步骤 4：验证迁移和隔离。** `uv run alembic upgrade head`；`uv run pytest tests/modules/providers/test_contracts.py tests/modules/providers/test_tenant_repository.py -q`；预期全部通过，跨租户外键插入失败，同配置键稳定，不同配置不混用。
- [ ] **步骤 5：提交本任务。** `git add app/modules/providers app/alembic/versions/0003_provider_links.py tests/modules/providers`，然后 `git commit -m "providers: add tenant-scoped link contracts"`。

### 任务 2：核对真实版权方协议，实现独立连接适配器

**文件（Files）：**

- 创建：`backend/app/modules/providers/connections.py`、`adapters/contract.py`、`adapters/wangyan.py`、`adapters/jiashu.py`。
- 创建：`docs/integrations/providers-contract.md`、`backend/tests/modules/providers/fixtures/{wangyan,jiashu}-contract.json`。
- 测试：`backend/tests/modules/providers/test_provider_protocols.py`、`test_connection_isolation.py`。

**接口（Interfaces）：** 消费凭据加解密助手；输出 `ProviderSession(connection_id, application_id, http)`、`JiashuClient`、`WangyanClient`。两者提供 `search(title, page) -> dict`、`find_existing(drama_id, config, cursor) -> dict`、`create_step(step, payload) -> dict`、`read_link(remote_id) -> dict`，只返回脱敏业务数据；远端写动作由任务 3 调度。

- [ ] **步骤 1：读取协议函数并形成核对清单。** 仅阅读原资料仓库 `jiashu-link-cli.js` 的 `searchDrama/listChannels/createChannel/generateGuideUrl/saveGuideUrl/getGuideUrl` 和 `drama-link-cli.js` 的 `getDramaList/createPromoteLink/getPromoteLinksRaw`；不读取账号存储与历史输出。将 HTTP 方法、路径、认证字段名称、分页结束条件、错误码、配置冲突、链接和归因字段来源记入 `providers-contract.md`。

当前证据中的嘉书顺序是 `getChannelList → create（缺失时）→ getGuideUrl → generateGuideUrl/saveGuideUrl（URL 缺失时）→ getGuideUrl`。网眼 `getPromoteLinksRaw` 默认只查近 30 天，服务不得照搬该历史窗口作为“全量查无链接”的依据。协议核对必须确认历史分页或远端精确查询的覆盖范围，未证明完整时返回 `lookup_incomplete`，禁止据此新建。

网眼 CLI 中的 `campaign_name` 是由原始字段构造的；先核对版权方实际返回或当前网页渲染契约。优先保留远端原名，只有复核后的渲染契约及脱敏样例可进入适配器；不能仅凭旧注释宣布已验证。未核实归因格式返回 `attribution_contract_unverified`。嘉书应用与渠道前缀按连接发现，发现不到时返回明确错误，不写入历史账号默认值。

- [ ] **步骤 2：先写独立会话和请求参数测试。**

```python
import httpx
from app.modules.providers.adapters.jiashu import JiashuClient

def test_two_connections_do_not_share_session_headers():
    seen = []
    def handler(request):
        seen.append((request.headers["session"], request.url.params["channel"]))
        return httpx.Response(200, json={"code": "0000", "data": {"data": []}})
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        a = JiashuClient(http, session="session-a", application_id="app-a")
        b = JiashuClient(http, session="session-b", application_id="app-b")
        a.search("Moon", page=1)
        b.search("Moon", page=1)
    assert seen == [("session-a", "app-a"), ("session-b", "app-b")]
```

运行 `uv run pytest tests/modules/providers/test_provider_protocols.py -q`；预期缺少客户端或全局会话实现不满足隔离而失败。

- [ ] **步骤 3：实现显式上下文请求与错误映射。** 下列嘉书请求边界直接来自本地协议函数，真实登录及应用发现字段在本任务核对后以脱敏 fixture 固定；对 `10001` 只重新认证当前连接，对 `10005` 返回 `blocked_auth`，不能尝试其他租户账号。

```python
import httpx
from app.core.errors import DomainError

class JiashuClient:
    def __init__(self, http: httpx.Client, *, session: str, application_id: str):
        self.http = http
        self.session = session
        self.application_id = application_id

    def post(self, path: str, payload: dict) -> dict:
        response = self.http.post(
            "https://video-wechat-open.eastdrama.net" + path,
            params={"channel": self.application_id, "channel_from": 7,
                    "channel_type": 1, "site_type": "oversea_video_iaa"},
            headers={"session": self.session}, json=payload, timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        code = str(body.get("code"))
        if code == "10001":
            raise DomainError("provider_session_expired", "版权方登录已失效")
        if code == "10005":
            raise DomainError("provider_application_forbidden", "该连接没有应用权限")
        if code != "0000":
            raise DomainError("provider_rejected", "版权方拒绝本次操作")
        return body["data"]

    def search(self, title: str, page: int) -> dict:
        return self.post("/Oversea/Video/getVideoList",
                         {"keywords": title, "page": page, "page_size": 20})
```

网眼对应独立 `httpx.Client` 请求 `/api/distribute_admin/drama/list`、`/promote/link/list`、`/promote/link/create` 的完整版权方路径；连接实例传入认证头，不设置进程级默认头。`httpx` 的错误由适配器按“只读可重试、写请求结果未知”交给任务 3，不把所有异常统一设为自动重试。
在合法授权连接就绪后，核对登录、应用发现、历史链接回查与一条允许创建的链接，保存脱敏请求形状和字段类型；不保存令牌、密码、完整请求头。无连接时完成离线契约测试，并在交付记录注明真实验证尚未通过，不能把 mock 结果记为生产协议验收。

- [ ] **步骤 4：回归并检查日志脱敏。** `uv run pytest tests/modules/providers/test_provider_protocols.py tests/modules/providers/test_connection_isolation.py -q`；预期两连接无串号，缺少应用权限不切账号，日志不含测试 session 原值。
- [ ] **步骤 5：提交本任务。** `git add app/modules/providers tests/modules/providers ../docs/integrations/providers-contract.md`，然后 `git commit -m "providers: isolate verified provider sessions"`。

### 任务 3：实现复用、配置冲突与可恢复的外部写步骤

**文件（Files）：**

- 创建：`backend/app/modules/providers/link_steps.py`。
- 修改：`backend/app/modules/providers/repository.py`、`adapters/jiashu.py`、`adapters/wangyan.py`。
- 测试：`backend/tests/modules/providers/test_link_recovery.py`、`test_link_concurrency.py`。

**接口（Interfaces）：** 输出 `check_jiashu_config(existing: dict, requested: dict) -> None`、`claim_effect(session, *, effect_id: UUID, tenant_id: UUID, attempt_token: UUID) -> bool`、`run_link_item(session, *, context: TenantContext, item_id: UUID) -> None`。`run_link_item` 接受准备任务的单行 ID，先解析明确剧目，再检查完整历史与复用结果。

- [ ] **步骤 1：先写冲突和不盲目重放测试。**

```python
import pytest
from app.core.errors import DomainError
from app.modules.providers.link_steps import check_jiashu_config, should_send

def test_jiashu_existing_episode_cannot_be_silently_overwritten():
    with pytest.raises(DomainError) as error:
        check_jiashu_config({"drama_num": 1, "jump_url": "https://example.test/a"},
                           {"episode": 2})
    assert error.value.code == "config_conflict"

@pytest.mark.parametrize("status", ["sending", "result_unknown", "succeeded"])
def test_duplicate_delivery_never_replays_unknown_write(status):
    assert should_send(status) is False
```

- [ ] **步骤 2：运行失败测试。** `uv run pytest tests/modules/providers/test_link_recovery.py -q`；预期缺少冲突检测与发送状态规则而失败。

- [ ] **步骤 3：实现配置比对和原子认领。**

```python
from uuid import UUID
from sqlalchemy import update
from app.core.errors import DomainError
from app.modules.providers.models import ProviderEffect

def check_jiashu_config(existing: dict, requested: dict) -> None:
    if not existing.get("jump_url"):
        return
    actual = existing.get("drama_num")
    wanted = requested.get("episode", 1)
    if actual is None:
        raise DomainError("config_unverifiable", "已有链接缺少可核实的起播配置")
    if int(actual) != int(wanted):
        raise DomainError("config_conflict", "已有渠道的起播集数与本次请求不一致")

def should_send(status: str) -> bool:
    return status in {"pending", "confirmed_absent"}

def claim_effect(session, *, effect_id: UUID, tenant_id: UUID,
                 attempt_token: UUID) -> bool:
    result = session.execute(
        update(ProviderEffect).where(
            ProviderEffect.id == effect_id,
            ProviderEffect.tenant_id == tenant_id,
            ProviderEffect.status.in_(["pending", "confirmed_absent"]),
        ).values(status="sending", attempt_token=attempt_token)
    )
    return result.rowcount == 1
```

认领事务必须先提交，再调用远端；响应按同一 `attempt_token` 条件落库。崩溃遗留 `sending` 进入结果核实，不自动恢复为 pending。回查找到远端结果时补存 ID；只有远端契约能证明不存在才设 `confirmed_absent`；无法证明则保留 `result_unknown`。
单独持久化嘉书的渠道创建、链接生成、保存、回查四个步骤。已有渠道但链接为空时补生成/保存，不重复建渠道。对已经持有 URL 的渠道先比对全部有效配置，再复用；规范化字段由任务 2 的协议样例确定，无法核实的有效配置必须阻塞而非假定相同。
复用键防本地重复，`remote_scope_key` 额外防远端冲突：嘉书按连接＋应用＋渠道串行检查；网眼按已核实的远端唯一约束。先锁定 ProviderRemoteScope 行并设置 active_item_id，在全部子步骤结束前保留占用；不同配置请求不能因 request_digest 不同而绕开远端范围占用。sending 或 result_unknown 的任务未核实前不释放该范围。并发获取当前链接使用 PostgreSQL 唯一约束和短事务认领，不能只依赖 Redis 锁或单 Worker。

- [ ] **步骤 4：验证真实 PostgreSQL 并发与中断恢复。** `uv run pytest tests/modules/providers/test_link_recovery.py tests/modules/providers/test_link_concurrency.py -q`；并发测试使用两个独立 session 同时认领同一 Effect，断言恰有一个返回 True。另模拟渠道已创建但保存未完成、保存超时回查已存在、十账户共享一剧链接，预期没有第二次创建请求。
- [ ] **步骤 5：提交本任务。** `git add app/modules/providers tests/modules/providers`，然后 `git commit -m "providers: recover link writes without duplicates"`。

### 任务 4：接通批量准备、分页结果、候选纠错与 outbox

**文件（Files）：**

- 创建：`backend/app/modules/providers/service.py`、`tasks.py`、`router.py`。
- 修改：`backend/app/modules/providers/repository.py`、`backend/app/api/main.py`。
- 测试：`backend/tests/modules/providers/test_preparation_api.py`、`test_pagination.py`。

**接口（Interfaces）：** 输出顶部两项跨模块接口；输出 `choose_drama_candidate(session, *, context, input_id: UUID, external_drama_id: str) -> UUID`，只接受该输入行已返回的同应用候选，恢复单行取链；消费任务 3 `run_link_item`。

- [ ] **步骤 1：先写批量输入和分页契约测试。**

```python
from app.modules.providers.service import clean_lines

def test_batch_keeps_original_line_numbers_and_deduplicates():
    assert clean_lines([" Moon ", "", "Moon", "Sun"]) == [(1, "Moon"), (4, "Sun")]
    assert clean_lines(["Moon", "moon"]) == [(1, "Moon"), (2, "moon")]
```

运行 `uv run pytest tests/modules/providers/test_preparation_api.py -q`；预期缺少模块入口而失败。大小写不同的输入不在前置清洗中合并，最终明确到相同远端剧 ID 后才合并有效剧目，保留各原始行结果。

- [ ] **步骤 2：实现批量输入和持久化入口。**

```python
def clean_lines(lines: list[str]) -> list[tuple[int, str]]:
    result = []
    seen = set()
    for number, raw in enumerate(lines, start=1):
        value = raw.strip()
        if value and value not in seen:
            result.append((number, value))
            seen.add(value)
    return result
```

`create_preparation_request` 先按 tenant_id 校验连接和应用，再计算完整输入摘要；相同 `request_id` 与同摘要返回已有任务，摘要不同返回 `request_id_conflict`。在同一事务内插入 LinkPreparation、各行 LinkPreparationItem 及 `enqueue_after_commit(session, context=context, task_name="providers.prepare_item", task_key=f"provider-item:{item.id}", payload={"item_id": str(item.id)})`。队列只放 item_id 和基础上下文，不放凭据、整批链接或原始大 JSON。

- [ ] **步骤 3：实现键集分页和任务路由。** `read_preparation_results` 查询 tenant_id＋task_id，按 line_no、id 排序，默认每页 100 条，新增可选 page_size（50 或 100）并多取一条计算 next_cursor；UI 默认显式请求50条，非UI调用维持默认100条；游标包含 task_id、line_no、id，并验证不能用于另一任务。Worker 从基础上下文恢复操作者并校验连接归属，调用 `run_link_item`，异常写回原行。

```python
from base64 import urlsafe_b64encode, urlsafe_b64decode
import json
from uuid import UUID
from app.core.errors import DomainError

def encode_result_cursor(task_id: UUID, line_no: int, item_id: UUID) -> str:
    raw = json.dumps([str(task_id), line_no, str(item_id)]).encode()
    return urlsafe_b64encode(raw).decode()

def decode_result_cursor(cursor: str, task_id: UUID) -> tuple[int, UUID]:
    try:
        owner, line_no, item_id = json.loads(urlsafe_b64decode(cursor))
        if owner != str(task_id) or not isinstance(line_no, int) or line_no < 1:
            raise ValueError("cursor scope")
        return line_no, UUID(item_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DomainError("invalid_cursor", "结果游标不属于本次任务") from exc
```

注册 `POST /api/tenants/{tenant_id}/providers/link-preparations`、`GET /api/tenants/{tenant_id}/providers/link-preparations/{task_id}?cursor=&page_size=50`、`POST /api/tenants/{tenant_id}/providers/inputs/{input_id}/candidate`；POST 返回 202 和 task_id。连接管理使用 `/api/tenants/{tenant_id}/providers/connections` 的 GET/POST 与 `/api/tenants/{tenant_id}/providers/connections/{connection_id}` 的 PATCH；重新认证为其 `/verify` 子路径 POST，应用列表为其 `/applications` 子路径 GET。连接新增/重认证使用 manage，批量取链和候选恢复使用 provider_write，查询使用 read。连接列表、应用列表均分页，客户端不接收加密凭据字段。

- [ ] **步骤 4：验证 API、重复提交、任务回滚和完整分页。** `uv run pytest tests/modules/providers/test_preparation_api.py tests/modules/providers/test_pagination.py -q`；预期 205 行默认得到 100/100/5 三页，显式 page_size=50 得到50/50/50/50/5，跨租户与跨任务游标失败，相同请求只入队一次，事务回滚不投递，异常行保留并且 ready 行可供搭建使用。
- [ ] **步骤 5：提交本任务。** `git add app/modules/providers app/api/main.py tests/modules/providers`，然后 `git commit -m "providers: expose resumable batch link preparation"`。

### 任务 5：交付连接管理、链接结果与异常纠错页面

**文件（Files）：**

- 创建：`frontend/src/features/providers/ConnectionPanel.tsx`、`LinkResultTable.tsx`、`CandidateDialog.tsx`、`queries.ts`。
- 创建：`frontend/src/routes/_layout/tenants.$tenantId.providers.tsx`、`frontend/tests/providers.spec.ts`。
- 修改：自动生成的 `frontend/src/client/` 与导航注册文件；后端 `router.py` 的 OpenAPI 输出。

**接口（Interfaces）：** 消费任务 4 的 OpenAPI 生成客户端；页面按租户切换清除旧缓存，查询键包含 tenant_id、connection_id、application_id 和 task_id。链接 URL、归因基础名分别展示，成功行不增加再次选择框。

**前端交互补充草案，待本轮评审。** 本任务映射 **UI-09 版权方连接**，以[版权方功能设计第 10 节](../specs/2026-09-08-tiktok-02-provider-links-design.md#10-前端交互补充草案待本轮评审)和本轮原型评审结果为页面依据，不改变任务 1～4 的后端规则。统一路由为 `/tenants/:tenantId/providers?tab=connections|links`；从搭建问题进入时可附带 connection_id/task_id 定位本次结果。保留任务编号，不把该模块另做成剧目选择或报表中心。

页面容器采用标题 → Tabs“连接 / 链接记录” → 工具条 → Table → 分页；连接新增/编辑、应用/链接详情使用 Sheet，歧义候选使用 Dialog。顶栏沿用租户/BC上下文，版权方连接属于租户；BC切换不迁移连接或原草稿。平台人员进入租户后复用此页面。

- [ ] **步骤 1：先写页面回归。** 在 Playwright 中拦截已生成 API 对应的链接结果路由，用 `{items:[ready 行,needs_resolution 行],next_cursor:null}` 脱敏 fixture 返回；以下断言直接进入测试文件。

```typescript
import { test, expect } from "@playwright/test"

const taskId = "00000000-0000-4000-8000-000000000031"
const connectionId = "00000000-0000-4000-8000-000000000032"
const tenantId = "11111111-1111-4111-8111-111111111111"

test.beforeEach(async ({ page }) => {
  await page.route("**/api/tenants/*/providers/link-preparations/*", async (route) => {
    const common = { provider_kind: "wangyan", connection_id: connectionId,
      application_id: "app-a", language: "en", tiktok_minis_id: null,
      candidates: [], error_code: null, error_message: null }
    await route.fulfill({ json: { items: [
      { ...common, input_id: "00000000-0000-4000-8000-000000000033",
        line_no: 1, raw_input: "Moon", drama_id: "00000000-0000-4000-8000-000000000034",
        external_drama_id: "drama-1", title: "Moon", status: "ready",
        link_id: "00000000-0000-4000-8000-000000000035", url: "https://example.test/moon",
        protected_base: "provider-base-moon" },
      { ...common, input_id: "00000000-0000-4000-8000-000000000036",
        line_no: 2, raw_input: "Star", drama_id: null, external_drama_id: null,
        title: null, status: "needs_resolution", link_id: null, url: null,
        protected_base: null, candidates: [
          { external_drama_id: "drama-2", title: "Star", language: "en" },
          { external_drama_id: "drama-3", title: "Star", language: "es" },
        ] },
    ], next_cursor: null } })
  })
})

test("链接结果保留异常行，成功行不要求重新选择", async ({ page }) => {
  await page.goto(`/tenants/${tenantId}/providers?tab=links&task_id=${taskId}`)
  await expect(page.getByRole("heading", { name: "版权方连接" })).toBeVisible()
  await expect(page.getByText("Moon", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "处理候选" })).toBeVisible()
  await expect(page.getByRole("button", { name: "复制推广链接" })).toBeVisible()
  await expect(page.getByRole("checkbox", { name: "选择 Moon" })).toHaveCount(0)
})
```

测试使用模板已有登录状态与租户测试数据，上述 `page.route` 固定任务结果；禁止测试依赖真实版权方。页面支持 `task_id` 查询参数恢复结果查看。运行 `bunx playwright test tests/providers.spec.ts`，预期页面未实现而失败。

- [ ] **步骤 2：实现结果组件和操作权限。**

```tsx
import { Button } from "@/components/ui/button"

type LinkActionsProps = {
  status: string
  url: string | null
  onResolve: () => void
  canResolve: boolean
}

export function LinkActions({ status, url, onResolve, canResolve }: LinkActionsProps) {
  if (status === "ready" && url) {
    return <Button onClick={() => navigator.clipboard.writeText(url)}>
      复制推广链接
    </Button>
  }
  if (status === "needs_resolution" && canResolve) {
    return <Button onClick={onResolve}>处理候选</Button>
  }
  if (status === "needs_resolution") return <span role="status">等待投手处理候选</span>
  return <span role="status">{status === "blocked_auth" ? "连接需要重新认证" : "处理中或待恢复"}</span>
}
```

连接页只让管理员新增、更新凭据和重新认证；普通投手能选可用连接及应用并处理剧目候选。结果列表显示原行号、剧名、语种、应用、链接状态、原始归因名与错误原因，分页由 TanStack Query 读取 next_cursor。点击重新认证不清除历史链接；配置冲突展示现有和请求值，不能一键覆盖远端渠道。
`canResolve` 来自当前用户对该租户的 provider_write 权限；不能仅因返回 needs_resolution 就让只读成员看到写操作。已有 ready 链接的复制动作仍可供只读成员使用。

页面交互细化如下，随本轮原型统一评审：

- 连接表列为连接名称/版权方/账号显示标识、验证状态、已发现应用入口、最近验证时间、操作。非管理员没有新增、更新认证、重新验证按钮，详情不返回凭据字段。
- 新增连接 Sheet 显示连接名称、版权方、对应协议需要的认证字段；只用已核实接入契约生成表单，不要求运营填原始 JSON。底部固定“取消 / 保存并验证”；提交中阻止重复点击；认证失败保留名称、清空秘密字段。
- 编辑既有连接不能改变版权方类型或连接 ID；更新凭据用空输入接收，禁止回填星号再提交。关闭 Sheet 后验证任务状态仍从列表查看。
- 链接页签表列为剧名/剧目 ID（任务模式含原行号和原文）、版权方/应用/语言、状态、推广链接、归因名、操作。复制取完整原文，详情分别展示 URL 和归因基础名；成功行没有勾选框。
- 候选 Dialog 固定显示原始输入，表列为正式剧名、语言、应用、剧目 ID，每行“使用此剧目”；不默认选第一项。操作完成只更新对应行，其他就绪行不重新取链。
- 配置冲突 Sheet 用“已有配置 / 本次请求”两列逐项比较，给出返回来源草稿调整入口，不提供覆盖远端渠道按钮。未找到剧目的纠错回到该原始行，不新增全局别名维护。
- 所有表格界面默认 50 行，可选 100，显式请求所选页长；本次取链结果使用任务4补充的 page_size 参数，cursor 契约保持一致。搜索/过滤由服务端处理，改变筛选或页长重置游标；tab/task/connection 留在当前页面 URL，不伪造任意跳页和总数。
- 空连接、未验证无应用、验证后无可见应用、无历史链接、筛选无结果分别有文案；首次加载用 Skeleton；请求错误保留旧数据并显示重试。401 进入 UI-01，403 保留登录显示权限错误；版权方会话过期只标记该连接。
- 打开记录页面只读取已有结果，原型中验证/保存/回查/候选选择均模拟；不得因查看或翻页触发真实版权方或 TikTok 业务调用。

追加浏览器验收矩阵：

| 场景 | 可观察断言 |
| --- | --- |
| 管理员与投手/只读成员进入 UI-09 | 看到相同连接业务状态；只有管理员看到凭据操作，只读成员不能处理候选 |
| 同一版权方两条独立连接 | 筛选、应用列表、链接结果分别绑定 connection_id，旧请求不覆盖新连接 |
| 新增/更新认证失败 | 字段错误靠近输入，Sheet保留非敏感内容，秘密字段无回显，列表无假“可用”状态 |
| 一个 ready 行、一个 needs_resolution 行 | ready 行可复制无需勾选；候选必须明确选择，操作仅改变异常行 |
| 205 行本次结果 | 默认显示 50/50/50/50/5；切换 100 后显示 100/100/5。接口按所选 page_size 进行游标分页，筛选清空后恢复任务范围，原始行号不随翻页重新编号 |
| URL与归因名超长 | 列表截断但复制值完整；详情两个字段独立显示 |
| 标签切换或页面刷新 | 恢复 connections/links、connection_id/task_id，不触发重复取链 |
| 登录401、权限403、版权方认证异常 | 三者文案、跳转和可执行动作不同，不把版权方过期注销成平台登录失效 |

- [ ] **步骤 3：接入生成客户端与路由。** 根据基础计划脚本重新生成客户端，`queries.ts` 对接生成方法，`tenants.$tenantId.providers.tsx` 组合上述组件；不要另写同形 HTTP DTO。连接输入使用 shadcn 表单，凭据提交后清空浏览器字段且不写入 localStorage。
- [ ] **步骤 4：执行完整模块验收。** 后端 `uv run pytest tests/modules/providers -q`；前端 `bunx playwright test tests/providers.spec.ts`、`bun run build`。预期协议离线、隔离、并发、恢复、205 行分页及页面测试通过；真实协议验收单独记录已验证的连接类型和证据。
- [ ] **步骤 5：提交本任务。** 在应用根目录执行 `git add frontend/src/features/providers 'frontend/src/routes/_layout/tenants.$tenantId.providers.tsx' frontend/tests/providers.spec.ts frontend/src/client backend/app/modules/providers/router.py`，然后 `git commit -m "providers: add connection and link result workspace"`。

## 完成条件与衔接

五个任务通过后，策略预览计划消费 `prepare_links/get_link_results`；版权方模块不创建广告、不分配素材组。交付时分别列出离线测试结果和真实版权方协议验证状态；未核实的应用发现、历史查重覆盖或归因契约不允许以 ready 结果进入真实搭建。
