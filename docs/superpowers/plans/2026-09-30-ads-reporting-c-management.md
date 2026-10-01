# 阶段 C：批量广告管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段 A/B 的真实广告目录与冻结选择上，交付可预览、可审计的 ROAS、即时预算、启停及管理任务。

**Architecture:** 管理模块展开真实影响范围，保存不可变预览与任务，通过现有 Outbox 和冻结路由执行。业务层复用 A 的对象身份与 B 的选择，不重新搜索；MCP/API 适配器独立验证，任务回执与后续投放同步分开。

**Tech Stack:** FastAPI、SQLModel、Alembic、PostgreSQL、Redis、Celery、官方 Python SDK、官方 MCP、React、TanStack Router、shadcn、Bun、Playwright。

**Spec:** [已确认设计](../specs/2026-09-30-ads-management-reporting-design.md)，重点 §5、§8—§12；[总计划与共享合同](2026-09-30-ads-reporting-roadmap.md)。所有步骤尚未执行。

## Global Constraints

- “预览有效期 5 分钟，超时或关键配置变化重新准备。”百分比只在预览计算一次，重试使用冻结最终值。
- “预览提交一次即授权该具体任务，不增加第二套激活流程。”本计划不授权真实广告写入或发布。
- “正常明确成功回执直接记为接口已成功接受/更新，不逐项串行阻塞回读”；请求可能发出后的未知结果不自动重发。
- 所有查询、预览、执行与回执保持 tenant/BC/账户权限；路由冻结授权版本、绑定代数和合同版本，撤权不换通道。
- 同一远端对象的锁不按 BC/连接拆分；Smart+ ROAS 按系列协调，既有搭建隔离路径共同遵守。
- 名称归组为当前 BC 内版权方＋第二段剧名；继承 A 的解析，不能增加剧 ID、人工关联或经营指标。
- UI 使用 generated client、shadcn 与黑色主按钮；普通/Smart+ 身份不可混用，无素材开关不得停整条广告代替。
- 数据库/并发测试使用真实 PostgreSQL/Redis，仅在外部传输边界替身；先读 `config/README.md`，使用既有测试环境定位流程，不打印凭据。
- 每个提交前执行 `git status -sb`、`git rev-parse --show-toplevel`，仅暂存本任务明确路径并检查暂存差异；不创建分支、不推送。

## Review Focus

1. 同一系列由多个所选广告组展开且提交不同最终 ROAS：拒绝冲突而非最后覆盖；覆盖任务 3。
2. 一个远端对象从两个 BC/通道被并发管理，或与既有组隔离相交：同一锁域且失联旧代不得写回；覆盖任务 4、5。
3. 请求已经发出但回执丢失，核查值恰好等于期望值：只记观测达成且归因不明，不重发；覆盖任务 5。
4. 下级启动时父级暂停、素材无独立开关或恢复时对象已被别人修改：扩大范围须明示，不能隐式写入；覆盖任务 3、6、7。
5. 预览后名称迁组、权限撤销或加入新广告：冲突/拒绝原项，不吸收新对象也不更换连接；覆盖任务 3、5、6。

## 文件职责、接口与集成所有权

