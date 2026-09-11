# TikTok MCP P1 Connections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户的 Superpowers 按需偏好优先：仅在执行时选择适合的工作流，本轮只编写计划，不启动实现或真实授权。

**Goal:** 让租户管理员独立授权官方 MCP、绑定一个 BC，并让业务任务通过固定连接完成账户与场景读取，不依赖本产品 TikTok App 配置。

**Architecture:** 连接、授权事实、BC 绑定和默认选路由账户模块持有；官方 API 和 MCP 分别解释自己的认证与权限证据。共享 gateway 只消费冻结路由，token 在工厂内部短事务读取；OAuth 候选尚未绑定 BC，走专门的候选只读工厂。P1 不开放任何素材或广告写入。

**Tech Stack:** Python >=3.14、uv、FastAPI、SQLModel/Alembic、真实 PostgreSQL/Redis、Celery prefork；P0 锁定的官方 MCP Python SDK；React、Bun、shadcn/ui、TanStack Router、Playwright。

**Spec:** [已批准双通道设计](../specs/2026-09-11-tiktok-dual-channel-mcp-design.md)，用户已于 2026-09-11 确认。

## Global Constraints

- “一个租户可以管理多个 BC；每个 MCP 连接明确绑定一个 BC。”
- “相同 BC 可配置两种通道及多条独立连接，但只能有一个当前默认执行连接。”
- “缺少 App 配置只影响官方 API，不能禁用 MCP。”
- “不能将‘工具存在’‘账户可见’‘一次只读成功’当作写权限。”
- “凭据、连接、BC、账户及任务始终带租户归属。”
- “同一 grant、主体、范围的正常刷新只改变凭据材料版本，冻结任务可使用新令牌继续执行”。
- “单纯发现批次 ID、检查时间或目录证据版本更新不构成授权语义变化”。
- 不读取 Codex token，不真实注册/授权/刷新/撤销，不调用线上 TikTok；外部替身只放 HTTP/MCP 传输边界。
- 只新增 Alembic 迁移，不改历史迁移；不自动创建分支或推送。生产迁移与联调属于后续明确授权操作。

## 执行入口、文件边界与交接

P0 先交付 `mcp/protocol.py` 的 `load_mcp_protocol()->McpProtocolProfile`、公共 metadata 与有来源的预期工具合同；`protocol-profile.json` 和 `tool-contracts.json` 不含注册凭据。profile 的固定 issuer/resource/端点、PKCE/token_auth_method/refresh_semantics/permission_source 与 schema 摘要均来自 P0 证据；运行时客户端注册配置是否就绪由 P1 单独检查。P0 不要求已经完成 TK-ADA 授权，实际 grant 的完整 tools/list 与权限核对在 Task 6 完成。认证条件未核实时配置状态为 `PROTOCOL_UNVERIFIED`，不能用假 App 配置、猜测端点或普通 `tt_user` OAuth 参数补齐。P0 拥有 `integrations/tiktok/contracts/context.py`、公共调用错误及准入合同；P1 实现 `gateway.py`，P2/P3 后续按顺序扩展 typed groups。`FrozenTikTokRoute` 为 frozen/extra-forbid Pydantic BaseModel，使用 `model_validate/model_dump(mode="json")` 持久化，不重复定义。

本计划路径以项目根为准。Python 测试命令从 `backend/` 执行；Bun 命令从 `frontend/` 执行；`bash scripts/generate-client.sh` 从项目根执行。先按 `backend/tests/database.py` 确认环境为专用 PostgreSQL 测试库及独立非零 Redis DB；不打印 DSN、不运行会删除卷的 `scripts/test-local.sh`。未提供测试环境时先准备本地测试依赖，不能把未运行写成通过。

| 文件组 | 本阶段职责 |
| --- | --- |
| `modules/accounts/models.py`、新 `connection_models.py`、新迁移 | 通道、三个版本、授权事实、候选/刷新尝试、BC 绑定和默认路由 |
| 新 `integrations/tiktok/mcp_auth/{service,refresh,bootstrap}.py` | 独立 MCP 授权及有界刷新、候选只读入口 |
| 新 `modules/accounts/{connections,routing}.py` | 管理动作、绑定、固定选路与每次执行授权 |
| 新 `integrations/tiktok/contracts/{accounts,scenes}.py` | 两通道一致的账户/场景 DTO 和 typed groups |
| 新 `integrations/tiktok/{official,mcp}/{accounts,scenes}.py` | SDK/MCP 只读调用与规范化；不暴露任意工具参数 |
| `modules/accounts/{tasks,discovery,capabilities}.py`、`modules/builds/{scene,scene_jobs}.py` | 持久化发现、证据、场景准备，替换 OAuth scope 及 SDK 直连依赖 |
| `modules/accounts/router.py`、新 `mcp_router.py`、连接页面 | 双入口、候选 BC 选择、默认连接和状态展示 |

P1 输出的路由和账户/场景合同供 P2/P3 使用。素材及广告读取由 P2/P3 的 typed groups 实现；其“读取迁移子任务”属于 P1 总体只读验收前置，不能因为本文件完成就宣称整个 P1 已覆盖素材/广告读取。

### Task 1: 连接模型与可审查迁移

**Files:** Modify `backend/app/modules/accounts/models.py`、`backend/app/alembic/env.py`、`backend/app/integrations/tiktok/auth.py`、`backend/app/modules/accounts/{tasks,discovery,capabilities,capability_models,router}.py`、`backend/app/modules/builds/{scene,scene_jobs,scene_job_models}.py`（这些调用方仅同步 TikTok 字段改名）；Create `backend/app/modules/accounts/connection_models.py`、`backend/app/alembic/versions/mcp01_tiktok_channel_connections.py`；Test `backend/tests/modules/accounts/test_channel_models.py`、`test_channel_migration.py`。

