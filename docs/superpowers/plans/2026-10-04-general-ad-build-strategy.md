# 通用投放策略实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已确认的通用投放策略落地到 TK-ADA，使广告组和广告都支持“固定数量 / 按素材数量”，支持固定数量下的素材共用或顺序平均分配，并支持系列预算、组预算、最高价值和目标 ROAS。

**Architecture:** 把素材搭建拆成两个独立的分配层：先根据广告组规则生成组，再根据广告规则在每组内生成广告素材集合。创意数量只在素材集合确定后复制广告。预览阶段冻结每个广告实际使用的素材集合，执行阶段按广告读取该集合，避免继续沿用“一个广告组的全部素材”这一旧假设。

**Tech Stack:** Python 3.14、FastAPI、Pydantic、SQLModel、Alembic、PostgreSQL、TikTok 官方 Python SDK/MCP 适配层、React 19、TypeScript、TanStack Query、shadcn/ui、Playwright。

**Spec:** [通用投放策略设计](../specs/2026-10-04-general-ad-build-strategy-design.md)

## Global Constraints

- 每次策略执行使用一个已确定的素材集合；不增加按标签分配或手动逐条分配。
- 广告组层和广告层分别采用“固定数量 / 按素材数量”二选一。
- 固定数量才显示素材安排：共用全部素材或按顺序平均分配。
- 按素材数量只设置每组或每个广告的素材上限，并按顺序拆分。
- 创意数量在素材分配完成后复制广告，复制广告沿用同一素材集合，只更换文案。
- 固定数量加平均分配在数量大于素材数时直接阻断，不自动补素材、不新增素材、不改变用户配置。
- 账户内素材不可用属于资源核验问题，继续保留现有跳过/阻断证据；它不属于策略层的“素材不足处理”，不得用另一批素材自动补齐。
- A/B 和多系列通过同一策略分批执行，不增加 A/B 专用字段。
- 一次策略只能使用系列预算或组预算其中一种。
- 组预算是否能用于目标 Smart+ 场景必须以平台合同和账户级只读能力证据为准；证据不足时预览阻断，不自动降级为系列预算。
- 竞价策略为最高价值或目标 ROAS；最高价值不发送 `roas_bid`，目标 ROAS 必须发送目标值。
- Campaign、Ad Group、Ad 按现有产品合同直接创建并启用；开发和自动化测试不得调用真实广告写接口。
- 所有策略、预览、执行和素材读取继续受 tenant、BC、advertiser、connection 作用域约束。

## Review Focus

- 20 个素材、固定 2 组、按顺序平均分配必须得到 10/10；固定 2 组、共用素材必须得到 20/20。测试：`test_fixed_group_allocation_shared_and_average`。
- 20 个素材、按素材生成组且每组最多 10 个，组内按素材生成广告且每个广告最多 1 个，必须得到 2 组、每组 10 个广告。测试：`test_nested_material_limits_generate_expected_counts`。
- 固定 2 个广告、共用 10 个素材、创意数量 3，必须得到 6 个广告，且每一组复制出的广告素材集合完全一致。测试：`test_creative_count_copies_each_ad_material_set`。
- 固定数量大于素材数且选择平均分配必须直接阻断，不得生成空广告组或补素材。测试：`test_average_allocation_rejects_more_units_than_materials`。
- 最高价值不能带 `roas_bid`，目标 ROAS 必须带 `roas_bid`；系列预算和组预算必须写入对应层级。测试：`test_bid_and_budget_strategy_payloads`。

---

### Task 1: 重构策略配置合同和历史数据迁移

**Files:**

- Modify: `backend/app/modules/strategies/schemas.py`
- Modify: `backend/app/modules/strategies/models.py`
- Modify: `backend/app/modules/strategies/saved_config.py`
- Modify: `backend/app/modules/strategies/service.py`
- Create: `backend/app/alembic/versions/20261004_general_ad_build_strategy.py`
- Modify: `backend/tests/modules/strategies/test_api.py`
- Modify: `backend/tests/modules/strategies/test_saved_config.py`
- Modify: `backend/tests/modules/strategies/test_versions.py`
- Create: `backend/tests/modules/strategies/test_strategy_config.py`

**Interfaces:**

