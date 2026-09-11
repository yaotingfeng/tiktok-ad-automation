# TikTok MCP P3 广告闭环与集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 经冻结的租户连接完成 CTA、三级广告创建及完整回读，发生未知结果时只核查原操作，并交付可审阅的集成和发布证据。

**Architecture:** 保留现有预览、提交、outbox、claim/nonce 与不可变请求；业务层只消费 typed gateway。API/MCP adapter 各自负责线协议，数据库伴随上下文保存原执行路线，未知结果围栏独立于本地租约。

**Tech Stack:** Python >=3.14、SQLModel/PostgreSQL、Redis/Celery prefork、Pydantic、固定版本 TikTok Python SDK/MCP Python SDK、React/shadcn/ui、Playwright/Bun。

**Spec:** `docs/superpowers/specs/2026-09-11-tiktok-dual-channel-mcp-design.md`，依赖 P0/P1/P2 对应阶段计划。当前文件仅是计划，不授权部署、OAuth 操作或真实广告写入。

## Global Constraints

- “三级广告直接以 `ENABLE` 创建”；“所有剧目投向全部输入账户”；“上传不绑定剧目”；保存真实来源及目标素材 ID。
- “没有 Agent、提示词执行或模型决策依赖”；“没有自建 TikTok MCP server”。
- tenant、BC、advertiser、connection、authorization_revision、adapter_contract_revision 均须校验；正常 credential_revision 刷新不得使冻结任务过期。
- 默认连接仅在新准备开始时解析一次；后代任务继承父路由，延迟派生也不能重新读取默认。
- MCP HTTP 200、isError=false、自然语言成功、非零业务码均不能独立证明副作用结果；遵循 P0 已验证业务 envelope 和重试语义。
- 远端 ID 先提交，再关闭客户端；超时/清理失败/worker 死亡不得发起竞争写或切通道补建；空页不是不存在证明。
- PostgreSQL/Redis 锁、准入和进程终止测试使用真实服务；替身只在 SDK HTTP/MCP transport 边界。
- 每任务完成后更新 `docs/implementation-progress.md`，核实 `git status -sb` 与 `git rev-parse --show-toplevel`，只暂存本任务明确文件并做聚焦提交；本计划编写不执行提交。

## 文件与阶段边界

P0 负责公共 context/evidence/errors、transport、协议映射和能力开放门槛。P1 负责连接/路由/账户/场景；P2 负责素材及封面 worker。P3 负责 `contracts/builds.py`、两个 builds adapter、广告执行/恢复、父路由迁移及广告页面。Task 1–2 在 P1 Task 6 后前置实施；Task 2 不依赖 P2 写入。P1 Task 7 与 P2 Task 2 分别顺序更新 execution 的场景、素材准备调用点，root 核对所有调用方，不并行编辑同一文件。SDK 纯请求编译及纯比对移到业务文件，旧文件迁移完即删除，不留运行时 fallback。

所有下列 backend 命令从 `backend/` 执行，frontend 命令从 `frontend/` 执行。数据库命令必须使用现有测试环境及 `tests/database.py` 防误连约束；不得拿本地应用库或生产库运行测试。

### Task 1: 创建类型与只读适配（随 P1 只读里程碑前置）

**Files:** Create `backend/app/integrations/tiktok/contracts/builds.py`, `backend/app/integrations/tiktok/adapters/sdk_builds.py`, `backend/app/integrations/tiktok/adapters/mcp_builds.py`, `backend/app/modules/builds/request_compiler.py`, `backend/app/modules/builds/readback_compare.py`, `backend/tests/contracts/test_tiktok_build_contract.py`, `backend/tests/integrations/tiktok/build_wire.py`; Modify `backend/tests/integrations/tiktok/conftest.py`, `backend/app/integrations/tiktok/gateway.py`; later remove `backend/app/modules/builds/sdk_requests.py`, `backend/app/modules/builds/readback_sdk.py` after Task 4 callers migrate.

**Interfaces:** Consumes P0 `FrozenTikTokRoute`、`contracts/common.py` 的 `CallEvidence(request_id:str|None=None,mcp_request_id:str|None=None,remote_task_id:str|None=None)`、`RemoteCallError(code:str,*,effect:Literal['NOT_SENT','UNKNOWN','REJECTED_NO_EFFECT'],evidence:CallEvidence)`，以及固定 `mcp/protocol-profile.json`/`mcp/tool-contracts.json`；produces `BuildOperations.create(*,attempt_id:UUID,intent:CreateIntent)->CreatedObject`, `read_page(*,query:BuildReadQuery)->BuildPage`, `read_adgroup_status(*,advertiser_id:str,adgroup_id:str)->AdGroupStatus`。adapter 具体类型为 `ApiBuildOperations(client:Any)` 与 `McpBuildOperations(client:BoundMCPClient)`，其中 Any 仅封装在 SDK 边界；gateway 实例已绑定 route，方法不得重新解析连接。`attempt_id` 是本地操作关联，不声称为上游幂等键。

