# Tenant Material Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 上传一次，B 从统一租户素材库搭建；先向 B 主账户转存一次，再原生共享给 B 内目标。

**Architecture:** `MaterialFile` 的 BC 保留原始上传来源语义，消费引用改为租户身份；实际副本、操作及冻结路由仍按目标 BC 校验。首次进入目标 BC 使用持久唯一依赖及现有 URL relay，目标准备在该依赖完成后沿用 native_share 和 IMAGE 流程。

**Tech Stack:** Python/SQLModel/FastAPI/Alembic/PostgreSQL/Redis/Celery，React/TypeScript/TanStack/shadcn/Bun/Playwright。

**Spec:** `docs/superpowers/specs/2026-09-16-tenant-material-library-design.md`

## Global Constraints

- 本轮实施限本地代码、迁移演练和测试，不包含推送、环境发布或真实平台写入。
- 浏览和预览均不得触发分发。
- 锁不跨 HTTP；UNKNOWN 只读核实，不更换账户或重复发送。
- 原上传 BC、上传批次、源占用、原件代数及已有冻结任务保持原身份。
- 实际平台副本和所有冻结授权继续限定 tenant/BC/account/connection/revision。
- 新行为统一替换旧 BC 库行为，不增加旧模式回退开关。
- 不新增分支；当前干净 `main` 原位实施，独立文件责任可并行，根代理负责模型、迁移、生成客户端和整合提交。
- 每阶段运行相应测试并更新本计划；提交前 `git status -sb`、`git rev-parse --show-toplevel`，仅暂存明确文件。

---

## Task 1: 数据引用、内容去重与迁移

**Files:** `backend/app/modules/materials/models.py`, `cover_models.py`, `backend/app/modules/builds/models.py`, `preview_models.py`; 新建 `backend/app/alembic/versions/tenant_material_library.py`, `backend/app/modules/materials/content_identity.py`, `backend/tests/modules/materials/test_tenant_library_migration.py`。

**Interfaces:** 保留 `material_reference()` 表示原始上传关联；新增 `tenant_material_reference()` 表示 `(tenant_id, material_id)`。内容辅助 `content_key(material: MaterialFile) -> str`，可信 SHA-256/MD5/大小相同则同 key，否则以 material UUID 区分。消费代码使用实际 route.bc_id，不再将原始 BC 作为目标。

- [x] 编写迁移测试：A 的文件在 B 建 AccountMaterial/Operation/Distribution/Cover 和草稿/预览可成功；跨租户、错误目标授权仍失败；ObjectUpload 的来源 BC 错配仍失败。

```python
def tenant_material_reference():
    return ForeignKeyConstraint(
        ["tenant_id", "material_id"], ["material_file.tenant_id", "material_file.id"]
    )
```

- [x] 运行新增测试确认旧复合 BC 外键阻止合法跨 BC 消费。
- [x] 按上述接口修改仅消费表的外键；操作、映射及 pending 唯一键显式包含实际 BC，逐项审计对应查询。上传尝试与原件关联保留来源约束。
- [x] 新迁移从当前 `material_share_receipts` head 升级，逐项保留其他约束；新转存表和依赖约束与 Task 3 合并。跨 BC 消费存在时 downgrade 抛异常；不删除历史记录。
- [x] 内容去重使用可信摘要，SQL 排名在分页前进行，匹配名称先筛选；同一草稿组不能重复使用相同内容。历史 ID/名称不回填合并。
- [x] 用专用测试 PostgreSQL 执行迁移、模型测试及 `alembic check`，核对 schema 一致；数据层与依赖它的执行契约合并为同一聚焦实现提交。

## Task 2: 租户目录与搭建候选

**Files:** `materials/router.py`, `catalog.py`, `repository.py`, `schemas.py`, `ingest_api.py`, `ingest_service.py`; `builds/drafts.py`, `catalog.py`, `schemas.py`; 测试 `test_tenant_materials.py`, `test_read_apis.py`, `test_matching.py`, `builds/test_drafts.py`, `test_draft_catalog.py`。

**Interfaces:** `GET /materials`、元数据/资产/尝试、上传历史以租户范围查询；上传写接口继续冻结原 BC。`matching_page` 只把 bc_id 当目标上下文校验，素材候选按 tenant 查询；返回素材自身来源 BC。`DraftMaterialPublic.source_bc_id` 保存显示来源，草稿 `bc_id` 仍表示目标。