- `StrategyConfig` 新增并固定以下字段：
  - `budget_strategy: Literal["SERIES", "ADGROUP"]`；
  - `bid_strategy: Literal["HIGHEST_VALUE", "TARGET_ROAS"]`；
  - `group_generation_mode: Literal["FIXED", "BY_MATERIAL"]`；
  - `group_count: int | None`；
  - `group_material_allocation: Literal["SHARED", "SEQUENTIAL_AVERAGE"] | None`；
  - `max_materials_per_group: int | None`；
  - `ad_generation_mode: Literal["FIXED", "BY_MATERIAL"]`；
  - `ads_per_group: int | None`；
  - `ad_material_allocation: Literal["SHARED", "SEQUENTIAL_AVERAGE"] | None`；
  - `max_materials_per_ad: int | None`；
  - `creative_count: int`；
  - `target_roas: Money | None`。
- 保留 `budget`、`currency`、`targeting`、`copy_pool_version`、`cta_option_ids` 和命名模板。
- `StrategyConfig` 的模型校验负责保证条件字段互斥：固定模式必须填写数量，按素材模式必须填写上限；无关字段必须为空；`TARGET_ROAS` 必须有目标值；`HIGHEST_VALUE` 不允许带目标值。
- `validate_strategy()` 保留文案池、CTA 和命名校验，并增加结构规则校验；服务层只返回字段级 `ValidationIssue`，不在服务层猜测素材数量。

- [ ] **Step 1: 先补配置模型的失败测试**

  在 `backend/tests/modules/strategies/test_strategy_config.py` 增加：

  - 默认一次搭建为 1 个系列、1 个广告组、广告按素材生成、每广告最多 1 个素材、创意数量 1；系列批次数量属于搭建任务，不写入策略配置；
  - 固定广告组要求 `group_count`；按素材广告组要求 `max_materials_per_group`；
  - 固定广告要求 `ads_per_group`；按素材广告要求 `max_materials_per_ad`；
  - 平均分配字段只能出现在固定数量模式；
  - 最高价值没有 `target_roas`，目标 ROAS 有 `target_roas`；
  - 额外字段和旧的 `group_size` 被拒绝。

- [ ] **Step 2: 实现 `StrategyConfig` 和校验规则**

  在 `schemas.py` 使用 Pydantic model validator 实现上述互斥关系，保留精确十进制金额校验。将现有字段 `group_size` 从新合同中移除，并将预算和竞价字段改为新名称。

- [ ] **Step 3: 编写数据库约束迁移**

  在 `20261004_general_ad_build_strategy.py`：

  - 将 `strategy_version.target_roas` 和 `build_preview.target_roas` 改为可空；
  - 调整 `strategy_version` 的金额和 JSON 一致性约束，允许最高价值没有目标 ROAS；
  - 不改写不可变的历史策略版本、已冻结预览或远端对象；旧策略在读取边界投影为新结构：旧 `group_size` 映射为 `group_generation_mode=BY_MATERIAL` 和 `max_materials_per_group=group_size`；旧 `creative_count` 映射为固定广告数量、共用组内素材，并将新的 `creative_count` 设为 1；旧预算映射为系列预算，旧目标 ROAS 映射为目标 ROAS竞价；
  - 新建或追加的策略只保存新字段，旧配置投影只存在于 `read_saved_config()`，规划器不保留旧分支。

- [ ] **Step 4: 更新保存、读取和版本服务**

  让 `saved_config.py`、`service.py`、策略 API 和幂等摘要全部使用新配置字段。新增测试覆盖创建、追加版本、复制策略、校验失败和旧数据迁移后的读取。

- [ ] **Step 5: 运行策略模块测试**

  Run: `cd projects/tiktok-ad-automation/backend && uv run pytest tests/modules/strategies -q`

  Expected: PASS；旧字段不再出现在新版本配置中，目标 ROAS 可空但仅在最高价值策略下为空。

### Task 2: 实现通用素材分组和广告素材分配器

**Files:**

- Create: `backend/app/modules/strategies/structure.py`
- Modify: `backend/app/modules/strategies/grouping.py`（删除旧的“组大小=广告组、创意数量=广告数”语义，改为兼容入口或移除调用）
- Create: `backend/tests/modules/strategies/test_structure.py`
- Modify: `backend/tests/modules/strategies/test_grouping.py`