- [ ] **1. 写契约失败测试，精确 ID/金额及拒绝多余字段。**

```python
from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.integrations.tiktok.contracts.builds import CampaignCreate

def test_campaign_keeps_exact_values_and_direct_enable():
    value = CampaignCreate(advertiser_id="90071992547409939999", name="冻结名称", budget=Decimal("100.01"))
    assert value.advertiser_id == "90071992547409939999"
    assert value.budget == Decimal("100.01")
    assert value.operation_status == "ENABLE"
    with pytest.raises(ValidationError):
        CampaignCreate(advertiser_id="a", name="c", budget=Decimal("1"), operation_status="DISABLE")
    with pytest.raises(ValidationError):
        CampaignCreate(advertiser_id="a", name="c", budget=Decimal("1"), unverified_option=True)
```

- [ ] **2. 跑红测试。** `uv run pytest tests/contracts/test_tiktok_build_contract.py -q`，预期缺少新模块。
- [ ] **3. 实现冻结模型及纯编译器，不把任意 dict 当业务类型。** 以下模型是共享名称与输入边界；所有 ID 用严格非空字符串，金额用有限正 Decimal。`model_config` 在所有嵌套模型继承，不用 SDK 类型。

```python
from decimal import Decimal
from typing import Annotated, Literal, Protocol
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

Id = Annotated[str, Field(strict=True, min_length=1)]
Money = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
class CampaignCreate(FrozenModel):
    kind: Literal["CAMPAIGN"] = "CAMPAIGN"
    advertiser_id: Id
    name: Id
    budget: Money
    operation_status: Literal["ENABLE"] = "ENABLE"
    objective_type: Literal["APP_PROMOTION"] = "APP_PROMOTION"
    app_promotion_type: Literal["MINIS"] = "MINIS"
    campaign_type: Literal["REGULAR_CAMPAIGN"] = "REGULAR_CAMPAIGN"
    catalog_enabled: Literal[False] = False
    budget_mode: Literal["BUDGET_MODE_DYNAMIC_DAILY_BUDGET"] = "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
    budget_optimize_on: Literal[True] = True
class AdGroupCreate(FrozenModel):
    kind: Literal["ADGROUP"] = "ADGROUP"
    advertiser_id: Id
    campaign_id: Id
    name: Id
    minis_id: Id
    roas_bid: Money
    location_ids: tuple[Id, ...] = Field(min_length=1)
    schedule_start_time: Id
    operation_status: Literal["ENABLE"] = "ENABLE"
    promotion_type: Literal["MINI_APP"] = "MINI_APP"
    optimization_goal: Literal["VALUE"] = "VALUE"
    optimization_event: Literal["ACTIVE_PAY"] = "ACTIVE_PAY"
    bid_type: Literal["BID_TYPE_NO_BID"] = "BID_TYPE_NO_BID"
    deep_bid_type: Literal["VO_MIN_ROAS"] = "VO_MIN_ROAS"
    billing_event: Literal["OCPM"] = "OCPM"
    placement_type: Literal["PLACEMENT_TYPE_NORMAL"] = "PLACEMENT_TYPE_NORMAL"
    placements: tuple[Literal["PLACEMENT_TIKTOK"], ...] = ("PLACEMENT_TIKTOK",)
    schedule_type: Literal["SCHEDULE_FROM_NOW"] = "SCHEDULE_FROM_NOW"
class CreativeAsset(FrozenModel):
    video_id: Id
    image_id: Id
class AdCreate(FrozenModel):
    kind: Literal["AD"] = "AD"
    advertiser_id: Id
    adgroup_id: Id
    name: Id
    identity_id: Id
    identity_authorized_bc_id: Id
    identity_type: Literal["BC_AUTH_TT"] = "BC_AUTH_TT"
    text: Id = Field(max_length=100)
    landing_page_url: Id
    portfolio_id: Id
    assets: tuple[CreativeAsset, ...] = Field(min_length=1, max_length=50)
    operation_status: Literal["ENABLE"] = "ENABLE"
class CtaAsset(FrozenModel):
    asset_ids: tuple[Id, ...] = Field(min_length=1, max_length=50)
    asset_content: Id
class CtaCreate(FrozenModel):
    kind: Literal["CTA"] = "CTA"
    advertiser_id: Id
    assets: tuple[CtaAsset, ...] = Field(min_length=1, max_length=50)
CreateIntent = Annotated[CampaignCreate | AdGroupCreate | AdCreate | CtaCreate, Field(discriminator="kind")]
```