- [x] 先补 A 素材/B 目标可自动匹配、手动入组、详情可读以及第三租户拒绝的回归，运行确认失败。
- [x] 将素材列表、详情、匹配、资产计数、历史列表的来源 BC 过滤移除；保留 tenant 条件与实际资产授权 EXISTS。按素材持久来源处理原件预览/重试，按真实资产来源处理远程预览。

```python
statement = select(MaterialFile).where(MaterialFile.tenant_id == context.tenant_id)
# 草稿/预览的 bc_id 仍用于核实目标账户授权，不能参与素材内容 JOIN。
```

- [x] 列表/匹配游标绑定租户、查询条件和范围版本，精确 total 使用相同筛选与去重关系。
- [x] 修改旧“同租户其他 BC 不可见”断言；新增仅其他 BC 有平台副本时仍可用。运行列出的目录、匹配和草稿测试并纳入整体验证后的聚焦提交。

## Task 3: BC 首次转存与目标原生共享

**Files:** 新建 `materials/bc_seeding.py`；修改 `source_selection.py`, `source_uploads.py`, `distribution.py`, `distribution_sources.py`, `readiness.py`, `batch_distribution.py`, `batch_verification.py`, `builds/material_execution.py`（仅依赖恢复衔接）；测试新建 `test_bc_seeding.py`, `test_bc_seeding_concurrency.py`，保留现有 native/remote-only/concurrency/UNKNOWN 用例。

**Interfaces:** `ensure_target_asset` 保持调用契约，先准备真实目标 BC 来源，再准备目标账户；主账户选择服务从 `claim_source_account` 提取，不修改原 ingest row。持久首次转存登记由 `content_key + tenant_id + target_bc_id` 唯一约束，记录原素材、主账户和实际 distribution。等待者不能把 seed 的 mapping 当作目标 mapping，不能把 seed ID 当作目标账户成功证据。

- [x] 编写先失败的真实 PG/Redis 用例：同时为 B1/B2/B3 准备 A 素材，断言一个 URL 上传目标为 B 主账户，随后 B 内共享；B4 再次准备零新增 URL 上传。

```python
# 传输边界计数应由 test_bc_seeding fixtures 捕获，不 mock 业务服务。
assert [call.advertiser_id for call in url_uploads] == [primary_b]
assert set(shared_targets) == {b1, b2, b3}
assert source_file.bc_id == bc_a
```

- [x] 提取主账户选择的短事务/冷却/权限逻辑，原始上传继续使用原 ingest 绑定；预览采用只读候选检查，不持久化选择。
- [x] 引入唯一 seed 登记，复用现有分发操作、outbox 和发送未知保护。目标账户任务持久关联 seed 等待；明确失败/撤权/未知可见并可按既有只读恢复，重复投递不重新创建 seed。
- [x] 分发所有内容加载按租户 ID，目标 route 用 operation/distribution BC；来源 JOIN 按 tenant+material，不限制等于原始 BC。现有 source/target 冻结路由仍逐次验证。
- [x] 同账户不同 BC、重复内容别名、并发用户、主账户本身是目标、已有 B 来源、超时未知和不同授权版本回归；源撤权后不能假借目标授权读取。
- [x] 运行新 seed/native/remote-only/distribution 并发与批次用例，纳入整体验证后的聚焦提交。

## Task 4: 封面和实际来源读取

**Files:** `materials/covers.py`, `cover_sharing.py`, `source_cover_service.py`, `remote_sources.py`；新建 `tests/modules/materials/test_tenant_library_covers.py`，扩展 source cover/URL relay 测试。

**Interfaces:** `MaterialFile.bc_id` 是 provenance；cover job 的 bc_id/route/asset 是实际目标。目标 primary 的真实 URL 上传证据继续由 `_start_recorded_source` 验证。

- [x] 先测试 A 的 material ID 在 B primary URL 上传后生成 SOURCE 封面、再 IMAGE 共享到 B1；同内容来源被改写/撤权时拒绝。
- [x] 仅移除内容查找里的 provenance BC 等值约束；远程读取前后独立保存/核对原文件身份与实际 source BC，保持 MD5/大小和冻结授权一致。

```python
content_identity = (material.tenant_id, material.bc_id, material.video_md5, material.byte_size)
# HTTP 后与同一 content_identity 对比，而非把 material.bc_id 与 source route.bc_id 比较。
```