**Interfaces:**

- `AdMaterialPlan(base_ad_no: int, material_ids: tuple[UUID, ...], copies: tuple[CopyChoice, ...])`
- `GroupPlan(group_no: int, material_ids: tuple[UUID, ...], ads: tuple[AdMaterialPlan, ...])`
- `plan_structure(materials, *, config: StrategyConfig, pool: tuple[CopyChoice, ...], seed: int) -> tuple[GroupPlan, ...]`
- `plan_structure()` 先按现有稳定顺序去重素材，再依次执行广告组分配、广告分配和创意复制；它不访问数据库、不读取平台、不自动补素材。

- [ ] **Step 1: 写分配器失败测试**

  覆盖以下精确结果：

  - 固定 2 组 + 共用：20 个素材得到 `(20, 20)`；
  - 固定 2 组 + 平均：20 个素材得到 `(10, 10)`，25 个素材得到 `(13, 12)`；
  - 按素材组 + 上限 10：25 个素材得到 `(10, 10, 5)`；
  - 固定 2 个广告 + 共用：10 个素材的两个广告都得到 10 个素材；
  - 固定 2 个广告 + 平均：10 个素材得到 `(5, 5)`；
  - 按素材广告 + 上限 1/10：20 个素材得到 20/2 个广告；
  - 创意数量 3 复制每一个广告的素材集合并生成 3 个不同文案；
  - 重复素材 ID 只保留一次，文件名相同但 ID 不同的素材仍保留；
  - 平均分配时数量超过素材数抛出 `invalid_material_allocation`。

- [ ] **Step 2: 实现独立的两层分配算法**

  将固定数量和按素材数量分别实现为两个纯函数，固定数量的余数放在前面的单元；共用模式复制同一素材元组，平均模式使用不重叠的连续切片。广告层只处理所属广告组的素材。

- [ ] **Step 3: 将创意数量放到分配之后**

  对每一个 `AdMaterialPlan` 按稳定 seed 抽取 `creative_count` 条不重复文案；同一个基础广告的所有复制广告使用同一 `material_ids`，不同基础广告可以各自抽取文案。文案不足抛出现有 `copy_pool_exhausted`。

- [ ] **Step 4: 运行纯逻辑测试**

  Run: `cd projects/tiktok-ad-automation/backend && uv run pytest tests/modules/strategies/test_structure.py tests/modules/strategies/test_grouping.py -q`

  Expected: PASS；测试中不得出现真实数据库、TikTok 调用或素材上传。

### Task 3: 改造预览持久化和冻结结构

**Files:**

- Modify: `backend/app/modules/builds/preview_models.py`
- Modify: `backend/app/modules/builds/preview_schemas.py`
- Modify: `backend/app/modules/builds/previews.py`
- Modify: `backend/app/modules/builds/drafts.py`
- Modify: `backend/app/modules/builds/preview_catalog.py`
- Modify: `backend/app/modules/builds/cover_execution.py`
- Modify: `backend/app/modules/builds/execution.py`
- Modify: `backend/app/modules/builds/material_execution.py`
- Modify: `backend/app/alembic/versions/20261004_general_ad_build_strategy.py`
- Create: `backend/tests/modules/builds/test_structure_preview.py`
- Modify: `backend/tests/modules/builds/test_previews.py`
- Modify: `backend/tests/modules/builds/test_preview_workspace_api.py`
- Modify: `backend/tests/modules/builds/test_cover_execution.py`

**Interfaces:**

- 新增 `PreviewAdMaterial(preview_id, drama_id, group_no, base_ad_no, position, material_id)`，保存每个基础广告实际使用的素材；创意复制不重复保存素材映射。
- `PreviewGroupMaterial` 的唯一约束改为包含 `group_no`，允许共用素材在多个广告组出现；同一广告组内仍按位置唯一。
- `PreviewCopy` 增加 `base_ad_no`，唯一键改为 `drama_id + group_no + base_ad_no + creative_no`。
- `PlannedAd` 增加 `base_ad_no`，唯一键改为 `group_id + base_ad_no + creative_no`。
- `FrozenAd` 增加 `base_ad_no` 和 `material_ids`；执行阶段以 `FrozenAd.material_ids` 为准，不再以组级素材覆盖所有广告。