实现 `request_compiler.decode_intent(kind:str,body:dict[str,object])->CreateIntent` 及 `encode_intent(intent:CreateIntent)->dict[str,object]`，逐字段搬迁现有 `compile_request`、`ad_assets`、`cta_portfolio`；未知字段拒绝，不用任意 extras 通道。用当前 `test_sdk_contract.py` 和 `scene._assemble_scene` 的全部实际请求验证 encode/decode 往返值不丢失，特别包含 campaign_type、targeting_spec、creative_info、CTA 选择及排期；不能通过删除当前字段让新模型通过。新增校验 UTC `YYYY-MM-DD HH:MM:SS` 排期、CTA asset ID 总并集最多50、同视频/封面身份必需且不可为空；时间只在现有首次 arm 时确定，编码不能重新取时钟。冻结业务请求仍是原不可变请求；adapter 翻译后的 wire 摘要存 attempt 伴随证据。

定义输出：`CreatedObject(kind:Literal['CTA','CAMPAIGN','ADGROUP','AD'],remote_id:Id,operation_status:str|None,evidence:CallEvidence)`；`BuildRecord(remote_id:Id,intent:CreateIntent|None,operation_status:str|None,missing_fields:tuple[str,...])`。缺少回读字段保留 `intent=None` 和具体 missing_fields，禁止用请求值补齐。`BuildReadQuery(intent:CreateIntent,remote_id:Id|None=None,page:int=1)`；`BuildPage(rows:tuple[BuildRecord,...],page:int,total_pages:int,total_number:int,complete:bool,evidence:CallEvidence)`；`AdGroupStatus(advertiser_id:Id,adgroup_id:Id,operation_status:str|None,evidence:CallEvidence)`。ID、父级、所有业务金额/设置、目标 VID/封面、CTA 内容与选择 ID 均参与比对，状态不是审核/消耗结论。

```python
class BuildOperations(Protocol):
    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject: ...
    def read_page(self, *, query: BuildReadQuery) -> BuildPage: ...
    def read_adgroup_status(self, *, advertiser_id: str, adgroup_id: str) -> AdGroupStatus: ...
```

本任务只迁移 GET/readback；create 方法先明确抛出 `RemoteCallError("build_capability_not_enabled",effect="NOT_SENT",evidence=CallEvidence())`，到 Task 3 才开放实现。API 迁移现有 SDK 只读调用并保留 `smart_plus_ad_id`；MCP 仅使用 P0 固定 mapping/schema，并消费 `BoundMCPClient.call(*,operation:str,advertiser_id:str|None,arguments:dict[str,Any],deadline:datetime|None=None)->McpBusinessResponse`。对 portfolio 未知 ID 的恢复，只有 P0 证明有完整关联查询才搜索，否则保留未知并阻断后继。分页完整性在 adapter 校验，跨页一致性在恢复业务层验证。MCP mapping 缺字段、不能完整回读、已知服务端非幂等自动重试且无保证时，将 build capability 关闭，不临时改默认参数。

- [ ] **4. 实现 Task 6 写明接口的 BuildWire read/HTTP 部分及两通道 fixture（create 队列部分 Task 3 扩展），两 adapter 用同一离线用例断言标准 DTO：大 ID、Decimal、父级错配、Smart+ ad ID、状态补查、CTA 单独回执、重复/缺页、结构化错误与文本 JSON 严格契约、schema 漂移。** `uv run pytest tests/contracts/test_tiktok_build_contract.py tests/modules/builds/test_sdk_contract.py -q`；预期全过且无外网调用。
- [ ] **5. 记录并提交** `feat(builds): add typed dual-channel create and readback contracts`，逐项显式暂存本任务文件；旧文件删除留到 Task 4。

### Task 2: 冻结父子路线与历史伴随上下文

**Files:** Create `backend/app/modules/builds/route_models.py`, `backend/app/modules/builds/routes.py`, `backend/app/alembic/versions/mcp_build_routes_frozen_build_routes.py`, `backend/tests/modules/builds/test_frozen_routes.py`, `backend/tests/modules/builds/test_route_migration.py`; Modify `backend/app/modules/builds/{previews,submissions,execution_state}.py`, `backend/app/modules/builds/{preview_models,execution_models,execution_schemas}.py`, `backend/app/alembic/env.py`。

