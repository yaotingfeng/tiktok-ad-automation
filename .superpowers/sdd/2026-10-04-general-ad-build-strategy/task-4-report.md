# Task 4 report — TikTok budget and bid contracts

基线：`299cc6e`
提交：`f5db89d`

## 完成内容

- 扩展 TikTok Campaign/Ad Group 合同，严格支持 `SERIES` / `ADGROUP` 预算策略和 `HIGHEST_VALUE` / `TARGET_ROAS` 竞价策略。
  - 系列预算写入 Campaign `budget` + `BUDGET_MODE_DYNAMIC_DAILY_BUDGET`。
  - 组预算使用 Campaign `BUDGET_MODE_INFINITE`，日预算写入 Ad Group；缺少组预算值或混用层级会在本地拒绝。
  - 最高价值写入 `VALUE`、`AD_REVENUE_VALUE`、`VO_HIGHEST_VALUE`，不生成 `roas_bid`。
  - 目标 ROAS 写入 `VO_MIN_ROAS` 和精确目标值。
- 保留创建 wire、标准回读（`ACTIVE_PAY`）和 Smart+ 回读（`IMPRESSION_LEVEL_AD_REVENUE`）的独立 fixture/mapping；回读通过 `deep_bid_type` / `roas_bid` 归一化策略，未覆盖真实事件值。
- 回读比对允许上述已知事件枚举差异，同时仍保留远端实际事件字段，不把未经证实的值改写成创建值。
- 场景约束新增 Campaign/Ad Group 预算能力槽位和竞价能力快照。没有账户级组预算只读证据时预览返回 `adgroup_budget_unverified`，不会降级为系列预算。
- 预览快照和 `FrozenUnit` 固定预算/竞价策略；执行编译只读冻结值，并在执行步骤的 `resolved` 审计字段保存从已发送 wire 推导的策略。
- 新增离线合同回归 `tests/modules/builds/test_strategy_payloads.py`。

## 验证

通过：

- `uv run pytest --confcutdir=<temporary-dir> <temporary-dir>/test_strategy_payloads.py -q`（7 passed）
- `uv run pytest --confcutdir=tests/contracts tests/contracts/test_tiktok_build_contract.py -q`（29 passed，1 个已有 reporting 导入环路导致的 collection/test failure）
- `uv run ruff check`（全部 Task 4 修改文件通过）
- `uv run python -m compileall`（全部 Task 4 修改文件通过）

计划要求的完整命令仍受环境限制：测试 conftest 需要专用 PostgreSQL/Redis；当前工作区已有 reporting 未提交改动造成 `app.modules.accounts.access` 与 `app.modules.reporting.filters` 循环导入，集成测试在 collection 阶段失败。未调用任何真实 TikTok 广告写接口。