- `backend/app/modules/ad_management/` 新建 `schemas.py`、`models.py`、`expansion.py`、`previews.py`、`submissions.py`、`execution.py`、`reconciliation.py`、`actions.py`、`api.py`、`tasks.py`，分别负责合同、持久化、影响范围、预览、提交、执行、核查、用户动作、HTTP、队列入口。
- 新建 `integrations/tiktok/contracts/management.py` 与 `adapters/mcp_management.py`、`adapters/sdk_management.py`；新建 `integrations/tiktok/object_coordination.py` 统一已有/新写入的锁域。
- 新建前端 `features/ad-management/` 的预览、任务列表和详情，任务路由消费 B 的工作台选择入口；不接管 B 的筛选或聚合实现。
- root 协调修改 `backend/app/models.py`、`app/api/main.py`、`modules/tenants/permissions.py`、`integrations/tiktok/gateway.py`、`jobs/celery_app.py`、`jobs/task_routes.py`、现有隔离写入调用点以及迁移；迁移文件固定为 `backend/app/alembic/versions/ad_management.py`，revision 为 `ad_management`，down_revision 为 `reporting_queries`；实施前静态核对阶段 B head，此刻不执行迁移。
- root 同时协调 `frontend/src/routes/_layout.tsx`、`src/routeTree.gen.ts`、`src/client/` 和 `playwright.config.ts`；C 新增 `src/features/ads/BulkActionBar.tsx` 并接入 B 的 `AdsWorkspace.tsx`，generated 文件由脚本生成，禁止手工改写。
- 消费 A 的 `integrations/tiktok/contracts/ads.py`：`EntityRef(tenant_id: UUID, advertiser_id: str, kind: Literal['campaign','adgroup','ad','creative'], remote_id: str)`、`MaterialUseRef(ad_ref: EntityRef, platform_material_id: str, ad_material_id: str | None, material_type: str)`；复用 `FrozenTikTokRoute`。普通素材缺少 ad_material_id 仍可展示目录/报表，独立启停为 UNSUPPORTED，不能以平台 VID 冒充引用 ID。
- 消费 A 的 `modules/ads/directory.py`：`locate(session, *, context, bc_id, ref) -> AdObject`、`list_objects(session, *, context, bc_id, advertiser_ids: tuple[str,...], kind: str, parent: EntityRef | None = None) -> tuple[AdObject,...]`；远端新鲜读取走 `gateway.ads.read_page`。
- 消费 B 的 `modules/reporting/selection.py`：`freeze_selection(session, *, context, bc_id, request: SelectionRequest) -> FrozenSelection`；`schemas.py` 定义 `selection_id,snapshot_id,refs: tuple[EntityRef,...],material_uses: tuple[MaterialUseRef,...],membership_digest,expires_at`。账户/剧已展开当时系列，素材 refs 为广告且 material_uses 保留具体引用；C 不重做筛选。
- 统一入口：`prepare_preview(session: Session, context: TenantContext, bc_id: str, selection_id: UUID, mutation: MutationSpec) -> ManagementPreviewPublic`；`submit_management_task(session: Session, context: TenantContext, preview_id: UUID, preview_digest: str, idempotency_key: UUID) -> ManagementTaskPublic`。
- 下列命令从 `backend/` 执行 `uv run --frozen pytest ...`，前端命令从 `frontend/` 执行；每任务 GREEN 后按上述约束仅提交该任务 Files，root 的共享文件须完成协调后才纳入提交。

### Task 1：管理合同、持久化与独立权限

**Files:** Create `backend/app/modules/ad_management/{__init__,schemas,models}.py`、`backend/app/modules/accounts/management_capability_models.py`、`backend/tests/modules/ad_management/{conftest,test_models}.py`；root Modify `backend/app/modules/tenants/permissions.py`、`backend/app/modules/accounts/{routing,access}.py`、`backend/app/models.py`、`backend/app/alembic/env.py`；root Create `backend/app/alembic/versions/ad_management.py`。

**Interfaces:** `MutationSpec(field: Literal['roas','budget','status'], mode: Literal['set','add','subtract','increase_percent','decrease_percent'], value: Decimal | Literal['ENABLE','DISABLE'], include_parents: tuple[EntityRef,...], excluded_refs: tuple[EntityRef,...], excluded_material_uses: tuple[MaterialUseRef,...])`；元组默认空，状态只允许 set。`ManagementPreviewPublic` 保存 `preview_id,digest,created_at,expires_at,bc_id,route,items,counts`；`ManagementTaskPublic` 保存 `task_id,bc_id,status,counts`。`ManagementItemPublic` 保存 `ref,material_use,original_value,final_value,reason,execution_result,observation_state,delivery_status,request_attribution`；持久项另保存归组版本/父子关系/能力，counts 定义 selected/targets/linked/unsupported。

管理权限证据独立为 `ManagementCapability(tenant_id,bc_id,advertiser_id,connection_id,authorization_revision,binding_revision,adapter_contract_revision,operation,entity_kind,state,verified_at,evidence)`；state 为 `VERIFIED/UNKNOWN/REVOKED`。证据记录已确认的 scope、账户管理角色和适用工具/接口，旧 `can_build` 不迁成管理授权；缺失/未知证据拒绝。租户 `ads_manage`、有效账户访问和该操作平台证据三者必须同时满足。