**Interfaces:** 消费 P0 `ChannelKind`。输出 `TikTokConnection.kind/display_name/service_profile/credential_revision/authorization_revision/adapter_contract_revision`；输出 `ConnectionAuthorization`、`ConnectionToolObservation`、`McpAuthorizationAttempt`、`McpRefreshAttempt`、`BCConnectionBinding`、`BCDefaultRoute` SQLModel。迁移将 TikTok 的旧 `credential_version` 物理改名为 `credential_revision` 并一次性更新全部 TikTok 引用，不保留双字段/双写 fallback；版权方同名字段保留。场景/任务对授权语义的切换分别由 Task 7 与 P2/P3 完成。

- [x] **Step 1: 添加模型失败用例。** `test_channel_models.py` 使用真实 Session 和现有 tenant fixture，先验证不同版本能独立变化：

```python
def test_token_revision_does_not_change_authority(session, context):
    row = TikTokConnection(tenant_id=context.tenant_id, kind="OFFICIAL_MCP",
        service_profile="tiktok-official", credential_revision=2,
        authorization_revision=7, adapter_contract_revision="accounts-v1")
    session.add(row)
    session.flush()
    row.credential_revision += 1
    session.flush()
    session.refresh(row)
    assert (row.credential_revision, row.authorization_revision) == (3, 7)
```

- [x] **Step 2: 从 backend 运行 `uv run --frozen pytest tests/modules/accounts/test_channel_models.py -q`，确认因新增字段/模型缺失失败。**
- [x] **Step 3: 实现模型和数据库不变量。** 绑定主键为 `(tenant_id,bc_id,connection_id)`，携带 kind 并通过复合外键与 connection 对应；对 kind 为 MCP 的 binding 建 `(tenant_id,connection_id)` 部分唯一索引。默认主键为 `(tenant_id,bc_id)`，复合外键引用 binding。工具观察记录保存tenant/connection/候选attempt、schema摘要、预期合同版本、完整分页标记、观察时间和非敏感调用证据；授权事实存上游 subject/grant（可空）、issuer/resource/scopes、授权连续关系与权限摘要；缺失上游字段保持空。attempt 存 actor、父连接版本、issuer/resource/redirect/state 摘要、期限、状态与加密候选；refresh 存所用凭据修订、claim/期限、加密候选和结果未知状态。

```python
# SQLModel.__table_args__ 中落实，不能仅由路由层限制。
Index("uq_mcp_one_bc", "tenant_id", "connection_id", unique=True,
      postgresql_where=text("kind = 'OFFICIAL_MCP'"))
ForeignKeyConstraint(["tenant_id", "bc_id", "connection_id"],
    ["bc_connection_binding.tenant_id", "bc_connection_binding.bc_id",
     "bc_connection_binding.connection_id"])
```

- [x] **Step 4: 写新增迁移并验证实际前置 head。** 执行 `uv run --frozen alembic heads` 确认只有一个真实 head，再执行 `uv run --frozen alembic revision --rev-id mcp01 --head head -m "TikTok channel connections"`，由 Alembic 生成实际 `down_revision`；预检 `mcp01` 尚未占用，不猜数字序号、不手填旧 head。旧连接回填 `OFFICIAL_API`；凭据修订承接旧版本、授权修订给确定初值。已有 API 多 BC 关系保留；唯一有效旧连接成为默认，多条有效旧连接留空待管理员选择。绝不重写历史请求或远端 ID。
- [x] **Step 5: 在迁移测试中创建上一 head 的独立临时 schema，放入一个单连接 BC、一个双连接 BC及历史 remote ID，升级后断言默认分别存在/为空且 ID 原样；再验证跨租户默认外键、MCP 第二 BC、负版本均失败。** 用 Alembic `Config` 和 `command.upgrade`，连接仅使用测试环境。运行 `uv run --frozen pytest tests/modules/accounts/test_channel_models.py tests/modules/accounts/test_channel_migration.py -q` 与 `uv run --frozen alembic check`，期望全部通过/无未生成模型差异。
- [x] **Step 6: 提交本任务。** 项目根先 `git status -sb`、`git rev-parse --show-toplevel`，显式暂存上述文件，检查 `git diff --cached`，提交 `accounts: model TikTok channels and BC bindings`；记录实施进度。

### Task 2: 账户与场景读取合同

**Files:** Create `backend/app/integrations/tiktok/contracts/accounts.py`、`scenes.py`；Test `backend/tests/modules/accounts/test_read_contracts.py`；Create `backend/app/integrations/tiktok/official/{accounts,scenes}.py`、`mcp/{accounts,scenes}.py`、`read_normalization.py`；Test `backend/tests/modules/accounts/test_dual_channel_reads.py`。现有 `integrations/tiktok/auth.py` 保持 API OAuth 所有权，新 `mcp_auth/` 不遮蔽它。

**Interfaces:** 输出以下 dataclass/Pydantic DTO 与 Protocol；ID 为精确字符串，不转换为浮点。均设置冻结/禁止额外字段，并校验页数、总数与重复 ID。`AuthorizationFacts` 的三项授权旗标允许 `None`，其含义为缺少可证明来源。

