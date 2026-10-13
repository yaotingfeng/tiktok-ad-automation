# 自动规则阶段 2(执行)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 开放「自动执行」「确认后执行」:规则命中后通过现有广告管理链路暂停 / 启用 / 调预算 / 调 ROAS / 启停素材,带频控、反向冷却期冲突检测、单次执行对象上限、每日累计涨幅上限、待确认批次与确认页,并发送执行结果 / 待确认 / 异常飞书卡片。

**Architecture:** 规则运行(`ads-reporting` 队列)判断后:仅通知 → 照旧发卡;确认后执行或超出单次上限 → 生成待确认批次;自动执行 → 投递 `automation.execute`(`ad-management` 队列)。执行器按 BC 分组命中目标,为每个 BC 写一条 `FrozenSelectionRecord`(来源标注自动规则),调用现有 `prepare_preview` 生成冻结预览,按频控 / 冷却期 / 每日涨幅上限排除对象后重新预览,再 `submit_management_task`。实际写入、重试、回读、事后定向刷新全部沿用广告管理。动作台账记录每次提交,作为频控与冷却期依据。

**Tech Stack:** 同阶段 1。

**Spec:** `docs/superpowers/specs/2026-10-14-automated-rules-design.md`(第 4、5、6、7.4 节);阶段 1 计划 `docs/superpowers/plans/2026-10-14-automated-rules-phase1.md`。

## Global Constraints

- 阶段 1 的全部约束继续有效(租户隔离、按账户时区、比率合计重算、币种分组、开关 `AUTOMATED_RULES_ENABLED`、不新增 TikTok 调用通道、测试不做真实写入)。
- 只有租户管理员(`manage` 权限)能保存「自动执行」规则;「确认后执行」「仅通知」需要 `ads_manage`。确认批次需要 `ads_manage`。
- 执行以规则最后修改人(`rule.updated_by`)身份进行,每次执行前重建权限;此人失去 `ads_manage`(自动执行还需 `manage`)时该次执行失败并记录。
- 执行必须走 `prepare_preview` + `submit_management_task`;不得直接调用 TikTok 网关。
- 冲突(同一对象 24 小时内被其他规则做过相反动作,或同一轮被要求相反动作)→ 冲突动作都不执行,发异常卡片。冷却期小时数为租户设置(默认 24)。
- 提高预算 / ROAS 时,同一对象当日(租户运行时区)累计涨幅不超过租户设置的百分比(默认 100%),超出的对象跳过。
- 命中执行对象总数超过 `max_targets` 时,自动执行本轮改为待确认。
- 待确认批次在该规则下一次运行开始时失效;确认时用最新数据重新判断。
- 预算 / ROAS 调整的上 / 下限通过 `MutationSpec.max_value` / `min_value` 在预览计算时钳制(新增字段)。
- 执行前不做同步的定向刷新:目录刷新是异步任务,预览以当前目录观测为基准;广告管理在写入前核对观测未变化、写入后定向刷新。设计第 4 节第 9 步的「执行前定向刷新」以此替代。
- 前端只开放「自动执行」给租户管理员;表单在提交前校验调整数值、上下限、频控、单次上限。

## Review Focus

1. **同一对象被两条规则同一轮要求相反动作**:两条运行几乎同时到达执行器,租户级事务锁保证后到者看到先到者的台账,判为冲突。Task 4 测试 `test_concurrent_reverse_actions_conflict`。
2. **规则最后修改人被降级为只读**:自动执行失败并记录 `rule_owner_forbidden`,不提交任何管理任务。Task 4 测试 `test_owner_without_manage_is_not_executed`。
3. **确认时对象已不满足条件**:默认不勾选、可显式勾选;状态已变为目标状态的对象跳过(预览里 NO_CHANGE)。Task 6 测试 `test_confirm_detail_rejudges_with_current_data`。
4. **一个 BC 的提交失败不影响其他 BC**:每个 BC 在独立保存点中处理,失败 BC 记录原因,其他 BC 照常提交。Task 4 测试 `test_one_bc_failure_does_not_block_others`。
5. **涨幅钳制后等于当前值**:预览 NO_CHANGE,不提交、不写台账。Task 1 测试 `test_bounds_clamp_increase_and_decrease`、Task 4 测试 `test_no_change_targets_skip_ledger`。