**Interfaces:** consumes P1 Task 5 `freeze_route(session,*,context,bc_id,connection_id=None)->FrozenTikTokRoute`, `verify_route(session,*,context,route,advertiser_id,capability)->None`。produces `save_preview_route(session,*,context,preview_id:UUID,route:FrozenTikTokRoute)->None`, `load_preview_route(session,*,context,preview_id:UUID)->FrozenTikTokRoute`, `inherit_route(parent:FrozenTikTokRoute,*,tenant_id:UUID,bc_id:str)->FrozenTikTokRoute`。本任务仅增加持久上下文及 claim.route；P1 Task 7、P2 Task 2 在各自改变准备函数签名时消费这些接口并同步全部调用点。

- [ ] **1. 写继承失败测试，不让延迟子任务得到另一条路线。**

```python
from uuid import uuid4
import pytest
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.builds.routes import inherit_route

def test_child_keeps_route_and_rejects_cross_bc():
    route = FrozenTikTokRoute(tenant_id=uuid4(), bc_id="bc-a", connection_id=uuid4(), channel="OFFICIAL_MCP", authorization_revision=3, adapter_contract_revision="build-v1")
    assert inherit_route(route, tenant_id=route.tenant_id, bc_id="bc-a") == route
    changed_default = route.model_copy(update={"connection_id": uuid4()})
    assert inherit_route(route, tenant_id=route.tenant_id, bc_id="bc-a") != changed_default
    with pytest.raises(DomainError):
        inherit_route(route, tenant_id=route.tenant_id, bc_id="bc-b")
```

- [ ] **2. 跑红测试。** `uv run pytest tests/modules/builds/test_frozen_routes.py -q`。
- [ ] **3. 新增不可变 `build_route_context`：tenant_id+preview_id 唯一并复合外键关联预览，保存 BC、connection、channel、两种语义版本；其他对象经 tenant+preview_id 引用同一行。** 路由连接外键引用 P1 tenant+BC+connection 绑定，应用也核实授权版本。新预览创建事务调用 freeze/save，提交与延迟任务只能 load；不能修改旧路由。`ExecutionStep` 增加持久 attempt_id UUID 与 `StepClaim` 的 route/attempt_id 对应，migration 为历史每个 attempt 的 evidence 建稳定关联而不改变原计数；Task 4 新授权核查有独立审计表。独立场景 job 使用 P1 持久 route；由预览派生时按父路线参与去重键，不能复用另一连接的场景。

```python
def inherit_route(parent: FrozenTikTokRoute, *, tenant_id: UUID, bc_id: str) -> FrozenTikTokRoute:
    if (parent.tenant_id, parent.bc_id) != (tenant_id, bc_id):
        raise DomainError("frozen_route_scope_mismatch", "冻结执行连接范围不匹配")
    return parent
```

`save_preview_route` 先校验预览归属，再 INSERT；相同值幂等，已有不同值报 `frozen_route_changed`。本任务不提前传入尚不存在的准备函数参数；后续 P1/P2 修改调用点时统一加载这里的父路由。来源依赖由 P2 单独保存 source_route，不能覆盖目标 route。

- [ ] **4. 编写新增 Alembic 迁移，依赖已完成的 P1 连接模型，执行 `uv run alembic revision --rev-id mcp_build_routes -m "frozen build routes"`，down_revision 由实施时真实单一 head 生成。** API 旧连接归类/默认路由已由 P1 处理。迁移逐个检查历史 BuildUnit.connection_id、step 原请求账户、当时可核实授权/连接事实；仅唯一且一致时旁挂 API route，不按今天默认推断过去。无法证明的提交标记 `legacy_route_unverifiable`、禁止恢复，保留请求原文/摘要/ID。旧未提交预览缺路线设 OBSOLETE，已提交请求不改；不把 UNKNOWN/RUNNING 任务迁入 MCP。回滚不靠 destructive downgrade。
- [ ] **5. 真实 PostgreSQL 验证迁移前后请求字节/摘要/remote_id 恒等，空、单连接、多连接、缺历史证据、两租户相同 BC 都有用例。** 使用现有 `tests.migration_database.historical_database` 独立临时数据库；与新增测试同跑 `uv run pytest tests/modules/builds/test_frozen_routes.py tests/modules/builds/test_route_migration.py tests/modules/builds/test_submissions.py -q`，预期冻结默认切换不影响旧任务，正常刷新允许继续，scope/主体变更阻断。
- [ ] **6. 记录并提交** `feat(builds): persist immutable execution routes and safe historical context`。

### Task 3: Worker 经 gateway 发送，结果先持久化

**Files:** Modify `backend/tests/integrations/tiktok/build_wire.py`, `backend/app/integrations/tiktok/adapters/{sdk_builds,mcp_builds}.py`, `backend/app/modules/builds/{execution,execution_state,execution_admission,dispatch,tasks}.py`, `backend/app/modules/builds/{execution_models,execution_schemas}.py`, `backend/tests/modules/builds/{test_execution,test_execution_state,test_execution_admission,test_review_sdk_cleanup}.py`; Create `backend/tests/modules/builds/test_channel_execution.py`。