```python
@dataclass(frozen=True)
class AuthorizationFacts:
    subject_id: str | None
    grant_id: str | None
    issuer: str
    resource: str
    scopes: tuple[str, ...]
    read_authorized: bool | None
    upload_authorized: bool | None
    build_authorized: bool | None
    evidence_source: str
    observed_at: datetime

@dataclass(frozen=True)
class BusinessCenterFact:
    bc_id: str
    name: str

@dataclass(frozen=True)
class AdvertiserFact:
    advertiser_id: str
    name: str
    currency: str
    timezone: str
    remote_status: str
    authorized: bool | None

@dataclass(frozen=True)
class AccountRoleFact:
    advertiser_id: str
    role: Literal["ADMIN", "OPERATOR", "ANALYST"] | None

@dataclass(frozen=True)
class DirectoryPage[T]:
    items: tuple[T, ...]
    page: int
    page_size: int
    total_pages: int
    total_number: int | None
    last: bool
    evidence: CallEvidence

    @property
    def request_id(self) -> str | None:
        return self.evidence.request_id

class AccountsGateway(Protocol):
    def authorization_facts(self) -> AuthorizationFacts: ...
    def business_centers(self, *, page: int, page_size: int) -> DirectoryPage[BusinessCenterFact]: ...
    def advertisers(self, *, bc_id: str, page: int, page_size: int) -> DirectoryPage[AdvertiserFact]: ...
    def roles(self, *, bc_id: str, page: int, page_size: int) -> DirectoryPage[AccountRoleFact]: ...

class ScenesGateway(Protocol):
    def read_page(self, *, resource: SceneResource, advertiser_id: str,
                  page: int, minis_id: str | None) -> ScenePage: ...
```

`SceneResource` 复用 `modules/builds/scene_schemas.py`。`ScenePage` 定义 `resource,page,last,facts,evidence:CallEvidence`，只读 `request_id` 属性取 `evidence.request_id`；DirectoryPage/ScenePage 同时保存上游 request ID、MCP request ID 和 remote task ID（有则记录），不把JSON-RPC ID当幂等键。`SceneFacts` 为以下六个 Pydantic DTO 联合；page DTO 校验 resource 与 facts 类型一致，禁止无类型远端 JSON 穿过合同：

| DTO | 字段（沿 `scene_sdk.parse_page` 规范化结果） |
| --- | --- |
| `RoleFacts` | `matches:tuple[AccountRoleFact,...]`、`item_id_hashes:tuple[str,...]`、`total_number:int,total_page:int,seen:int` |
| `IdentityFacts` | 同上分页字段；matches 为 `IdentityMatch(identity_id:str,identity_type:Literal['BC_AUTH_TT'],identity_authorized_bc_id:str)` |
| `MinisFacts` | 同上分页字段；matches 为 `MinisMatch(minis_id:str,status:Literal['ACTIVE','INACTIVE'],type:Literal['MINI_SERIES','MINI_GAME'],regions:tuple[str,...])` |
| `CtaFacts` | `asset_ids:tuple[str,...],recommend_assets:tuple[CtaRecommendation,...]`；推荐项有 `asset_ids,asset_content:str` |
| `VboFacts` | `vo_status,vo_min_roas,roas_status_day0,roas_status_day7:str|None`，至少一项存在；数值字符串保持精确 |
| `RegionFacts` | `locations:tuple[RegionLocation,...]`；元素为 `region_code:str,location_id:str` |

- [x] **Step 1: 写失败用例覆盖授权未知不等于 false/true、19 位账户 ID、错误页数和 resource/facts 不匹配。**

```python
def test_large_advertiser_id_stays_exact():
    row = AdvertiserFact("90071992547409931", "Test", "USD", "UTC", "ENABLE", None)
    assert row.advertiser_id == "90071992547409931"
    assert row.authorized is None
```

- [x] **Step 2: 运行 `uv run --frozen pytest tests/modules/accounts/test_read_contracts.py -q`，确认缺模块失败。**
- [x] **Step 3: 实现以上类型和验证；business_centers 只供授权目录用途，运行期工厂不得让它扩张绑定范围。** `ScenePage.facts.model_dump(mode='json',exclude_none=True)` 仅在现有证据持久化边界转换，业务逻辑复用原 `SceneContext`；不丢失分页核查字段。
- [x] **Step 4: 实现四个纯适配器并运行传输边界合同测试。** `OfficialAccountsGateway/OfficialScenesGateway` 只接收工厂拥有的 SDK client；`McpAccountsGateway/McpScenesGateway` 只接收 P0 `BoundMCPClient`。调用固定 `call(operation=...,advertiser_id=...,arguments=...)`，operation 取 P0 已审查清单，不是 Codex 前缀。API advertiser 页内部核对授权集合/BC assets/details；所有实际调用逐次准入且整体有界，持久化分页由 worker 驱动。将现有 `scene_sdk.parse_page` 纯校验迁到 `read_normalization.py`，两适配器产出相同 ScenePage，不解析自然语言成功。尚未完成授权观察的预期 schema 只用于离线实现，不能令连接写能力就绪。
- [x] **Step 5: 运行 `uv run --frozen pytest tests/modules/accounts/test_read_contracts.py tests/modules/accounts/test_dual_channel_reads.py -q` 与 `uv run --frozen ty check app/integrations/tiktok/contracts app/integrations/tiktok/official app/integrations/tiktok/mcp`。** 固定输入验证两通道 DTO 等价、19位ID、精确金额、分页缺字段/重复ID、错误issuer、业务错误及文本JSON不满足合同均失败；将方法签名交接 P2/P3。
- [x] **Step 6: 检查仓库状态与根目录，显式暂存本任务文件，提交 `tiktok: define and adapt account and scene reads`。**

### Task 3: MCP 独立授权、候选凭据和 BC 绑定

**Files:** Create `backend/app/integrations/tiktok/mcp_auth/service.py`、`bootstrap.py`、`backend/app/modules/accounts/connections.py`、`mcp_router.py`；Modify `backend/app/api/main.py`、`backend/app/core/errors.py`、`backend/app/modules/accounts/schemas.py`；Test `backend/tests/modules/accounts/test_mcp_authorization.py`、`test_mcp_binding.py`。