- [x] RED：新增 `test_status_modes_and_management_permission` 的下面断言及同键不同摘要拒绝、跨租户复合 FK 拒绝；测试 fixture 创建真实租户/成员/目录/选择，`management_env.wire` 只替换 SDK/MCP 传输。后续片段中的对象由该 fixture 初始化，load/count/concurrent helpers 在此 conftest 中调用真实 Session/线程屏障，不 mock 业务函数。
```python
with pytest.raises(ValidationError):
    MutationSpec(field="status", mode="increase_percent", value="ENABLE")
with pytest.raises(DomainError, match="action_forbidden"):
    require_tenant(session, actor_id=viewer.id, tenant_id=tenant.id, action="ads_manage")
assert require_tenant(session, actor_id=operator.id, tenant_id=tenant.id, action="ads_manage").role == "operator"
with pytest.raises(DomainError, match="management_permission_unverified"):
    verify_route(session, context=operator_context, route=read_build_only_route,
                 advertiser_id=advertiser_id, capability="ads_manage")
```
- [x] Run RED：`uv run --frozen pytest tests/modules/ad_management/test_models.py -q`，应因缺少模型/权限失败。
- [x] Implement：新增租户 `ads_manage`，同步扩展 `routing.Capability`、`verify_route`、`access.resolve_account_access` 和 `usable_grants`，管理访问只接受当前代数 VERIFIED 证据，禁止 read/build 权限推导。建管理证据、预览、批次、目标、请求尝试和回执，幂等键按租户/操作者约束。批次状态 `PREPARING,READY,QUEUED,RUNNING,SUCCEEDED,PARTIAL,FAILED,NEEDS_REVIEW,CANCELLED`；项结果另含 `NO_CHANGE,CONFLICT,UNSUPPORTED`。
- [x] GREEN：上述测试通过；迁移在隔离数据库 upgrade 后通过模型测试，回滚只验证新迁移且不触碰真实业务库。
- [x] Commit：`ad-management: add scoped preview and task contracts`；暂存本任务明确文件，检查 diff 后提交。最终修复提交为 `d414a9c`，并以不可变后续迁移 `ad_management_integrity` 保留已应用迁移历史。

### Task 2：MCP/API 写入合同与能力门禁

**Files:** Create `backend/app/integrations/tiktok/contracts/management.py`、`backend/app/integrations/tiktok/adapters/{mcp_management,sdk_management}.py`、`backend/app/modules/accounts/management_capabilities.py`、`backend/tests/integrations/tiktok/test_management_adapters.py`；root Modify `backend/app/integrations/tiktok/gateway.py`、`backend/app/integrations/tiktok/contracts/accounts.py`、`backend/app/integrations/tiktok/{official,mcp}/authorization.py`、`backend/app/integrations/tiktok/mcp/tool-contracts.json`。

**Interfaces:** `ManagementCommand(ref: EntityRef, field: str, original: dict, desired: dict, ad_material_id: str | None)`；`ManagementReceipt(outcome: Literal['ACCEPTED','REJECTED','NOT_SENT','UNKNOWN'], request_id: str | None, retryable: bool)`；两个适配器提供 `apply(command: ManagementCommand) -> ManagementReceipt`，读取复用 A 目录适配合同。

`AuthorizationFacts` 新增默认空的 `management_operations: frozenset[str]`，只表示连接 scope 经两通道 authorization 明确映射的操作；`gateway._facts` 同步读取该字段，不能沿用 build_authorized 或把单个账户权限扩散到整连接。账户最终写权由当前 route＋advertiser 的 VERIFIED ManagementCapability 单独核验，每次发送前重新读取。

`refresh_management_capabilities(database_engine: Engine, *, context: TenantContext, route: FrozenTikTokRoute, advertiser_id: str) -> tuple[ManagementCapability,...]` 在 `modules/accounts/management_capabilities.py` 使用只读 gateway 的 `accounts.authorization_facts()` 与现有完整账户角色证据；缺少角色时复用现有角色采集，不用广告写入试探权限。`record_management_capabilities(session, *, route: FrozenTikTokRoute, advertiser_id: str, evidence: ManagementPermissionEvidence) -> tuple[ManagementCapability,...]` 保存结果，Evidence 在 contracts/management.py 定义 scope/role/operations/source/observed_at；scope/tool 存在但无法证明账户写权仍为 UNKNOWN。首次准备预览先完成此只读核验，再要求 ads_manage，避免先要求管理证明才能读取证明。