- [ ] **Step 1: 先增加数据库模型和预览 schema 测试**

  测试同一素材可以被两个广告组共用、一个广告组内不同基础广告可以拥有不同素材集合、一个基础广告的多个创意复制共享同一素材集合，并确认租户和预览外键仍然完整。

- [ ] **Step 2: 增加迁移表和约束**

  在同一个 Alembic migration 中创建 `preview_ad_material`，调整 `uq_preview_drama_material` 为包含 `group_no` 的唯一约束，补充 `base_ad_no > 0` 检查和新唯一约束。迁移执行前后用真实 PostgreSQL 验证升级、降级和已有预览读取。

- [ ] **Step 3: 改造预览生成流程**

  `previews.py` 不再把 `DraftGroupMaterial.group_no` 直接视为最终广告组。每部剧读取草稿中匹配/筛选后冻结的有序素材集合，把它作为本次素材包并调用 `plan_structure()`，再持久化：广告组、组素材展示行、每个基础广告的素材行、每个基础广告的文案复制行。第一期不新建长期 `MaterialPackage` 表；草稿表继续作为素材来源和顺序快照，不再决定最终组数。

- [ ] **Step 4: 改造组和广告读取接口**

  更新 `get_frozen_groups()`、`PreviewUnit`、预览剧目统计和组/广告分页接口，返回每个广告的素材数、创意数量和最终广告数。`daily_budget_sum` 按预算策略计算：系列预算为系列数乘预算，组预算为广告组数乘预算。

- [ ] **Step 5: 改造素材准备和最终素材校验**

  `material_execution.py` 对同一账户内重复使用的素材只登记一次目标素材准备任务；`execution.py` 和 `cover_execution.py` 按 `PreviewAdMaterial` 查询当前广告素材，去重后构建 `creative_list`，并在最终校验时按该广告的素材集合逐一核对视频和封面。账户内不可用素材继续记录 `PreviewSkippedMaterial`；受影响广告阻断并展示证据，不触发策略层自动补素材。

- [ ] **Step 6: 验证预览结果**

  Run: `cd projects/tiktok-ad-automation/backend && uv run pytest tests/modules/builds/test_structure_preview.py tests/modules/builds/test_previews.py tests/modules/builds/test_preview_workspace_api.py tests/modules/builds/test_cover_execution.py -q`

  Expected: PASS；用 20 个素材验证 1 组/20 广告、2 组/10 广告、2 组共用素材和固定广告复制四种结构，数据库中没有重复的同一广告素材行。

### Task 4: 适配系列预算、组预算和竞价策略的 TikTok 请求合同

**Files:**

- Modify: `backend/app/integrations/tiktok/contracts/builds.py`
- Modify: `backend/app/integrations/tiktok/build_wire.py`
- Modify: `backend/app/modules/builds/scene.py`
- Modify: `backend/app/modules/builds/scene_constraints.py`
- Modify: `backend/app/modules/builds/preview_validation.py`
- Modify: `backend/app/modules/builds/request_compiler.py`
- Modify: `backend/app/modules/builds/execution.py`
- Modify: `backend/app/modules/builds/execution_models.py`
- Modify: `backend/tests/contracts/test_tiktok_build_contract.py`
- Modify: `backend/tests/integrations/tiktok/test_build_adapters.py`
- Modify: `backend/tests/modules/builds/test_sdk_contract.py`
- Create: `backend/tests/modules/builds/test_strategy_payloads.py`

**Interfaces:**

- `AdGroupCreate` 支持可选组预算，并要求组预算策略下发送精确日预算；系列预算策略下不发送组预算。
- `CampaignCreate` 支持系列预算策略下的 Campaign 日预算；组预算策略下发送平台认可的无系列预算形式。实施第一步必须把官方合同和账户级只读事实固化为 contract fixture；若平台不支持该组合，保留配置合同但在预览阶段以 `adgroup_budget_unverified` 阻断，绝不静默降级为系列预算。
- `AdGroupCreate` 支持：
  - 最高价值：`optimization_goal="VALUE"`、`optimization_event="AD_REVENUE_VALUE"`、`deep_bid_type="VO_HIGHEST_VALUE"`，不包含 `roas_bid`；
  - 目标 ROAS：`deep_bid_type="VO_MIN_ROAS"`，包含精确 `roas_bid`。