**Interfaces:** 消费 P0 固定 `McpProtocolProfile`/读取器及其已验证认证传输，Task 1 模型和 Task 2 `AccountsGateway`。输出 `start_mcp_authorization(session,*,context:TenantContext,connection_id:UUID|None)->AuthorizationURL`；`accept_mcp_callback(*,database_engine:Engine,state:str,code:str)->UUID` 返回候选 attempt ID；`bind_candidate_bc(session,*,context:TenantContext,attempt_id:UUID,bc_id:str)->UUID` 返回发现任务 ID。候选读取入口 `open_candidate_accounts(*,database_engine:Engine,redis_client:Redis,context:TenantContext,attempt_id:UUID,task_deadline:datetime)->AbstractContextManager[AccountsGateway]` 不接受任意 connection/token。

- [ ] **Step 1: 添加无 App 配置测试及合成协议 fixture。** 在新测试目录 fixture 中显式覆盖上级 `app_config` 的三字段为空；只注入已固定协议的合成 metadata/HTTP 响应，不 monkeypatch 授权服务函数。

```python
def test_api_app_absence_does_not_block_mcp(session, monkeypatch):
    context = create_context(session, role="tenant_admin")
    for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, name, "")
    result = start_mcp_authorization(session, context=context, connection_id=None)
    assert urlsplit(result.url).hostname == "auth.example.test"
    assert session.exec(select(McpAuthorizationAttempt)).first().tenant_id == context.tenant_id
```

- [ ] **Step 2: 运行 `uv run --frozen pytest tests/modules/accounts/test_mcp_authorization.py -q`，确认缺实现失败。** fixture 的 fixed profile 是 P0 正式 profile 类型，认证 host 限 `auth.example.test`；上线 profile 来自部署固定配置，页面不提交 URL、client secret 或 issuer。
- [ ] **Step 3: 实现开始与回调状态机。** state 只存摘要、PKCE verifier 加密；认证尝试绑定管理员、tenant、固定 issuer/resource/redirect 和父授权/凭据版本。回调先一次性 claim，再有界换 token；校验回调与 token 语义使用 P0 真实合同。收到凭据先保存加密候选，成功后跳回当前 tenant 的候选选择页面，URL 仅含不敏感 attempt ID/结果码。重放、超时、跨租户、管理员已撤权均拒绝；回调响应 no-store/no-referrer。

```python
# 发布前重新鉴权；不能因为发起时是管理员就沿用旧角色。
require_tenant(session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id, action="manage")
if attempt.status != "CANDIDATE_READY":
    raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
```

- [ ] **Step 4: 实现候选 BC 分页和显式绑定。** 提供 tenant 路径下 `/tiktok/mcp/authorizations` POST、`/tiktok/mcp/candidates/{attempt_id}/bcs` GET、`/tiktok/mcp/candidates/{attempt_id}/binding` POST；固定回调 `/integrations/tiktok/mcp/callback`。候选只读工厂每次要求当前租户 manage 权限（仅曾是候选发起人不够），逐调用复核权限；本任务仅保存明确 BC 选择并创建持久化发现任务，不提前设 ACTIVE；Task 6 完整核实后原子发布连接/binding。候选测试使用 Task 2 实际适配器/合成传输，核对发现任务确已排队，不能 mock 绑定服务。失败、取消和部分目录都不覆盖旧 active 授权。第一个可用绑定可建默认，已有默认不变。

在本任务的 `mcp_auth/bootstrap.py` 实现 `observe_candidate_tools(*,database_engine:Engine,redis_client:Redis,context:TenantContext,attempt_id:UUID,task_deadline:datetime)->UUID`。候选 BC 第一次读取前先经 P0 BoundMCPClient.list_tools 完成所需合同观测；未观察 schema 的会话仅准枚举元数据。逐页按 P0 admit_candidate_call 限流并核实当前管理员权限，返回持久 observation ID；Task 6 消费同一记录完成目录发布，不重复实现。离线测试用合成 tools/list，不能登记真实 OBSERVED 证据。
- [ ] **Step 5: 实现独立停用/撤销动作并增加 HTTP 边界测试。** `disable_connection(session,*,context:TenantContext,connection_id:UUID,task_deadline:datetime)->None` 立即阻止本系统新调用、取消待派发，保留在途证据；`request_mcp_revocation(session,*,context:TenantContext,connection_id:UUID)->UUID` 生成独立可追踪授权撤销任务，只有明确管理员动作才派发。P0未证实revoke接口则显示不支持，不猜端点；任何迟到结果不得恢复disabled连接。测试回调重放、state 和 tenant 不匹配、未知 BC、第二 BC、候选分页失败、旧连接仍可用、operator 403、客户端未注册与协议未核实分别返回可识别配置状态。部署方通过 `MCP_CLIENT_REGISTRATION_REF` 指向本环境的注册材料，使用固定profile已核实的认证方法读取；public protocol JSON不存client secret，运行时不自动注册。运行 `uv run --frozen pytest tests/modules/accounts/test_mcp_authorization.py tests/modules/accounts/test_mcp_binding.py tests/modules/accounts/test_callback.py -q`。
- [ ] **Step 6: 状态与仓库根核对后显式暂存并提交 `accounts: authorize MCP connections per tenant`。** 提交前查看 staged diff，确认不含授权码/token/完整回调 URL。

### Task 4: 自动刷新、轮换未知与迟到结果围栏

**Files:** Create `backend/app/integrations/tiktok/mcp_auth/refresh.py`、`backend/app/modules/accounts/refresh_tasks.py`；Modify `backend/app/jobs/tasks.py`、Task 1 refresh 模型（Task 5 工厂消费此接口）；Test `backend/tests/modules/accounts/test_mcp_refresh.py`、`test_mcp_refresh_concurrency.py`。