**Interfaces:** consumes `open_tiktok_gateway(*,database_engine:Engine,redis_client:Redis,context:TenantContext,route:FrozenTikTokRoute,task_deadline:datetime)->AbstractContextManager[TikTokGateway]`、`gateway.builds:BuildOperations` 与 P0 quota/error 合同。produces 保持 `process_step(*,database_engine,redis_client,context,step_id,revision)` 现有调度入口、`record_created(session,*,claim:StepClaim,result:CreatedObject)->str`；原 StepEvidence 增加 attempt_id、MCP correlation/task ID 安全字段与 route 引用。

- [ ] **1. 写 UNKNOWN 围栏回归，使用已存在真实 PG fixture。** 将原 `attempt` fixture 补 route/attempt_id 后复用；没有 SDK/MCP 替身注入业务层。

```python
from datetime import UTC, datetime, timedelta
from tests.modules.builds.test_execution_state import attempt as attempt, body
from app.modules.builds.execution_state import arm_request, expire_attempt

def test_armed_lease_expiry_preserves_intent_and_blocks_replay(session, context, attempt):
    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    digest = step.request_body_digest
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert expire_attempt(session, step=step) == "RECONCILE"
    assert step.status == "UNKNOWN"
    assert step.request_body_digest == digest
    assert step.request_body == body(claim)
```

- [ ] **2. 跑现状测试作为基线，并加断言 `claim.route == load_preview_route(...)`，新 route 用例预期失败。** `uv run pytest tests/modules/builds/test_channel_execution.py tests/modules/builds/test_execution_state.py -q`。
- [ ] **3. 在两个 builds adapter 实现 create，再替换 worker SDK 客户端及 App 专属准入依赖，保留入场、arm、commit、单次发送、保存回执的顺序。** API 按现有 invoke_create/invoke_portfolio 移植固定 SDK 调用；MCP 只调用 P0 映射声明的固定 operation，完整校验业务 envelope 和实际 ID，缺 P0 创建/回读/重试证据则仍 NOT_SENT 阻断。 P1 gateway 通过 P0 准入层为每次实际调用准入，外层只保留现有租户公平，不重复计算同一个 API 额度；创建/核查仍 45 秒 prefork 硬期限，准入拒绝持久化重排、不 sleep。

```python
with open_tiktok_gateway(database_engine=database_engine, redis_client=redis_client,
                         context=context, route=claim.route,
                         task_deadline=min(task_deadline, claim.lease_expires_at)) as gateway:
    result = gateway.builds.create(attempt_id=claim.attempt_id, intent=intent)
    with Session(database_engine) as receipt, receipt.begin():
        record_created(receipt, claim=claim, result=result)
# 上述事务已提交；客户端关闭异常不能清除 remote_id 或重新创建。
```

`process_step` 入口以当前 UTC 时间加现有 HARD_LIMIT 确定本次 task_deadline；此后只取更早期限，不能在每次会话重新起算。发送前由 P1 verify_route 检查最新连接状态、授权语义、账户能力，正常同 grant 凭据刷新可用。精确请求、stable attempt_id、摘要在网络前提交；数据库事务不跨网络。只有 `RemoteCallError.effect == "NOT_SENT"` 可安全重排；`REJECTED_NO_EFFECT` 需 P0 错误白名单证明无副作用，按可修复规则处理；`UNKNOWN` 及未分类异常（包括解析失败）都 record_unknown，不依据 code 非零切换到可重发。记录远端已知 ID 时保留调用证据；迟到回执按旧 nonce 追加证据、不覆盖新 claim；连接被停用后在途结果仍保存，后继不派发。

- [ ] **4. 将既有执行/cleanup 行为测试参数化到 OFFICIAL_API/OFFICIAL_MCP：无 App 配置 MCP 可创建、API 明确阻断；远端成功掉响应两次派发只发送一次；close 异常仍有 ID；CTA 与每级请求均单独 attempt；不引入 activation。** `uv run pytest tests/modules/builds/test_channel_execution.py tests/modules/builds/test_execution.py tests/modules/builds/test_execution_state.py tests/modules/builds/test_execution_admission.py tests/modules/builds/test_review_sdk_cleanup.py -q`。
- [ ] **5. 记录并提交** `feat(builds): execute frozen attempts through TikTok gateway`。

### Task 4: UNKNOWN 原连接核查与新授权只读恢复