- [x] RED：新增 `test_material_status_uses_ad_reference`，在两个通道传输 fixture 上参数化下述素材状态命令断言；追加普通组整体替换必需字段保留、Smart+ 广告/创意 ID 分离、素材批量上限与未知能力拒绝。
```python
receipt = adapter.apply(command)
assert receipt.outcome == "ACCEPTED"
assert wire.last_payload["advertiser_id"] == command.ref.advertiser_id
assert wire.last_payload["ad_material_ids"] == [command.ad_material_id]
assert wire.last_payload["smart_plus_ad_id"] == command.ref.remote_id
assert wire.calls_to_other_channel == []
with pytest.raises(DomainError, match="management_permission_unverified"):
    read_build_only_adapter.apply(command)
assert read_build_only_wire.write_count == 0
```
- [x] Run RED：`uv run --frozen pytest tests/integrations/tiktok/test_management_adapters.py -q`，应因缺失合同/适配器失败。
- [x] Implement：注册精确 `management.update_roas/update_budget/set_status/set_material_status` 到 `ads_manage`，接通 Task 1 路由/账户门禁、AuthorizationFacts、两通道 authorization 和权限响应证据保存。普通与 Smart+ endpoint/schema 分开，只发验证过的最小字段集，金额保持 Decimal 精度。未验证的 Smart+ 联动合同不可发送；API 使用官方 SDK，MCP 校验实际工具 schema，不以工具存在或 build=true 认定管理授权。换授权/绑定代数后旧证据不能使用。
- [x] GREEN：上述测试通过；追加首次缺管理证据但实际 scope/ADMIN 角色齐全可通过只读核验、ANALYST 拒绝、甲账户证明不授予乙账户、换代旧证明失效断言；运行 `uv run --frozen pytest tests/modules/accounts/test_build_gateway.py tests/modules/accounts/test_group_isolation_gateway.py -q` 验证旧权限无扩大。
- [x] Commit：`ad-management: add capability-gated API and MCP mutations`；提交明确文件及协调完成的 gateway 改动。最终修复链为 `07ef214`、`5386465`、`9378f76`、`b20c516`、`bcf2649`、`fb3e9d3`。

### Task 3：真实影响展开、5 分钟预览和冲突检测

**Files:** Create `backend/app/modules/ad_management/{expansion,previews}.py`、`backend/tests/modules/ad_management/test_previews.py`。

**Interfaces:** Task 1 的 `MutationSpec`；B 的 `FrozenSelection`；输出统一 `prepare_preview(...)`。新增 `expand_targets(session: Session, context: TenantContext, selection_id: UUID, mutation: MutationSpec) -> tuple[ManagementCommand,...]`，命令之外的不可操作项由预览完整保留。

- [x] RED：新增 `test_percentage_and_budget_owner_previews` 的下面断言，以及同系列不同最终 ROAS 拒绝、预算拥有者去重、素材无开关不变成停广告、父级启用仅在 include_parents 内出现。
```python
p = prepare_preview(session, context, bc_id, selection_id, increase_ten_percent)
assert (p.expires_at - p.created_at).total_seconds() == 300
assert p.items[0].original_value == Decimal("1.20")
assert p.items[0].final_value == Decimal("1.32")
q = prepare_preview(session, context, bc_id, two_groups_selection_id, fixed_budget)
assert q.counts.selected == 2 and q.counts.targets == 1
assert q.items[0].ref.kind == "campaign"  # 两个组的共同系列预算拥有者
r = prepare_preview(session, context, bc_id, ordinary_material_selection_id, disable_material)
assert r.items[0].material_use.ad_material_id is None
assert r.items[0].execution_result == "UNSUPPORTED" and r.counts.targets == 0
```
- [x] Run RED：`uv run --frozen pytest tests/modules/ad_management/test_previews.py -q`，应因预览函数缺失失败。
- [x] Implement：仅接纳未过期的 B 查询选择（15 分钟），复制 refs/material_uses/成员摘要及配置证据供 5 分钟管理预览与后续任务独立保存，B 快照清理不影响已提交任务；新鲜读取后按能力展开，ROAS 作用于组且 Smart+ 展开系列影响，预算只改实际拥有者；账户/剧停用展开系列。精度/最小值/模式按能力合同校验，零/负数非法不截断。原状态无需改变记录 NO_CHANGE；扩大范围、联动、不可操作项全部展示，excluded_refs 显式排除后产生新摘要。
- [x] GREEN：上述测试通过，加入冻结后新广告不进入预览、非法命名仍能按对象管理、重复素材引用去重和错误 BC selection 拒绝。
- [x] Commit：`ad-management: freeze validated impact previews`；仅提交本任务文件。最终修复链为 `eba8f0a`、`a29959e`、`d700fae`、`f3c1b18`、`28b07fb`、`eb08d33`、`0c8642e`、`abd0200`、`f02b84a`、`e3ee502`、`4a286bb`、`782aee9`。