- `FrozenUnit`、场景快照和执行步骤保存 `budget_strategy`、`bid_strategy`，确保预览冻结后执行不会读取可变策略。

- [ ] **Step 1: 写请求合同失败测试**

  对 Campaign、Ad Group、Ad 三层分别断言系列预算和组预算的字段位置；断言最高价值不出现 `roas_bid`，目标 ROAS 出现目标值；断言未知预算或竞价组合在本地被拒绝，不触发远端调用。

- [ ] **Step 2: 更新 TikTok 业务合同和 wire 编解码**

  采用显式的预算/竞价联合校验，保持 `extra="forbid"`、精确十进制和 `operation_status="ENABLE"`。更新 `encode_intent()`、`create_arguments()`、回读字段和 SDK/MCP 适配测试。

- [ ] **Step 3: 更新场景能力和预算约束**

  将预算能力拆成 `campaign_daily_budget` 与 `adgroup_daily_budget`；若目标账户没有已核实的组预算能力，预览以 `adgroup_budget_unverified` 阻断，不退回系列预算。最高价值和目标 ROAS的能力字段分别写入场景快照。

- [ ] **Step 4: 更新执行请求编译**

  `prepare_request()` 根据冻结的预算策略把预算固定在 Campaign 或 Ad Group；根据竞价策略选择是否加入 `roas_bid` 和对应的 `deep_bid_type`。更新执行 readback/final fence，确保实际发送体与预览冻结值一致。

- [ ] **Step 5: 运行合同和执行测试**

  Run: `cd projects/tiktok-ad-automation/backend && uv run pytest tests/contracts/test_tiktok_build_contract.py tests/integrations/tiktok/test_build_adapters.py tests/modules/builds/test_sdk_contract.py tests/modules/builds/test_strategy_payloads.py -q`

  Expected: PASS；所有验证均在本地 fixture 或官方 SDK 边界完成，不发送真实广告创建请求。

### Task 5: 改造预览 API 和业务汇总展示数据

**Files:**

- Modify: `backend/app/modules/builds/preview_schemas.py`
- Modify: `backend/app/modules/builds/preview_catalog.py`
- Modify: `backend/app/modules/builds/submission_catalog.py`
- Modify: `backend/app/modules/builds/submissions.py`
- Modify: `backend/app/modules/builds/api.py`
- Modify: `frontend/src/features/builds/api.ts`
- Modify: `frontend/src/features/builds/BuildInputPage.tsx`
- Modify: `frontend/src/features/builds/presentation.tsx`
- Modify: `frontend/src/features/builds/PreviewWorkspace.tsx`
- Modify: `frontend/src/features/builds/SubmissionTables.tsx`
- Modify: `frontend/src/features/builds/PreviewGenerationProgress.tsx`
- Modify: `frontend/tests/build-preview.spec.ts`
- Modify: `frontend/tests/utils/buildsBoundary.ts`

**Interfaces:**

- `PreviewSummary` 增加 `budget_strategy`、`bid_strategy`、`group_generation_mode`、`ad_generation_mode`、`creative_count` 和面向用户的结构摘要。
- `PreviewUnit` 增加每组广告数、每个广告素材数的可读摘要，保留已有 `group_count`、`ad_count` 和阻断原因。
- 预览摘要使用业务语言：`1 个系列、2 个广告组、每组 10 个广告、创意数量 1、最终 20 个广告、每个广告 1 个素材`；内部的 `base_ad_no` 不直接展示给用户。
- 日预算合计根据预算层级展示“系列预算合计”或“组预算合计”，不再固定写成 Campaign 日预算。

- [ ] **Step 1: 增加 API schema 和 fixture 字段**

  更新 FastAPI response model、前端 API 类型和 `buildsBoundary.ts` 测试夹具，补充两种预算策略、两种竞价策略和两层分配模式。