**Files:** Modify `backend/app/modules/builds/{reconciliation,recovery,recovery_api,recovery_models,recovery_tasks}.py`, `backend/app/modules/builds/{execution_schemas,execution_state}.py`, `backend/tests/modules/builds/{test_reconciliation,test_recovery,test_review_recovery_candidates,test_prefork_deadline}.py`; Create `backend/app/modules/builds/recovery_routes.py`, `backend/app/alembic/versions/mcp_build_recovery_build_recovery_routes.py`, `backend/tests/modules/builds/test_channel_recovery.py`; Delete old `sdk_requests.py`/`readback_sdk.py` after fixing all imports and moving tests to new API adapter/纯编译比对边界。

**Interfaces:** consumes `BuildReadQuery`/`BuildPage`/`read_adgroup_status`、原 StepClaim.route；produces `reconciliation_decision(*,page:BuildPage,matches:tuple[BuildRecord,...])->Literal['CONFIRMED','UNKNOWN']` 与 `authorize_historical_read(session,*,context:TenantContext,source_step_id:UUID,new_route:FrozenTikTokRoute)->UUID`。后者保存独立审计行/新旧授权关联，返回核查授权 ID，只能供 read capability 使用。新审计表迁移在 P2 完成后执行 `uv run alembic revision --rev-id mcp_build_recovery -m "build recovery routes"`，由实际单一 head 生成依赖（须同时含 Task 2 与 P2 路由），使用 tenant+source_step 复合外键，保存原/new route、actor、request、核实来源与时间；不改旧请求。

- [ ] **1. 写不完整/多候选/空页始终未知的纯判定测试。**

```python
import pytest
from app.modules.builds.reconciliation import reconciliation_decision
from app.integrations.tiktok.contracts.builds import BuildPage

@pytest.mark.parametrize("complete,total", [(False, 0), (True, 0), (False, 2)])
def test_empty_or_incomplete_page_never_authorizes_recreate(complete, total):
    page = BuildPage.model_construct(rows=(), page=1, total_pages=1, total_number=total, complete=complete, evidence=None)
    assert reconciliation_decision(page=page, matches=()) == "UNKNOWN"
```

此处 `model_construct` 只针对纯判定，不作为 transport contract 测试；adapter 测试必须真实校验 evidence。
- [ ] **2. 跑红测试。** `uv run pytest tests/modules/builds/test_channel_recovery.py -q`。
- [ ] **3. 用 typed query/page 替换 SDK read，逐页留证据且只准许完整、唯一、字段齐全、父级和账户全匹配结果。** `CONFIRMED` 只表示原对象已核实，不授权再次 create。缺 status 通过同 route `read_adgroup_status` 补查；状态补查不能覆盖父子错配。

```python
def reconciliation_decision(*, page, matches):
    if not page.complete or len(matches) != 1:
        return "UNKNOWN"
    item = matches[0]
    return "CONFIRMED" if item.intent is not None and not item.missing_fields else "UNKNOWN"
```

业务层在调用此判定前跨页聚合完整扫描的 candidate（以远端 ID 去重，检查分页总数与父级）；不能以单页 complete 推断全查询完整。对象删除/权限失效/空页/多候选/期限耗尽保留 UNKNOWN；只读再次核查由持久 outbox 或明确用户请求触发，从不修改原 create body。
- [ ] **4. 新授权核查必须显式 API 操作并只读。** 对 `authorize_historical_read` 验证同租户、同连接、同主体、同 BC、同 advertiser 的重新核实读权限；旧授权原记录保留，不变更冻结 route。任一主体/归属未知拒绝 `historical_read_scope_unverified`。核查 worker 记录原 authorization_revision 与新 revision/管理员/请求；即使查到对象也不自动续建受影响子任务，页面提示重新准备剩余意图。
- [ ] **5. 扩展真实 Linux prefork 测试的本地 transport 服务：收到 create 后阻塞/断流，worker hard kill，原服务继续返回迟到结果，恢复仅 GET/tools/read。** 复用 `test_prefork_deadline.py` Redis 唯一队列和真实 PG，MCP 服务实现 P0 固定握手/tools 协议，不替换 process_step/gateway。两通道断言 create 计数始终 1、本地租约释放不代表远端结束、恢复未偷换默认/连接、停用后无新发送。
- [ ] **6. 运行** `uv run pytest tests/modules/builds/test_channel_recovery.py tests/modules/builds/test_reconciliation.py tests/modules/builds/test_recovery.py tests/modules/builds/test_review_recovery_candidates.py tests/modules/builds/test_prefork_deadline.py -q`；Linux 必须实际执行 hard-kill 用例，macOS skip 不是通过证据。`rg -n 'sdk_requests|readback_sdk|sdk_client|business_api_client' app/modules/builds` 预期无业务 SDK 调用。
- [ ] **7. 记录并提交** `feat(builds): reconcile unknown operations on their original authorization`。

### Task 5: 预览与执行页面展示冻结连接和可操作状态