---

## 文件结构

| 文件 | 操作 | 职责 |
| --- | --- | --- |
| `backend/app/modules/ad_management/schemas.py` | 修改 | `MutationSpec` 增加 `min_value`、`max_value` |
| `backend/app/modules/ad_management/expansion.py` | 修改 | `_final_value` 钳制 |
| `backend/app/modules/automation_rules/models.py` | 修改 | 新增 `AutomationActionLedger`、`AutomationConfirmation`;`AutomationTenantSetting` 增加 `reverse_cooldown_hours`、`daily_increase_limit_pct`;`AutomationRuleRun` 增加 `mode`、`execution_status`、`execution_summary` |
| `backend/app/alembic/versions/0027_automation_execution.py` | 新建 | 上述表与列 |
| `backend/app/modules/automation_rules/actions.py` | 新建 | 规则操作 → `MutationSpec`;目标按 BC 分组;相反动作判断 |
| `backend/app/modules/automation_rules/executor.py` | 新建 | 执行一批命中:锁、频控、冷却期、每日上限、冻结选择、预览、提交、台账、结果回写 |
| `backend/app/modules/automation_rules/confirmations.py` | 新建 | 待确认批次的创建、失效、详情(重新判断)、确认、忽略 |
| `backend/app/modules/automation_rules/runner.py` | 修改 | 按执行模式分流;卡片类型 |
| `backend/app/modules/automation_rules/tasks.py` | 修改 | `automation.execute` 任务 |
| `backend/app/modules/automation_rules/service.py`、`schemas.py`、`api.py`、`settings_service.py` | 修改 | 模式权限、确认接口、设置字段、运行明细的执行结果 |
| `frontend/src/features/automation-rules/*` | 修改 / 新建 | 开放模式、表单校验、待确认页、运行明细执行结果、设置字段 |

---

### Task 1: MutationSpec 上下限钳制

**Files:** Modify `backend/app/modules/ad_management/schemas.py`、`expansion.py`;Test `backend/tests/modules/ad_management/test_previews.py`

**Interfaces:** Produces `MutationSpec.min_value: Decimal | None = None`、`MutationSpec.max_value: Decimal | None = None`(仅数值字段;`max_value` 只用于 add / increase_percent,`min_value` 只用于 subtract / decrease_percent,其他组合校验失败)。`_final_value` 在计算后执行 `min(result, max_value)` / `max(result, min_value)`。

- [ ] **Step 1: 写失败测试**(追加到 `test_previews.py`)

```python
def test_bounds_clamp_increase_and_decrease():
    from app.modules.ad_management.expansion import _final_value

    up = MutationSpec(field="budget", mode="increase_percent", value=Decimal("50"), max_value=Decimal("120"))
    assert _final_value(Decimal("100"), up) == Decimal("120")
    down = MutationSpec(field="budget", mode="decrease_percent", value=Decimal("50"), min_value=Decimal("80"))
    assert _final_value(Decimal("100"), down) == Decimal("80")
    # 当前值已达上限时钳制结果等于当前值,预览判为 NO_CHANGE。
    assert _final_value(Decimal("120"), up) == Decimal("120")


@pytest.mark.parametrize("kwargs", [
    {"field": "budget", "mode": "set", "value": "10", "max_value": "20"},
    {"field": "budget", "mode": "add", "value": "10", "min_value": "5"},
    {"field": "status", "mode": "set", "value": "ENABLE", "max_value": "5"},
    {"field": "budget", "mode": "add", "value": "10", "max_value": "0"},
])
def test_bounds_validation(kwargs):
    with pytest.raises(ValueError):
        MutationSpec.model_validate(kwargs)
```