- [ ] **Step 2: 更新预览统计查询**

  让剧目、单元、提交任务和总览统计从冻结的 PlannedGroup/PlannedAd/PreviewAdMaterial 计算，确保共享素材不会被错误计数为额外的唯一素材，最终广告数包含创意数量复制；同时保留分配次数和去重素材数两个口径供业务解释。

- [ ] **Step 3: 更新预览页面文案和阻断显示**

  将 `Campaign 日预算` 改为“预算策略”对应的业务文案；展示“按素材生成”或“固定数量”及其结果；把 `invalid_material_allocation`、`adgroup_budget_unverified`、`bid_strategy_invalid` 映射为用户能理解的调整提示。

  同一策略版本允许被两个独立搭建草稿引用；A/B 只由本次系列名称、素材包和批次输入区分，API 不增加 A/B 专用字段。

- [ ] **Step 4: 运行预览页面测试**

  后端 schema 变化后运行 `cd projects/tiktok-ad-automation/frontend && bun run generate-client`，提交生成的 `frontend/src/client/*` 类型和 OpenAPI 快照变更；禁止手改生成文件。

  Run: `cd projects/tiktok-ad-automation/frontend && bun run test -- tests/build-preview.spec.ts`

  Expected: PASS；预览数量、预算层级、竞价策略和阻断文案与后端返回一致。

### Task 6: 重做策略配置页面，按联动字段降低学习成本

**Files:**

- Modify: `frontend/src/features/strategies/StrategyForm.tsx`
- Modify: `frontend/src/features/strategies/StrategyList.tsx`
- Modify: `frontend/src/features/strategies/StrategyVersionList.tsx`
- Modify: `frontend/src/features/strategies/StrategyStructureExample.tsx`
- Modify: `frontend/src/features/strategies/StrategyNamingExample.tsx`（仅在新字段影响命名预览时调整）
- Modify: `frontend/src/features/strategies/validation.ts`
- Modify: `frontend/src/features/strategies/feedback.ts`
- Modify: `frontend/tests/strategies.spec.ts`

**Interfaces:**

- UI 标签使用业务名称：
  - “广告组数量规则”：固定数量 / 按素材数量；
  - “广告组素材安排”：共用全部素材 / 按顺序平均分配；
  - “广告数量规则”：固定数量 / 按素材数量；
  - “广告素材安排”：共用本组素材 / 按顺序平均分配；
  - “每个广告创意数量”；
  - “预算策略”：系列预算 / 组预算；
  - “竞价策略”：最高价值 / 目标 ROAS。
- 固定 1 组或固定 1 个广告时，默认共用素材并隐藏没有实际决策价值的安排选项；切换到多数量时再显示。
- 选择按素材数量后只显示对应上限；选择最高价值后隐藏目标 ROAS；输入切换不会保留无关字段。

- [ ] **Step 1: 增加页面失败测试**

  在 `frontend/tests/strategies.spec.ts` 覆盖：

  - 默认值和保存 payload；
  - 固定数量联动显示素材安排；
  - 按素材数量联动显示素材上限；
  - 最高价值隐藏目标 ROAS，目标 ROAS显示并要求目标值；
  - 切换规则会清理无关字段；
  - 前端提示固定数量大于素材数的校验由预览阶段完成，不显示不存在的“素材不足处理”选项。

- [ ] **Step 2: 更新表单状态和 payload**

  将 `StrategyForm.tsx` 中旧的 `groupSize` 状态拆为新字段，保存时提交 `StrategyConfig` 新结构；同步更新策略列表和版本列表中的预算/竞价/结构摘要；保留策略版本、只读、冲突和幂等保存流程。

- [ ] **Step 3: 重做结构示例卡片**

  `StrategyStructureExample.tsx` 使用固定示例素材数展示组层、广告层、创意数量和最终广告数；不把示例误认为本次真实搭建结果，并明确提示真实结果以搭建预览为准。

- [ ] **Step 4: 运行策略页面测试和构建**

  Run: `cd projects/tiktok-ad-automation/frontend && bun run test -- tests/strategies.spec.ts`

  Run: `cd projects/tiktok-ad-automation/frontend && bun run build`

  Expected: PASS；页面没有面向用户的旧 `group_size`、`Campaign 日预算`或将创意数量当成广告数量的说明。

### Task 7: 更新端到端搭建回归、迁移验收和运行文档