**Interfaces:** 输出 `ensure_mcp_credentials(*,database_engine:Engine,redis_client:Redis,context:TenantContext,connection_id:UUID,task_deadline:datetime)->None`，只由 gateway/bootstrap 内部调用；它接收调用方冻结的本次 UTC task_deadline（不是静态协议配置），仅在旧 token 可证明覆盖本次有界任务及清理裕量时返回；否则持久化刷新并抛出待重调度错误，不把 token 返回给业务任务。输出 `process_mcp_refresh(*,database_engine:Engine,redis_client:Redis,attempt_id:UUID)->str`；状态为 `PENDING|CLAIMED|REQUEST_ARMED|CANDIDATE_READY|PUBLISHED|OUTCOME_UNKNOWN|REJECTED|SUPERSEDED`。

- [ ] **Step 1: 用真实 PostgreSQL 创建一个 active MCP 连接及固定授权摘要；传输替身返回同 grant 新 token，断言刷新只增凭据修订。** 新建共享 fixture `mcp_refresh_case` 返回 `(context, connection_id, attempt_id)`，造数使用 Task 1 SQLModel、加密工具和合成 token；fixture commit 到测试库以便 worker 独立 Session 可见，结束逐条清理本测试 ID。

```python
def test_rotation_keeps_authorization_revision(database_engine, redis_client, mcp_refresh_case):
    context, connection_id, attempt_id = mcp_refresh_case
    with Session(database_engine) as session:
        before = session.get(TikTokConnection, connection_id).authorization_revision
    assert process_mcp_refresh(database_engine=database_engine, redis_client=redis_client,
                               attempt_id=attempt_id) == "PUBLISHED"
    with Session(database_engine) as session:
        assert session.get(TikTokConnection, connection_id).authorization_revision == before
```

- [ ] **Step 2: 运行 `uv run --frozen pytest tests/modules/accounts/test_mcp_refresh.py -q` 确认失败；在 fixture 明确定义 `database_engine` 为受测试库保护的真实 engine，不替换数据库/Redis。**
- [ ] **Step 3: 实现互斥、持久化 attempt 和凭据 CAS。** 刷新前保存用过的凭据修订与 `REQUEST_ARMED`；网络外完成数据库事务。响应先保存完整候选，再按 connection credential/authorization revision、attempt claim 和停用状态原子替换；scope/主体/BC/issuer/resource 改变不作为例行刷新发布。角色缩减即时拒绝受影响操作；权限扩大/未知新 grant 进入管理员候选验证。

```python
if row.status == "DISABLED" or row.credential_revision != attempt.base_credential_revision:
    attempt.status = "SUPERSEDED"
elif attempt.status == "OUTCOME_UNKNOWN":
    raise DomainError("mcp_refresh_unknown", "令牌更新结果待核实，请重新连接")
```

- [ ] **Step 4: 实现未知处理。** 发送后失去响应保留旧加密凭据、旧有效期和 attempt，禁止自动再次消费旧 refresh token。P0 profile 明确提供可证恢复保证时才选择该策略；否则保持未知，旧 access token 在可证明仍有效且未撤销时可暂用，到期暂停远端调用。不得根据 HTTP 401 猜已发创建是否失败，也不得让 gateway 自动重放写调用。
- [ ] **Step 5: 用两个真实 worker/Session 和真实 Redis 验证只有一次 refresh HTTP 请求；传输替身模拟轮换后断线、候选持久化后中断、停用后迟到、重授权后迟到。** 断言请求次数、旧凭据保留、候选未跨连接发布、授权语义版本稳定/变更正确；运行 `uv run --frozen pytest tests/modules/accounts/test_mcp_refresh.py tests/modules/accounts/test_mcp_refresh_concurrency.py -q`；另断言剩余有效期不足本次taskdeadline时请求次数为0，运行中401只出现一次原业务调用。
- [ ] **Step 6: 状态与仓库根核对后显式暂存并提交 `accounts: fence MCP token refresh and unknown outcomes`。**

### Task 5: 固定 BC 路由、gateway 工厂与 worker 逐次授权

**Files:** Create `backend/app/modules/accounts/routing.py`、`backend/app/integrations/tiktok/gateway.py`；Modify `access.py`、`connections.py`、`schemas.py`、`router.py`；Test `backend/tests/modules/accounts/test_routing.py`、`test_worker_route_authorization.py`、`test_gateway_factory.py`。

**Interfaces:** 消费 P0 `FrozenTikTokRoute(tenant_id,bc_id,connection_id,channel,authorization_revision,adapter_contract_revision)`；输出下列固定签名，P2/P3 不另造选路规则。`verify_route` 不发远端请求、不读 token、不更换默认，只验证当前数据库权限；证据陈旧时返回明确需要刷新错误，调用方安排只读刷新。

```python
def freeze_route(session: Session, *, context: TenantContext, bc_id: str,
                 connection_id: UUID | None = None) -> FrozenTikTokRoute: ...
def verify_route(session: Session, *, context: TenantContext, route: FrozenTikTokRoute,
                 advertiser_id: str | None,
                 capability: Literal["read", "upload", "build"]) -> None: ...
def set_default_route(session: Session, *, context: TenantContext,
                      bc_id: str, connection_id: UUID) -> FrozenTikTokRoute: ...
```

- [ ] **Step 1: 将现有 `test_access.py` 的 account fixture 移到 `tests/modules/accounts/conftest.py` 供读取/路由复用，补种 binding/default 和当前授权事实。** 新测试先冻结默认，改默认后核验原 route；新增更小 UUID 的连接不能影响旧 route。

```python
def test_default_change_cannot_change_frozen_route(session, account_access_case):
    context, grant = account_access_case
    route = freeze_route(session, context=context, bc_id=grant.bc_id)
    session.delete(session.get(BCDefaultRoute, (context.tenant_id, grant.bc_id)))
    session.flush()
    verify_route(session, context=context, route=route,
                 advertiser_id=grant.advertiser_id, capability="read")
    assert route.connection_id == grant.connection_id
```