**Files:** Modify `backend/app/modules/builds/{preview_schemas,execution_schemas,submission_catalog,submission_catalog_api}.py`, `frontend/src/features/builds/api.ts`, `frontend/src/features/builds/{BuildInputPage,PreviewWorkspace,SubmissionDetailPage,SubmissionPresentation,SubmissionRecoveryActions}.tsx`, `frontend/tests/utils/buildsBoundary.ts`, `frontend/playwright.config.ts`; Create `frontend/tests/build-channels.spec.ts`。

**Interfaces:** API 增加 `execution_route:{connection_id:string,connection_name:string,channel:'OFFICIAL_API'|'OFFICIAL_MCP',bc_id:string}` 与 `recovery_mode:'ORIGINAL_READ'|'REAUTHORIZE_READ'|'BLOCKED'`、已有安全 error_code；不返回 token、回调 URL、签名素材 URL、raw MCP response。选连接输入复用 P1 BC scoped connection picker，预览返回实际冻结值。

- [ ] **1. 增加现有 HTTP 边界 fixture 的 `channel?:'OFFICIAL_API'|'OFFICIAL_MCP'` 选项，并在 preview 返回 execution_route；写浏览器失败测试。**

```ts
import { expect, test } from "@playwright/test"
import { BC, buildsBoundary, P, T } from "./utils/buildsBoundary"
test("预览展示冻结MCP连接并保留直接启用确认", async ({ page }) => {
  await buildsBoundary(page, { channel: "OFFICIAL_MCP" })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText("官方 MCP", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: /创建并立即启用/ })).toBeEnabled()
})
```

- [ ] **2. 将 build-channels 加入 playwright.config.ts 的 workspace testMatch 及其他项目的排除规则，然后跑红测试。** `bunx playwright test --project workspace tests/build-channels.spec.ts --reporter line`，不得触发真实登录 setup。
- [ ] **3. 使用已有 shadcn/ui Badge/Alert 和中性黑按钮展示连接名/方式/BC。** 在前后端明确区分“已创建并启用”“等待审核/核查”“结果未知”“连接失效需重新连接”；已有 ID 和逐级进度保留。UNKNOWN 只提供“核查原操作”，不得显示“重试创建”；新授权只读核查单独解释不会续建。旧 preview OBSOLETE 提示重新准备；切 BC/tenant 清查询缓存，不能把上一租户连接或 ID 留在页面。

```ts
const channelLabel = { OFFICIAL_API: "官方 API", OFFICIAL_MCP: "官方 MCP" } as const
const routeText = `${channelLabel[route.channel]} · ${route.connection_name} · BC ${route.bc_id}`
```

- [ ] **4. 添加 Playwright 两通道、默认切换仍显示旧连接、UNKNOWN 禁创建、授权缺失阻断、tenant 切换、390/900/1440 宽度用例。** 先在仓库根运行 `bash scripts/generate-client.sh` 并检查生成差异，再从 frontend 执行 `bunx playwright test --project workspace tests/build-channels.spec.ts tests/build-preview.spec.ts tests/build-preparation.spec.ts --reporter line` 和仓库根目录 `bun run --filter frontend build`；预期均过，无新增激活流程/暴露原始协议字段。
- [ ] **5. 记录并提交** `feat(builds-ui): show frozen connection and unknown-result recovery`。

### Task 6: 完整矩阵、迁移与发布验收包

**Files:** Create `backend/tests/integrations/tiktok/test_dual_channel_flow.py`; Modify `backend/tests/integrations/tiktok/build_wire.py`, `backend/tests/integrations/tiktok/conftest.py`; Create `docs/acceptance/live-mcp.md`, `docs/validation/2026-09-11-tiktok-dual-channel-offline.md`; Modify `docs/contracts/tiktok-readback.md`, `docs/runbooks/{deployment,recovery,production-junbo}.md`, `docs/implementation-progress.md`。真实联调另建实际执行日期记录，不能提前填写成功。

**Interfaces:** consumes P0 capability matrix、P1 accounts/scenes、P2 materials、P3 builds 同一 gateway；produces 每能力 offline/live/protocol 状态及阻断原因、数据库 head、版本/环境和可核实测试报告。跨阶段测试在 transport 层参数化，不打补丁跳过租户授权、gateway 或任务锁。

- [ ] **1. 写调用已接收后断线不重试的真实 HTTP 边界测试，并抽取双通道执行 fixture。** `mcp_wire`、`bound_client` 复用 P0 `tests/integrations/tiktok/conftest.py`，本用例的合成 mapping 将 `builds.create_campaign` 映射到 `fixture_create`（只用于离线测试，不冒充真实官方名称）。