**Files:**

- Modify: `backend/tests/modules/builds/test_execution.py`
- Modify: `backend/tests/modules/builds/test_execution_assets.py`
- Modify: `backend/tests/modules/builds/test_material_execution.py`
- Modify: `backend/tests/modules/builds/test_partial_material_execution.py`
- Modify: `frontend/tests/build-preparation.spec.ts`
- Modify: `frontend/tests/build-task-pages.spec.ts`
- Modify: `projects/tiktok-ad-automation/docs/implementation-progress.md`
- Create: `projects/tiktok-ad-automation/docs/validation/2026-10-04-general-ad-build-strategy.md`
- Modify: `projects/tiktok-ad-automation/docs/superpowers/specs/2026-10-04-general-ad-build-strategy-design.md`（若实施中发现需澄清的业务规则，只更新已确认结论，不扩张范围）

- [ ] **Step 1: 增加四套结构回归夹具**

  使用离线素材和目标账户映射验证：

  1. 1 系列/1 组/按素材/每广告 1 素材；
  2. 1 系列/2 组/平均分配/每广告 1 素材；
  3. 1 系列/1 组/固定 2 广告/共用素材/创意数量 3；
  4. 1 系列/2 组/每组 10 广告/组预算/最高价值。

  每个夹具都断言 PlannedAd 数量、PreviewAdMaterial 集合、预算字段层级、竞价字段和 CTA/封面校验。

  另增加一个重复执行夹具：两个独立草稿引用同一策略版本，分别生成 A/B 系列，两个预览互不共享冻结表数据，也不改变策略版本内容。

- [ ] **Step 2: 验证共享素材的执行幂等性**

  断言同一账户、同一素材被多个组或多个广告使用时只产生一个必要的素材准备/共享任务，但每个广告仍按自己的冻结素材集合构建 `creative_list`。

- [ ] **Step 3: 验证失败和恢复路径**

  覆盖素材目标账户不可用、封面待核实、预算能力未核实、最高价值字段缺失、部分广告失败和未知远端结果；失败恢复不得扩大素材集合或重新拆组。

- [ ] **Step 4: 执行完整离线验证**

  Run: `cd projects/tiktok-ad-automation/backend && uv run pytest -q`

  Run: `cd projects/tiktok-ad-automation/frontend && bun run test`

  Run: `cd projects/tiktok-ad-automation/frontend && bun run build`

  Run: `cd /Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill && node .codex/skills/tiktok-smart-plus-drama-ads/scripts/test-batch-tools.mjs`

  Expected: 全部通过；测试只使用离线夹具，不调用真实广告创建或启用接口。

- [ ] **Step 5: 做数据库迁移和 staging 验收**

  在测试环境执行 Alembic upgrade/downgrade、真实 PostgreSQL/Redis 并发和恢复验证；记录迁移 head、结构结果、预览样例和请求 payload 对照。生产环境未建立前不得把 staging 结果称为生产验收。

- [ ] **Step 6: 更新实施进度和验收记录**

  在 `docs/implementation-progress.md` 记录完成的任务、测试命令、迁移版本、未完成的外部 TikTok 读回验证和提交信息；在 validation 文档中区分“离线合同通过”“staging 预览通过”和“真实平台写入待授权”。

## 执行顺序和交付门槛

按以下顺序实施，每个任务完成后单独测试并提交：

1. Task 1：策略配置合同和迁移；
2. Task 2：纯素材结构规划器；
3. Task 3：预览持久化和广告级素材映射；
4. Task 4：预算和竞价请求合同；
5. Task 5：预览 API 和展示；
6. Task 6：策略配置页面；
7. Task 7：端到端回归、迁移验收和文档。

交付前必须同时满足：

- 新策略可以生成 1 组/20 广告、2 组/10 广告、固定广告共用素材和创意复制四类预览；
- 预览中每个广告的实际素材集合可追溯；
- 组预算和系列预算的请求层级、汇总金额和回读字段一致；
- 最高价值和目标 ROAS的请求字段严格区分；
- 现有策略数据完成迁移，历史已冻结预览和执行记录可读；
- 后端、前端和项目主回归测试通过；
- 没有执行未经授权的真实广告写入。