- [ ] **Step 2: 运行 `uv run --frozen pytest tests/modules/accounts/test_routing.py -q`，确认新模块缺失失败。**
- [ ] **Step 3: 实现 freeze/verify/default 操作。** 显式 connection 必须存在匹配 tenant+BC binding；未提供只读唯一默认，不存在时报 `bc_default_connection_required`。verify 重读用户/tenant/成员及连接状态、channel、授权/合同版本、归属冲突和账户 grant；账户为空仅用于 BC 级 read，upload/build 缺账户立即拒绝。角色、scope 和能力证据不完整时拒绝对应动作。

```python
if route.tenant_id != context.tenant_id:
    raise DomainError("connection_tenant_mismatch", "连接不属于当前租户")
if capability != "read" and advertiser_id is None:
    raise DomainError("account_required", "该操作必须指定广告账户")
```

- [ ] **Step 4: 删除 `access.py` 的 connection UUID 排序选路。** `resolve_account_access(...,connection_id:UUID|None=None)` 先 freeze 再基于固定连接找 grant；来源上传账号可在该连接内确定性选择，不能跨连接兜底。增加 `PUT /tenants/{tenant_id}/bcs/{bc_id}/default-connection`，请求只接受 connection ID；管理员管理权限和绑定外键双重验证。
- [ ] **Step 5: 实现任务作用域工厂并绑定 P0 callbacks。** 输出下列签名；`TikTokGateway` 初始仅含 `accounts:AccountsGateway` 和 `scenes:ScenesGateway`。工厂开会话前逐次检查 route，MCP ensure/刷新完成后在短事务取出当前加密凭据并解密，退出事务再建立客户端；token 仅在工厂拥有的会话存活，不传到任务/group 调用参数或持久化 route。官方客户端也在工厂内部取 token，只有 API 分支要求 Marketing API app 配置。

```python
def open_tiktok_gateway(*, database_engine: Engine, redis_client: Redis,
                        context: TenantContext, route: FrozenTikTokRoute,
                        task_deadline: datetime
                        ) -> AbstractContextManager[TikTokGateway]: ...
```

- [ ] **Step 6: 对 BoundMCPClient 绑定 `authorize(advertiser_id,operation)` 与 `admit(advertiser_id,operation)`。** authorize 每次独立 Session 调 `verify_route`，按受控 operation map 选择 capability；admit 使用 P0 的共享配额域/操作/账户机制。SDK 也逐实际请求走同样授权和准入。BC级目录/角色重检以 advertiser_id=None 的只读模式核验连接和BC后拉取新证据，不能要求待刷新的账户证据先新鲜；普通账户读取和写入仍需对应完整证据。会话请求使用保守MCP共享桶；任务deadline不可延长，token不足先调度刷新，运行中401不自动刷新并重放原请求。
- [ ] **Step 7: 覆盖 worker 发请求前用户被移除、连接停用、scope 缩减、credential revision 变化、authorization revision 变化、其他 tenant/BC/账户、过期证据和重检相同证据。** 运行 `uv run --frozen pytest tests/modules/accounts/test_routing.py tests/modules/accounts/test_worker_route_authorization.py tests/modules/accounts/test_gateway_factory.py tests/modules/accounts/test_access.py tests/modules/accounts/test_resolver.py -q`。
- [ ] **Step 8: 状态与仓库根核对后显式暂存并提交 `accounts: pin BC routes across background work`。**

### Task 6: 两通道账户读取与完整目录/权限发布

**Files:** Modify Task 2 的 `backend/app/integrations/tiktok/official/accounts.py`、`mcp/accounts.py`；Modify `integrations/tiktok/accounts.py`、`modules/accounts/{tasks,discovery,capabilities,capability_models}.py`；Test `backend/tests/modules/accounts/test_dual_channel_reads.py`、`test_mcp_directory_publish.py`、`test_mcp_permissions.py`。

**Interfaces:** 消费 Task 2 的 `OfficialAccountsGateway/McpAccountsGateway` 和 Task 3 候选发现任务，输出 `publish_mcp_directory(session:Session,*,context:TenantContext,run_id:UUID)->None`，置于 `modules/accounts/discovery.py`，只有完整证据才能发布；消费 P0 私有 MCP transport、固定工具映射、schema 哈希、错误合同、配额准入。业务目录继续输出现有 `AccountPublic/BCPublic`，授权依据来自 `AuthorizationFacts + AccountRoleFact`，不再由业务层读取 token scope。

- [ ] **Step 1: 写不完整候选发现不可发布测试。** fixture `incomplete_mcp_run` 用 Task 1 实际模型创建候选、所选 BC 及 `DiscoveryRun`，不创建末页证据，返回 run；与现有 `context` 归属一致。两通道适配器的合成SDK HTTP/MCP响应 fixture 复用 Task 2，只替换传输。

```python
def test_publish_rejects_incomplete_scan(session, context, incomplete_mcp_run):
    with pytest.raises(DomainError) as failure:
        publish_mcp_directory(session, context=context, run_id=incomplete_mcp_run.id)
    assert failure.value.code == "discovery_incomplete"
    connection = session.get(TikTokConnection, incomplete_mcp_run.connection_id)
    assert connection.status != "ACTIVE"
```