```python
from decimal import Decimal
from uuid import uuid4
import pytest
from app.integrations.tiktok.adapters.mcp_builds import McpBuildOperations
from app.integrations.tiktok.contracts.builds import CampaignCreate
from app.integrations.tiktok.contracts.common import RemoteCallError

def test_mcp_adapter_does_not_retry_after_accept(mcp_wire, bound_client):
    mcp_wire.disconnect_after_accept("fixture_create")
    adapter = McpBuildOperations(bound_client)
    intent = CampaignCreate(advertiser_id="123", name="stable-name", budget=Decimal("100"))
    with pytest.raises(RemoteCallError) as exc:
        adapter.create(attempt_id=uuid4(), intent=intent)
    assert exc.value.effect == "UNKNOWN"
    writes = [call for call in mcp_wire.calls if call.get("method") == "tools/call"]
    assert len(writes) == 1
```

Task 1 建立、Task 3 扩展、这里集成的 `backend/tests/integrations/tiktok/build_wire.py` 定义 `BuildWire(channel:ChannelKind)` 测试工具，仅提供 `endpoint:str`、`calls:list[dict[str,object]]`、`enqueue_created(kind:str,remote_id:str)`、`drop_created_response(kind:str)`、`enqueue_readback(kind:str,rows:list[dict[str,object]],page:int,total:int)`；API 分支真实 HTTP handler 返回现有 SDK envelope，MCP 分支包装 P0 McpWire 并返回 schema 验证的 CallToolResult。`backend/tests/integrations/tiktok/conftest.py` 的 `build_wire` fixture，参数化两通道，将这个本地 endpoint 配置到真实 adapter transport 工厂；不得 patch gateway/业务调用。改造现有 `tests/modules/builds/test_execution.py` 的 `executable` fixture 只按 channel 创建对应连接/授权/目录证据，保留真实预览、提交、任务展开；既有 wire 测试中的 URL 断言改由 BuildWire 记录读取，冻结业务 body/ID/金额断言保持共享。

扩充完整场景：无 App 的 MCP 授权后账户目录→场景→R2 源入库→跨账户目标 VID/封面→预览→CTA→三级→父子/金额/ENABLE 回读。提交沿用现有 READY/PREPARING 合同：已核实可准备的素材允许提交并先执行 MATERIAL，目标视频/封面未完成并核实前不得发送广告创建；明确 BLOCKED 的权限、路由或不可准备依赖不得提交。每调用计数、调用 route、实际来源/目标 ID 均断言；租户隔离、共享桶、outbox 去重、refresh 语义、schema 漂移及 R2 未知保护覆盖 P1/P2 对应测试，报告引用具体测试路径。
- [ ] **2. 跑相关后端全矩阵。** `uv run pytest tests/contracts tests/integrations/tiktok tests/modules/accounts tests/modules/materials tests/modules/builds tests/jobs -q`。运行 `uv run ruff check app/integrations/tiktok app/modules/builds`、`uv run ty check app/integrations/tiktok app/modules/builds`。Linux PG/Redis prefork 套件不得被跳过。
- [ ] **3. 在隔离迁移数据库运行 Alembic upgrade/head 验证、历史冻结请求恒等检查，前端运行 Task 5 命令。** `uv run alembic heads` 必须单一 head；迁移测试必须从现有旧 head 和 P2 head 各升级一次，不能 downgrade 共享库。所有测试失败先修正，不把离线替身通过标记成真实 MCP 成功。
- [ ] **4. 更新发布手册：冻结新写→排空正在执行任务→暂停/等待备份 timer→可恢复备份→新增迁移→固定同 SHA API/worker/beat→读取/旧 API 回归→按已完成能力开放 MCP→恢复 timer。** 骏伯生产用已发布版本 `deploy/production-compose.sh`、独立 `tt-ada-production`、8000 入口，按 `production-junbo.md` 全流程；默认连接歧义、历史 UNKNOWN 无上下文单列阻断清单。回退应用前先核实 schema 兼容，未解决写操作保留证据和围栏，不清空队列/持久卷。
- [ ] **5. 写具体真实联调验收表，并保持未执行状态。** 每行记录目标环境、租户、BC、后台 connection_id/通道、账户、素材真实来源/目标 ID、广告具体冻结名称/预算、用户授权凭据的审计 ID、原 request/attempt 及远端对象数量。P0/P2 权限或字段尚未核实的操作不可开放。真正请求批准前由操作者生成这一批的具体预览；本阶段不代为授权、不复制 Codex token、不发广告。
- [ ] **6. 记录并提交** `docs(tiktok): verify dual-channel integration and release gates`。最终汇报分别列代码/离线、协议、授权联调和目标环境状态；只有对应证据存在才声称对应能力已完成。