- [x] SOURCE promotion 仍要求实际上传证据；禁止 IMAGE 跨 BC 原生共享、native 派生冒充源及 UNKNOWN 重传。
- [x] 运行新增跨 BC 封面、原有 cover throughput/source events/channel cover 测试，纳入整体验证后的聚焦提交。

## Task 5: 前端统一目录与冻结上传目标

**Files:** `frontend/src/features/materials/{MaterialsPage,MaterialTable,AssetDetails,BatchHistory,UploadQueue,BatchUploadSheet}.tsx`, `presentation.tsx`, `useUploadManager.ts`; `features/builds/{MaterialBatchPicker,DramaMaterialSheet}.tsx`；生成 `frontend/openapi.json`, `frontend/src/client/`；扩展 `frontend/tests/materials.spec.ts`, `r2-ingest.spec.ts`, `build-preparation.spec.ts`。

**Interfaces:** 租户素材 query key `['tenant', tenantId, 'materials', ...filters]`，不含顶栏 BC；上传 manager 仍以 tenant/冻结 BC 为生命周期。详情从素材身份获取来源，打开旧批次以 summary.bc_id 构建 manager。

- [x] 页面测试先证明切 A/B 不改变素材列表；B 自动/手动选中 A 视频；上传窗口显示 B；A 批次在顶栏 B 下查看仍为 A。
- [x] workspace 改租户生命周期；拆开目录和上传组件状态。无目标 BC 可读目录但禁用新上传；切换 BC 不改变已提交上传目的地。

```tsx
// 列表属于租户，上传组件属于创建时选定的 BC。
<MaterialTable tenantId={tenantId} />
```

- [x] 来源字段使用后端实际值，保持既有 UI 规范、分页和异常状态。目录范围文案明确为租户素材库，不暴露内部调度细节。
- [x] 根代理运行 `bash scripts/generate-client.sh` 后前端构建；Playwright workspace 跑素材、上传及搭建相关页面。提交前核对生成文件只包含新接口差异。

## Task 6: 整体验收、复审与交付

**Files:** 新建 `docs/validation/2026-09-16-tenant-material-library.md`，更新 `docs/implementation-progress.md` 和本计划。

- [x] 测试环境从本地配置定位，禁止输出私密值；创建专用 `_test` 数据库、独立非零 Redis DB，执行当前 head 迁移。外部请求只允许传输替身。
- [x] 运行 A 来源/B 多账户双通道集成，视频→封面→预览→提交→创建参数回读；用断言核对仅一次 seed 上传、目标 VID/image_id 各自真实且来源记录未变。
- [x] 执行新旧 head 迁移演练、跨租户/错 BC/错 route 拒绝、旧任务及 UNKNOWN 保留测试。
- [x] Python Ruff/改动模块类型检查、前端 TypeScript/Vite 与页面回归；真实 PG/Redis 并发与队列验证，不用 SQLite 代替。
- [x] 使用独立审查代理按设计检查权限、锁序、内容去重、seed 依赖、封面所有权和原上传生命周期；发现问题修复并补回归后再提交。
- [x] 更新本计划勾选、实施进度、验证记录，区分本地模拟和真实平台边界。完成聚焦提交，交付代码/文档链接与验证结果；不自行发布。

## 执行记录

- 2026-09-16：设计及计划已按用户确认方案落盘；只读审计确认封面可复用现有 SOURCE/IMAGE 流程，需解除内容 provenance BC 与实际任务 BC 混用。实现开始前工作区为干净 `main`。

- 实现收口：消费模型、seed 循环外键及执行路径互相依赖，各阶段已分别验证，最终合为一个可运行的实现提交。新增 `material_target_scope` 显式保持目标 BC 归属；现有队列和搭建恢复入口无需增加新的分发接口。
- 独立复审发现的同内容别名封面复用、选材部分勾选后全选问题已补充回归修复；另补自动匹配续跑代表变化去重、刷新恢复待确认上传时切 BC 的回归。最终检查及边界记录见 `docs/validation/2026-09-16-tenant-material-library.md`。

- 最终验收：根集成46通过；目录81、网关22、分发广泛191及最终28、封面专项20及既有176分别通过；API/MCP同/跨 BC四种完整流程通过；页面四组145通过，最终恢复请求专项5通过；Ruff44文件、mypy26模块、前端构建及 Alembic check 通过。各组存在重叠，不累计为全仓测试总数。独立复审问题已关闭。