### Task 4：原子提交、Outbox 与跨路径对象互斥

**Files:** Create `backend/app/modules/ad_management/submissions.py`、`backend/app/integrations/tiktok/object_coordination.py`、`backend/tests/modules/ad_management/test_submission_concurrency.py`；root Modify `backend/app/integrations/tiktok/gateway.py`、`backend/app/integrations/tiktok/adapters/{mcp_builds,sdk_builds}.py` 的既有 `disable_adgroup` 调用点。

**Interfaces:** `submit_management_task(...)`；`claim_mutation(session: Session, refs: tuple[EntityRef,...], owner_id: UUID) -> MutationLease`，lease 含领取代数。锁键按 tenant/账户/祖先系列协调重叠操作，既有 build.disable_adgroup 与新管理共用；网络调用不持有数据库行事务锁。

- [x] RED：新增 `test_duplicate_submission_and_cross_bc_lock`；真实 PostgreSQL 双连接并发提交和真实 Redis 准入，重复 delivery fixture 重放下述结果；另测同对象跨 BC 与组隔离互斥。
```python
first, second = submit_same_preview_concurrently(preview, idempotency_key)
assert first.task_id == second.task_id
assert count_pending_dispatches(first.task_id) == 1
lease = claim_mutation(session_a, (target_ref,), owner_a)
with pytest.raises(DomainError, match="mutation_busy"):
    claim_mutation(session_b, (same_object_via_other_bc,), owner_b)
```
- [x] Run RED：`uv run --frozen pytest tests/modules/ad_management/test_submission_concurrency.py -q`，应因提交/互斥未实现失败。
- [x] Implement：校验未过期摘要、显式排除和权限，单事务保存不可变目标/冻结路由/Outbox。双请求摘要不一致冲突；不重新搜索。互斥范围包含所有联动组、祖先启停及素材所属广告；以租约＋数据库代数阻止旧工作者发布，失联已可能发送请求必须转核实再释放重发资格。
- [x] GREEN：上述测试通过，加入事务在 Outbox 前失败无残留、预览 301 秒拒绝、重复领取不产生第二次发送、非相交系列可并发。
- [x] Commit：`a1965c9` 基础实现，`1e8e260` 修复并提交 Celery handoff、Lua 原子 fence/续租、任务项审计迁移与 nested savepoint；最终独立复审 `task-4-rereview-final.md` 为 APPROVED。

### Task 5：执行、成功回执和未知结果核查

**Files:** Create `backend/app/modules/ad_management/{execution,reconciliation,tasks}.py`、`backend/tests/modules/ad_management/test_execution.py`、`deploy/staging-ad-management.service`；root Modify `backend/app/jobs/{celery_app,tasks,task_routes}.py`、`backend/app/core/config.py`、`.env.example`、`compose.yml`、`compose.production.yml`、`docs/runbooks/deployment.md`、`docs/runbooks/staging-singapore.md`、A 的 `backend/tests/jobs/test_ads_reporting_queues.py`。