- [ ] **Step 2: 运行 `uv run --frozen pytest tests/modules/accounts/test_mcp_directory_publish.py -q`，确认缺发布函数失败。**
- [ ] **Step 3: 集成适配器与持久化发现阶段。** API 内部组合授权 advertiser 集合、BC assets 与 details；MCP 仅按 P0 已核实字段解释权限。每个 DTO 页限制 50 条，内部组合调用逐次准入并受总期限约束；大授权集合采用持久化发现阶段/分页，不在单次 worker 内无限扫描。目录适配器需要跨页中间态时交由 `DiscoveryRun.work` 保存引用而非 token。若官方接口无分页完整性依据则阻止发布，不伪造 last。单页内部读取授权集合也执行P0响应容量上限，超限返回明确不可核实错误，不进入无界扫描。
- [ ] **Step 4: 消费 Task 3 observe_candidate_tools 的完整 tools/list 证据并核对发布时间有效性。** 走当前租户自己的 grant，持久化分页、工具名、schema规范化摘要、P0预期合同版本和观察时间；与预期schema逐项校验，通过后发布连接级 capability evidence。缺页、冲突schema、缺权限来源只阻止对应能力，不用预期清单冒充实际观察。未发生真实观察不得宣称完成P0真实协议验收；离线边界测试只证明解析与状态机。必要的重新观测仍只枚举元数据，沿同一个 bootstrap 接口和 P0 准入。
- [ ] **Step 5: 迁移 discovery/capabilities worker。** 每次出站前 verify route；候选发现用受限 bootstrap。完整扫描保存 staging，校验连续页、重复 ID、总数、ownership 与绑定后原子发布；中途失败不覆盖已发布 grant。记录支持能力、明确授权、角色、检查时间与联调证据分别独立；只有支持+授权+账户角色+完整回读合同都满足才产生 can_build/can_upload，P1 开放开关仍保持写禁用。

```python
grant.can_build = bool(facts.build_authorized is True and role in {"ADMIN", "OPERATOR"}
                       and supported_build and complete_readback)
grant.permission_state = "VERIFIED" if authorization_proven and role is not None else "UNKNOWN"
```

- [ ] **Step 6: 测试“工具存在+只读成功但 scope 未知”仍阻止写；换连接不能借用旧 grant；目录页失败保留旧快照；权限缩减即时限制但目录完整发布仍原子化。** 运行 `uv run --frozen pytest tests/modules/accounts/test_dual_channel_reads.py tests/modules/accounts/test_mcp_directory_publish.py tests/modules/accounts/test_mcp_permissions.py tests/modules/accounts/test_discovery.py tests/modules/accounts/test_discovery_worker.py tests/modules/accounts/capabilities -q`。
- [ ] **Step 7: 状态与仓库根核对后显式暂存并提交 `accounts: read and verify directories through both channels`。**

### Task 7: 场景只读、刷新稳定性与场景数据收尾

执行依赖：P3 Task 1–2 已在本任务前完成只读契约及父路由持久化，见总览的跨文件执行顺序。

**Files:** Modify Task 2 的 `backend/app/integrations/tiktok/official/scenes.py`、`mcp/scenes.py`；Modify `backend/app/modules/builds/{scene_sdk,scene,scene_jobs,scene_job_models,scene_models,drafts,execution}.py`、`backend/app/modules/accounts/{models,tasks}.py`；Create `backend/app/alembic/versions/mcp02_tiktok_scene_routes.py`；Test `backend/tests/modules/builds/scene/test_dual_channel_scenes.py`、`test_scene_route_versions.py`。

**Interfaces:** 两适配器实现 Task 2 `ScenesGateway`。`ensure_scene_preparation` 增加必填 `route:FrozenTikTokRoute`，父任务传入同一 route；独立页面操作在调用前 freeze 一次。`SceneJob` 保存 route 和稳定 scope digest，保持对上层的 `ScenePreparation/SceneContext` 输出类型。当前调用点为 `drafts.py:570/629` 和 `execution.py:372`，本任务同步迁移全部三处：草稿独立读取准备 freeze 一次，执行调用方使用已完成的 P3 `load_preview_route(session,*,context,preview_id)`。不能漏参数或给历史任务伪造当前授权上下文。

- [ ] **Step 1: 添加场景版本测试：同一 route 在 token 轮换、证据时间/发现 run 改变后仍有效；scope、主体或合同版本改变即拒绝。** 在测试中通过真实模型更新对应字段，不 mock `_scope`。

```python
def test_route_survives_material_credential_rotation(session, account_access_case):
    context, grant = account_access_case
    route = freeze_route(session, context=context, bc_id=grant.bc_id)
    connection = session.get(TikTokConnection, route.connection_id)
    connection.credential_revision += 1
    session.flush()
    verify_route(session, context=context, route=route,
                 advertiser_id=grant.advertiser_id, capability="read")
```

- [ ] **Step 2: 运行 `uv run --frozen pytest tests/modules/builds/scene/test_scene_route_versions.py -q`，确认新增“同一SceneJob在轮换后可完成、scope改变时禁止发布”的集成用例因旧scope依据失败；上方verify_route用例是已完成Task5的防回归断言，不要求它再次变红。** 为 scene 测试目录提供同样造数 fixture；只共享 fixture helper，不让测试文件互相 import。
- [ ] **Step 3: 接入 Task 2 已实现的场景适配器与共用规范化器；删除场景业务中的 SDK 请求/解析直连，保留官方适配器中的固定 SDK 方法。** identity/minis 分页及 regions/vbo/cta 现有字段验证保持；误 BC identity、错误角色、重复 ID、缺 total_number 和大于边界的页均失败，不用自然语言成功兜底。
- [ ] **Step 4: 修改 scene scope digest 与任务执行。** 摘要包含冻结路由、策略/版权方/Minis 依据及约束版本，排除 token 修订、发现 run ID和观察时间。每次调用使用公共工厂 `gateway.scenes.read_page(...)`；延迟任务不再解析默认，场景只能基于自己的固定 connection 取 capability evidence。
- [ ] **Step 5: 新迁移仅补场景任务自己的 route。** 确认单 head 后运行 `uv run --frozen alembic revision --rev-id mcp02 --head head -m "TikTok scene routes"`，由实际单一 head 生成依赖，此时应已包含 P3 Task 2 父路由迁移；根负责与 P2/P3 迁移串行合入。 可证明原 connection 的 SceneJob 增冻结 route；缺证据的 scene job 终止并要求基于明确连接重新准备。此迁移不处理 build preview、submission 或执行步骤：历史广告伴随上下文和旧预览过期由 P3 独占。P1仅核实 Task 1 字段改名完整且场景不再把 credential revision 当授权语义。运行 `rg -n 'credential_version|TIKTOK_APP_ID|sdk_client' app/modules/accounts app/modules/builds` 审查每处归属，素材/广告剩余调用交 P2/P3，不宣称全项目迁完。
- [ ] **Step 6: 运行 `uv run --frozen pytest tests/modules/builds/scene tests/modules/accounts -q`、`uv run --frozen alembic check`；运行 `uv run --frozen ty check app/integrations/tiktok app/modules/accounts app/modules/builds`。** 状态与仓库根核对后提交 `builds: preserve frozen routes through scene refreshes`。