- [ ] **Step 2: 运行确认失败**:`uv run --frozen pytest tests/modules/ad_management/test_previews.py -q -k bounds`,预期 ValidationError(extra 字段被拒)。
- [ ] **Step 3: 实现**:`MutationSpec` 新增两个可选 Decimal 字段(`field_validator` 复用 `_decimal`),在 `validate_operation` 末尾校验上述组合与正数;`_final_value` 在 `result` 计算后、正数检查前钳制。中文注释说明「提高必须有上限、降低必须有下限,由自动规则传入」。
- [ ] **Step 4: 运行** `tests/modules/ad_management -q` 全部通过。
- [ ] **Step 5: 提交** `ad-management: 预算与 ROAS 调整支持上下限钳制`。

---

### Task 2: 执行相关数据表与租户设置

**Files:** Modify `models.py`、`settings_service.py`、`schemas.py`(设置模型);Create `0027_automation_execution.py`;Test `test_models.py`、`test_settings.py`

**Interfaces:**
- `AutomationActionLedger`(`id, tenant_id, rule_id(FK CASCADE), run_id(FK CASCADE), target_key(varchar 300), action(varchar 32:PAUSE/ENABLE/BUDGET_UP/BUDGET_DOWN/BUDGET_SET/ROAS_UP/ROAS_DOWN/ROAS_SET), original_value varchar(128) null, final_value varchar(128) null, management_task_id uuid null, created_at`;索引 `(tenant_id, target_key, created_at)`。
- `AutomationConfirmation`(`id, tenant_id, rule_id(FK CASCADE), run_id(FK CASCADE, unique), status(PENDING/CONFIRMED/DISMISSED/EXPIRED), expires_at null, decided_by uuid null(FK user), decided_at null, created_at`;检查约束状态值;索引 `(tenant_id, status, created_at)`)。
- `AutomationRuleRun` 新增 `mode varchar(16) default 'NOTIFY'`、`execution_status varchar(24) default 'NOT_REQUIRED'`(NOT_REQUIRED / AWAITING_CONFIRMATION / QUEUED / SUBMITTED / PARTIAL / FAILED / DISMISSED / EXPIRED)、`execution_summary JSONB default {}`(`submitted, skipped_frequency, skipped_cooldown, skipped_daily_limit, conflicts, no_change, failed_bcs: [{bc_id, reason}], tasks: [{bc_id, task_id}]`)。
- `AutomationRuleHit.outcome` 取值扩展:NOTIFIED / PENDING_CONFIRMATION / SUBMITTED / SKIPPED_FREQUENCY / SKIPPED_COOLDOWN / SKIPPED_DAILY_LIMIT / CONFLICT / NO_CHANGE / FAILED / NOT_SELECTED。
- `AutomationTenantSetting` 新增 `reverse_cooldown_hours int default 24`(1–168)、`daily_increase_limit_pct int default 100`(1–1000);`AutomationSettingsPublic` / `AutomationSettingsUpdate` 同步增加这两个字段(Update 中有默认值,保持旧请求可用)。

- [ ] **Step 1: 写失败测试**:`test_models.py` 新增 `test_one_confirmation_per_run`(同一 run 两条确认 → IntegrityError);`test_settings.py` 新增 `test_execution_settings_roundtrip`(PUT `reverse_cooldown_hours=12, daily_increase_limit_pct=50` 后 GET 返回相同值;`reverse_cooldown_hours=0` → 422)。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现**模型、迁移(`down_revision = "0026_automation_rules"`,含 downgrade)、设置读写。
- [ ] **Step 4: 运行** `tests/modules/automation_rules -q`、`alembic check`、降级再升级。
- [ ] **Step 5: 提交** `automation-rules: 动作台账、待确认批次与执行设置数据表`。

---

### Task 3: 规则操作映射

**Files:** Create `actions.py`;Test `test_actions.py`

**Interfaces:**
- `action_key(action: Action) -> str`:PAUSE / ENABLE / `BUDGET_UP|DOWN|SET` / `ROAS_UP|DOWN|SET`。
- `opposite(a: str, b: str) -> bool`:PAUSE↔ENABLE;`*_UP`↔`*_DOWN`(同一字段);两个 `*_SET` 视为相反仅当来自不同规则(由调用方保证),这里 `*_SET` 与同字段任一调整都相反。
- `mutation_for(action: Action) -> MutationSpec`(不含引用):PAUSE/ENABLE → `status set DISABLE/ENABLE`,素材层级 → `material_status`;BUDGET → `budget`,ROAS → `roas`;`INCREASE+PERCENT → increase_percent, max_value=limit`;`INCREASE+AMOUNT → add, max_value=limit`;`DECREASE+PERCENT → decrease_percent, min_value=limit`;`DECREASE+AMOUNT → subtract, min_value=limit`;`SET → set`。
- `group_by_bc(targets: Iterable[dict]) -> dict[str, list[dict]]`(按 `bc_id`,顺序稳定)。
- `selection_payload(targets, tenant_id) -> tuple[list[dict], list[dict]]`:非素材目标 → `refs`(`{tenant_id, advertiser_id, kind, remote_id}`),素材目标 → 展开其全部 `material_uses` 为 `material_uses`(`{ad_ref: {...kind:"ad"}, platform_material_id, ad_material_id, material_type}`)。
- `frequency_allows(frequency: Frequency | None, history: list[datetime], now) -> bool`:ONCE → 无历史;EVERY_24H/48H/WEEK → 最近一次早于窗口;CUSTOM → 窗口内次数 < count;None → True。

- [ ] **Step 1: 写失败测试**:覆盖上述每个映射、`opposite` 真值表、`frequency_allows` 各类型边界(恰好 24 小时)、素材目标展开为 material_uses。
- [ ] **Step 2–4:** RED → 实现 → GREEN。
- [ ] **Step 5: 提交** `automation-rules: 规则操作映射为广告管理变更`。

---

### Task 4: 执行器

**Files:** Create `executor.py`;Test `test_executor.py`

**Interfaces:**
- `execute_hits(session, *, run: AutomationRuleRun, rule: AutomationRule, selected_keys: set[str] | None, now, require_manage: bool) -> dict`:返回并写入 `run.execution_summary`、更新命中 `outcome`、`run.execution_status`(全部提交 → SUBMITTED;部分 BC 失败或部分跳过 → PARTIAL;全部失败 → FAILED;没有可执行对象 → SUBMITTED 且 submitted=0)。`selected_keys` 为空表示全部命中;否则只执行这些 `subject_key`,其他命中标 NOT_SELECTED。
- 处理顺序:
  1. `pg_advisory_xact_lock(hashtextextended('automation-exec:'||tenant, 0))` 串行同租户执行。
  2. `require_tenant(actor=rule.updated_by, action="ads_manage")`,`require_manage` 时再要 `manage`;失败 → FAILED `rule_owner_forbidden`,不提交。
  3. 收集选中命中的目标(按 `target.key` 去重,记住来自哪个命中)。
  4. 对每个目标查台账:同租户、同 `target_key`、`created_at >= now - cooldown` 且来自其他规则、动作与本动作 `opposite` → 冲突(命中 outcome=CONFLICT,计数 conflicts);同规则同动作历史不满足 `frequency_allows` → SKIPPED_FREQUENCY。
  5. 按 BC 分组;每个 BC 在 `session.begin_nested()` 中:写 `FrozenSelectionRecord`(actor=rule.updated_by,`snapshot_id=uuid4()`,`advertiser_ids` 为该 BC 目标账户,`filters={"source":"automation_rule","rule_id","run_id"}`,`filter_digest`=sha256(filters),`refs`/`material_uses` 来自 `selection_payload`,`membership_digest` 用 `reporting.selection._digest` 同口径计算,`expires_at=now+15min`)→ `prepare_preview(...)`;对提高类动作,读取预览项 `final_value` 与当日(租户运行时区零点起)该对象最早台账 `original_value`(无则用本次 `original_value`)比较,超过 `基准 ×(1+上限%)` 的对象加入 `excluded_refs` 并重新 `prepare_preview`;预览项 `NO_CHANGE` 不提交、outcome=NO_CHANGE;若仍有 PENDING 项 → `submit_management_task(..., idempotency_key=uuid5(NAMESPACE_URL, f"automation:{run.id}:{bc_id}"))`,对每个提交项写台账(original/final、management_task_id),命中 outcome=SUBMITTED;BC 内任何 `DomainError`/`HTTPException` → 回滚保存点,该 BC 目标 outcome=FAILED,`failed_bcs` 记录 `{bc_id, reason}`。
- `MANAGEMENT_SOURCE = "automation_rule"`:写入 `FrozenSelectionRecord.filters` 供操作记录展示来源。

- [ ] **Step 1: 写失败测试**(使用 `tests/modules/ad_management/conftest.py` 的 `management_env` 思路:新增夹具 `execution_env`,在一个 BC 下建系列/广告组 AdObject、ManagementCapability(update_budget/set_status)、规则与一条已完成运行及命中;提交走真实 `submit_management_task`,只断言 `ManagementTask` 行与 outbox,不执行 provider)。用例:
  - `test_auto_execution_submits_management_task`(PAUSE 广告组 → 1 个 ManagementTask,台账 1 条,命中 SUBMITTED,运行 SUBMITTED)
  - `test_frequency_once_skips_second_run`
  - `test_reverse_action_within_cooldown_is_conflict`(另一规则 1 小时前 ENABLE 过同一对象 → CONFLICT,不提交)
  - `test_concurrent_reverse_actions_conflict`(两个独立提交的会话依次执行相反动作;第二个看到第一个的台账)
  - `test_daily_increase_limit_excludes_target`(当日已从 100 涨到 180,本次再 +20%、上限 1000、日上限 100% → 跳过)
  - `test_no_change_targets_skip_ledger`(预算已达上限)
  - `test_owner_without_manage_is_not_executed`(require_manage=True,修改人为 operator → FAILED)
  - `test_one_bc_failure_does_not_block_others`(第二个 BC 无默认路由 → 该 BC FAILED,第一个 BC SUBMITTED,运行 PARTIAL)
  - `test_selected_keys_limit_execution`(只执行一个命中,另一个 NOT_SELECTED)
- [ ] **Step 2–4:** RED → 实现 → GREEN(`tests/modules/automation_rules tests/modules/ad_management -q`)。
- [ ] **Step 5: 提交** `automation-rules: 通过广告管理链路执行规则动作`。

---

### Task 5: 运行分流与执行任务

**Files:** Modify `runner.py`、`tasks.py`、`service.py`(模式权限);Test `test_runner.py`、`test_tasks.py`、`test_api.py`

**Interfaces:**
- `execute_run` 写命中后按 `definition.mode`:
  - NOTIFY → 原逻辑。
  - CONFIRM,或 AUTO 且所有命中 `target_count` 之和 > `max_targets` → `confirmations.open_batch(session, run, rule, now)`;`run.execution_status="AWAITING_CONFIRMATION"`;命中 outcome=PENDING_CONFIRMATION;通知照发(卡片类型「待确认」)。
  - AUTO 且未超限且有命中 → `run.execution_status="QUEUED"`,投递 `automation.execute`(payload `{"run_id", "mode": "AUTO"}`,task_key `automation-exec:{run_id}`);通知改由执行完成后发送(`notify_status` 保持 NOT_REQUIRED,执行器完成后置 PENDING 并投递发送)。
  - 运行开始时调用 `confirmations.expire_open(session, rule_id, now)`,把该规则仍为 PENDING 的批次置 EXPIRED,对应运行 `execution_status="EXPIRED"`。
- `automation.execute`(`ad-management` 队列,`register_dispatch_task` 放在投递方 runner):payload `{"run_id", "mode", "selected_keys"?}`;读取运行、规则,`runnable` 检查;调用 `execute_hits(require_manage=mode=="AUTO")`;完成后置 `notify_status=PENDING` 并投递飞书发送。
- 保存规则:`mode == "AUTO"` 需要 `manage`(否则 `action_forbidden` 403);删除 `automation_mode_unavailable` 校验与错误码引用(错误码条目保留在 `ERROR_HTTP_STATUS` 中删除)。
- 规则修改人后续被降级不影响已保存规则;执行时由执行器校验。

- [ ] **Step 1: 写失败测试**:`test_auto_run_queues_execution`、`test_confirm_run_opens_batch_and_expires_previous`、`test_auto_over_limit_becomes_confirmation`、`test_operator_cannot_save_auto_rule`(403)、`test_admin_saves_auto_rule`(201)、`test_execute_task_payload_validation`;修改阶段 1 用例 `test_rejects_non_notify_mode_and_duplicates` 为只测重名。
- [ ] **Step 2–4:** RED → 实现 → GREEN。
- [ ] **Step 5: 提交** `automation-rules: 按执行模式分流，开放自动执行与确认后执行`。

---

### Task 6: 待确认批次

**Files:** Create `confirmations.py`;Modify `schemas.py`、`api.py`;Test `test_confirmations.py`

**Interfaces:**
- `open_batch`、`expire_open`(Task 5 使用)。
- `list_confirmations(session, *, context, status="PENDING") -> ConfirmationPage`(`items: [{id, rule_id, rule_name, run_id, status, created_at, hit_count, target_count}]`)。
- `confirmation_detail(session, *, context, confirmation_id, now=None) -> ConfirmationDetail`:用运行的定义快照重新 `load_inputs` + `evaluate`,对每个命中返回 `{subject_key, subject_label, drama_name, target_count, metrics_then, metrics_now, still_matching: bool}`。
- `confirm(session, *, context, confirmation_id, subject_keys: list[str]) -> ConfirmationPublic`:需 `ads_manage`;状态必须 PENDING 且未过期,否则 `automation_confirmation_closed`(409);`subject_keys` 必须是该运行命中的子集且非空;置 CONFIRMED、记录 decided_by;投递 `automation.execute`(payload `mode="CONFIRM"`、`selected_keys`)。
- `dismiss(...)`:置 DISMISSED,运行 `execution_status="DISMISSED"`,命中 outcome=NOT_SELECTED。
- 路由:`GET /automation-confirmations?status=`、`GET /automation-confirmations/{id}`、`POST /automation-confirmations/{id}/confirm`(body `{"subject_keys": [...]}`)、`POST /automation-confirmations/{id}/dismiss`。错误码 `automation_confirmation_not_found` 404、`automation_confirmation_closed` 409。

- [ ] **Step 1: 写失败测试**:`test_confirm_detail_rejudges_with_current_data`(修改事实使一个命中不再满足 → `still_matching=False`)、`test_confirm_queues_execution_with_selected_keys`、`test_confirm_twice_is_conflict`、`test_dismiss_closes_batch`、`test_viewer_cannot_confirm`、`test_other_tenant_cannot_see_batch`。
- [ ] **Step 2–4:** RED → 实现 → GREEN。
- [ ] **Step 5: 提交** `automation-rules: 待确认批次与确认接口`。

---

### Task 7: 飞书卡片类型与运行明细的执行结果

**Files:** Modify `runner.py`(`build_run_card`)、`service.py`(`get_run`)、`schemas.py`;Test `test_runner.py`、`test_api.py`

**Interfaces:**
- 卡片按运行状态选择:
  - 待确认:标题 `【待确认】{规则名}`,摘要含「请在 {有效期说明} 前确认」,按钮「去确认」链接 `/tenants/{t}/automation-rules?tab=confirmations&confirmation={id}`,模板颜色 `yellow`。
  - 执行结果:标题 `【已执行】{规则名}`,摘要「提交 N 个对象;频控跳过 a、冷却期冲突 b、超出每日涨幅 c、无需变更 d」,失败 BC 列出原因,颜色 `green`(有失败为 `red`)。
  - 有冲突或失败时额外一行「冲突:对象 X 与规则 Y 相反」(最多 5 条)。
  - 预警(仅通知)与运行报告保持阶段 1 行为。
- `RunDetail` 增加 `mode`、`execution_status`、`execution_summary`、每条命中 `outcome`;`execution_summary.tasks` 中的 `task_id` 供前端链接到现有操作记录页 `/tenants/$tenantId/ad-management-tasks/$taskId`。

- [ ] **Step 1: 写失败测试**:三种卡片标题与按钮链接;`get_run` 返回 outcome 与 summary。
- [ ] **Step 2–4:** RED → 实现 → GREEN。
- [ ] **Step 5: 提交** `automation-rules: 执行结果与待确认飞书卡片`。

---

### Task 8: 前端:执行模式、表单校验、待确认页、执行结果

**Files:** Modify `RuleEditorSheet.tsx`、`ruleForm.ts`、`RunHistory.tsx`、`AutomationRulesPage.tsx`、`NotificationSettingsPage.tsx`、路由搜索参数;Create `Confirmations.tsx`;重新生成客户端;Test `automation-rule-form.spec.ts`、`automation-rules.spec.ts`、`utils/automationBoundary.ts`

**Interfaces:**
- `ruleForm.validateDefinition(def): string[]`(中文错误):条件数值为空或非数字;介于下限 ≥ 上限;调整数值为空或 ≤0;提高缺上限、降低缺下限;自定义频控窗口/次数为空;单次上限 1–5000;仅一次时间为空;按 BC 时未选 BC;指定账户/对象为空;合计口径含属性条件;素材相关规则频率低于 1 小时。保存与试运行前调用,有错误时在抽屉顶部列出且不提交。
- 执行模式:租户管理员可选「自动执行」;其他角色「自动执行」禁用并说明「需要租户管理员」;「确认后执行」对有写权限成员开放。
- 页面新增「待确认」标签(`tab=confirmations`,`confirmation` 参数直接打开批次):列表(规则、时间、命中数、对象数、状态);批次抽屉:对象表,并排「判断时」「现在」指标,`still_matching=false` 默认不勾选并标记「条件已不满足」;按钮「确认执行所选」「忽略本批」。
- 执行记录:新增「执行」列(待确认 / 已排队 / 已提交 N / 部分完成 / 失败 / 已忽略 / 已失效);明细抽屉显示 `execution_summary` 与每条命中的处理结果,`tasks` 链接到操作记录。
- 通知设置新增「反向操作冷却期(小时)」「每日累计涨幅上限(%)」。

- [ ] **Step 1: 写失败测试**:`validateDefinition` 单元用例;「操作员看到自动执行禁用」;「待确认批次勾选与确认请求体」;「执行记录显示已提交并链接操作记录」;「设置页保存冷却期与日涨幅上限」。
- [ ] **Step 2–4:** RED → 实现 → GREEN(`bunx playwright test tests/automation-*.spec.ts --project=workspace`,`bun run lint`,`bun run build`)。
- [ ] **Step 5: 后端回归与进度记录**:`tests/modules/automation_rules tests/modules/ad_management tests/modules/reporting tests/jobs tests/test_migration_graph.py`;更新 `docs/implementation-progress.md`。
- [ ] **Step 6: 提交** `frontend: 自动规则执行模式、待确认与执行结果`;`docs: 记录自动规则阶段 2`。