**Interfaces:** `execute_item(database_engine: Engine, item_id: UUID) -> None`；`reconcile_item(session: Session, context: TenantContext, item_id: UUID) -> ManagementItemPublic`。公共项 DTO 定义 `execution_result,observation_state,delivery_status,request_attribution`；消费 A 的 `modules/reporting/scheduling.py`：`request_sync(session, *, context: TenantContext, request: SyncRequest) -> UUID`；SyncRequest 携原冻结 route、受影响 advertiser_ids/refs，`scope='targeted'`，start_date/end_date 为 None。仅提交合并刷新，不复用创建步骤表。

- [x] RED：新增 `test_success_without_blocking_read_and_unknown_not_resent`；外部传输分别返回明确成功/超时，真实数据库记录请求已武装后崩溃；下面两组断言不能被同步回读阻塞。
```python
execute_item(engine, accepted_item.id)
assert load_item(accepted_item.id).execution_result == "ACCEPTED"
assert wire.reads_after_accepted_write == 0
execute_item(engine, uncertain_item.id); execute_item(engine, uncertain_item.id)
assert wire.write_count(uncertain_item.id) == 1
result = reconcile_item(session, context, uncertain_item.id)
assert result.observation_state == "TARGET_OBSERVED" and result.request_attribution == "UNKNOWN"
```
- [x] Run RED：`uv run --frozen pytest tests/modules/ad_management/test_execution.py -q`，应因执行与核查缺失失败。
- [x] Implement：注册独立 `ad-management` 队列，补齐 Compose/systemd 消费者、`AD_MANAGEMENT_WORKER_CONCURRENCY=1` 注入与新增精确操作的非空调用策略校验/示例，不在本任务部署。每次物理请求前再验用户权限/冻结路由/领取代数/原配置/父子关系/剧归组，冲突仅阻断相关项。明确成功即记接受并后台刷新；后续不同值不重发旧任务。可能已发送的超时记 NEEDS_REVIEW，核查不等于期望值仍不能重发；仅明确未发送/可重试拒绝且原值未变才可续接。
- [x] GREEN：上述测试通过；追加撤权发生在准入后、改名迁组、旧领取代写回、请求成功但 DB 回执提交失败、跨通道不回退，以及一个失败项不阻断无关项；运行 `uv run --frozen pytest tests/jobs/test_ads_reporting_queues.py -q` 验证管理消费者映射、变量注入、缺调用策略拒发及不挤占搭建最低份额。
- [x] Commit：`7d28573`；最终独立窄复审 `task-5-rereview-final.md` 为 APPROVED。

### Task 6：任务查询、取消/重试/恢复和 HTTP 权限

**Files:** Create `backend/app/modules/ad_management/{actions,api}.py`、`backend/tests/modules/ad_management/test_api_actions.py`；root Modify `backend/app/api/main.py`。

**Interfaces:** `cancel_task(session, context, task_id: UUID) -> ManagementTaskPublic`；`retry_task(session, context, task_id: UUID, idempotency_key: UUID) -> ManagementTaskPublic`；`prepare_restore(session, context, task_id: UUID) -> ManagementPreviewPublic`。查询 `get_task(session,context,bc_id,task_id)` 和分页 `list_tasks(session,context,bc_id,cursor,limit)` 始终从当前访问关系过滤。

- [ ] RED：新增 `test_readonly_cancel_and_restore_scope`；API 断言无写权限返回 403，取消只影响未发送项、恢复保留原本停用项且修改后冲突。
```python
assert client.post(preview_url, headers=viewer_headers, json=preview_body).status_code == 403
cancel_task(session, context, task.id)
assert load_item(queued.id).execution_result == "CANCELLED"
assert load_item(sent.id).execution_result == "ACCEPTED"
restore = prepare_restore(session, context, pause_task.id)
assert original_disabled_ref not in [item.ref for item in restore.items]
assert next(item for item in restore.items if item.ref == externally_changed_ref).reason == "configuration_conflict"
```
- [ ] Run RED：`uv run --frozen pytest tests/modules/ad_management/test_api_actions.py -q`，应因端点/动作缺失失败。
- [ ] Implement：在现有 `/api` 前缀下注册 `POST /tenants/{tenant_id}/ad-management-previews`、`POST/GET .../ad-management-tasks`、`GET .../ad-management-tasks/{task_id}`，以及该任务路径下 `POST cancel/retry/restore/reconcile`。路由 tags 固定 `["ad_management"]`，明确 operation_id 前缀 `ad_management-`；restore 返回新预览，reconcile 只读核查，retry 无法接纳未知/成功项。GET 带 bc_id，任务身份/路由以服务器事实为准。
- [ ] GREEN：上述测试通过；追加跨租户同 ID、绑定撤销、重复取消、取消与发送竞争、部分成功不回滚、下级成功但父级暂停仍不可称投放；HTTP 测试不调用真实平台。
- [ ] Commit：`ad-management: expose scoped task actions and audit queries`；检查 OpenAPI operation_id 唯一并提交本任务文件。