### Task 8: 连接页面、HTTP 合同与 P1 验收

**Files:** Modify `backend/app/modules/accounts/{router,mcp_router,schemas}.py`、`frontend/src/features/accounts/ConnectionsPage.tsx`、`AccountsPage.tsx`、`presentation.tsx`、`frontend/tests/tenants-accounts.spec.ts`、生成的 `frontend/src/client/`；Create `frontend/src/features/accounts/McpAuthorizationSheet.tsx`；Test `backend/tests/modules/accounts/test_channel_router.py`；Update `docs/implementation-progress.md`。

**Interfaces:** `/tiktok/configuration` 返回两个独立通道准备状态，每项 `kind,status,configured,code`；API 原缺配置只影响 API。`ConnectionPublic` 增 kind、display_name、binding BC、是否默认、授权/发现/刷新进度、可证明能力，仍为显式 allowlist，不序列化 ORM。候选列表/绑定/default 路由消费 Task 3/5；读账户页面操作必须明确选当前 BC 的连接。

- [ ] **Step 1: 在现有 Playwright HTTP boundary 增 `mcpReady:true,apiReady:false` 测试选项及候选响应；API handler fixture 独立提供两通道配置。** 保留真实页面、路由、生成客户端、query 和 dialog，只替换 HTTP。

```typescript
test("MCP authorization remains available without an API app", async ({ page }) => {
  await boundary(page, { role: "tenant_admin", mcpReady: true, apiReady: false })
  await page.goto(`/tenants/${A}/accounts?tab=connections`)
  await page.getByRole("button", { name: "新增连接", exact: true }).click()
  await page.getByRole("button", { name: "官方 MCP", exact: true }).click()
  await expect(page.getByRole("button", { name: "前往 TikTok 授权" })).toBeEnabled()
})
```

- [ ] **Step 2: 启动本地 frontend 开发服务后运行 `bunx playwright test tests/tenants-accounts.spec.ts --project=workspace --grep 'MCP authorization'`，确认新入口尚不存在而失败。** 不连接生产 URL。
- [ ] **Step 3: 实现双通道入口、BC 候选选择、连接详情及默认切换。** 管理员看“新增连接 → 官方 API/官方 MCP → 前往授权 → 选择 BC → 发现中 → 可用”；普通操作员仅查看。刷新故障展示是否仍可用/需要重连，结果未知显示明确原因；不展示注册 secret、App 参数、token 或完整回调 URL。使用 shadcn Sheet/Select/Alert 和现有中性黑主按钮。
- [ ] **Step 4: 为新/旧 HTTP 接口添加严格 response allowlist 与错误码测试，注册 FastAPI 路由后运行 `uv run --frozen pytest tests/modules/accounts/test_channel_router.py tests/modules/accounts/test_router.py -q`；项目根运行 `bash scripts/generate-client.sh`，不手工修改生成类型。**
- [ ] **Step 5: 扩展浏览器测试覆盖取消/失败保留旧连接、绑定单 BC、默认改变后旧任务路由不变、tenant 切换清除候选缓存、operator 不显示管理、scope 未知明确阻止。** 从 frontend 运行 `bunx playwright test tests/tenants-accounts.spec.ts --project=workspace` 与 `bun run build`；后端跑 `uv run --frozen pytest tests/modules/accounts tests/modules/builds/scene -q`。
- [ ] **Step 6: 记录 P1 实际范围和待 P2/P3 消费合同。** 核对账户、OAuth/refresh、BC 路由、场景四组离线验收；素材/广告读取未完成时列为整体 P1 尚未关闭项。核对所有新增错误映射、无 token 任务载荷、无任意 URL/工具名参数、无写能力默认打开。运行 `git status -sb` 与 `git rev-parse --show-toplevel`，逐文件暂存检查后提交 `accounts: expose tenant-managed MCP connections`；不推送、不部署。


## 计划自身校验与阶段关闭条件

- 文档检查覆盖 Task 1 数据迁移、Task 3/4 授权生命周期、Task 5 冻结选路与工厂、Task 6 证据发布、Task 7 场景、Task 8 页面；真实权限或 schema 尚未观察的功能保持未开放。
- P0 提供公开协议及预期合同，P1 授权后取得实际观察，再由 P2/P3 首批只读任务补素材/广告读取；任务相互依赖只通过上文具名 DTO、route 和 factory，不传播 token。
- 所有复合外键、锁/轮换/迟到回执测试使用真实 PostgreSQL/Redis；仅离线测试成功不代表已授权、不代表生产能力已验证。本轮没有运行这些未来测试。