### Task 7：管理预览与任务 UI 集成

**Files:** Create `frontend/src/features/ad-management/{ManagementPreviewSheet,ManagementTasksPage,ManagementTaskDetailPage}.tsx`、`frontend/src/features/ads/BulkActionBar.tsx`、`frontend/src/routes/_layout/tenants.$tenantId.ad-management-tasks{,.index,.$taskId}.tsx`、`frontend/tests/ad-management.spec.ts`；Modify `frontend/src/features/ads/AdsWorkspace.tsx`；root Modify `frontend/src/routes/_layout.tsx` 的 workItems、`frontend/playwright.config.ts`；Generate `frontend/src/client/`、`frontend/src/routeTree.gen.ts`。

**Interfaces:** `ManagementPreviewSheet({selectionId: string, bcId: string, open: boolean, onOpenChange: (open: boolean) => void})`；调用 generated `AdManagementService` 对应 prepare/submit/get/list/cancel/retry/restore/reconcile。当前页/跨页/全选继续使用 B 的 selection_id，不在浏览器展开远端对象。

- [ ] RED：新增 Playwright `test('preview impact and uncertain task')`，传输边界返回两个联动组、一个不可操作素材、有效预览和未知任务，验证用户可见行为；预览失效另用独立场景测试。
```typescript
await page.getByRole("button", { name: "批量调整" }).click()
await expect(page.getByText("联动 2 个广告组")).toBeVisible()
await expect(page.getByRole("button", { name: "提交" })).toBeDisabled()
await page.getByLabel("排除不支持项").check()
await expect(page.getByRole("button", { name: "提交" })).toBeEnabled()
await page.getByRole("button", { name: "提交" }).click()
await expect(page.getByText("请求结果需核实")).toBeVisible()
await expect(page.getByRole("button", { name: "重试未知项" })).toHaveCount(0)
```
- [ ] Run RED：先将 `ad-management` 加到 Playwright workspace `testMatch` 与 chromium `testIgnore`，再运行 `bunx playwright test tests/ad-management.spec.ts --project=workspace --workers=2 --reporter=line`，应因页面/控件尚缺失失败而非“没有测试”。
- [ ] Implement：预览展示 BC、连接、账户、选中/去重/联动/不可用数量、旧新值、到期提示与显式父级选择；提交一次导航任务详情。列表及详情分别显示接口结果、同步确认和实际投放；恢复打开新预览，未知项只提供核查。只读隐藏写入口但可查看任务审计；刷新只轮询本地任务，不触发 TikTok 查询。
- [ ] GREEN：从仓库根运行 `bash scripts/generate-client.sh` 后，前端执行 `bunx playwright test tests/ad-management.spec.ts tests/build-task-pages.spec.ts --project=workspace --workers=2 --reporter=line` 与 `bun run build`。追加父级未选不被提交、BC 切换清空旧预览、超时需重新准备、只读无写入口、窄屏/长名称布局检查。
- [ ] Commit：`ad-management: deliver previews and management task pages`；root 统一生成客户端/路由树并确认阶段 B 无回归，提交前记录本地验证及未获授权的真实联调项。

## 阶段验收与交接

- 全部 7 个任务通过后运行 `uv run --frozen pytest tests/modules/ad_management tests/integrations/tiktok/test_management_adapters.py tests/modules/accounts/test_group_isolation_gateway.py -q`，前端按任务 7 验证，不执行在线写入。
- root 更新 `docs/implementation-progress.md`，记录提交、真实 PostgreSQL/Redis 验证与双通道传输合同证据；API/MCP 实际广告写入验收必须另获具体 BC/账户/操作授权。
- 当前文档只给出实施计划，测试片段是后续验收契约，不表示已经运行或功能完成。
