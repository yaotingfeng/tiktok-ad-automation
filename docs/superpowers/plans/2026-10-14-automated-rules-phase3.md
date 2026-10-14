# 自动规则阶段 3(完善)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐规格第 7 节与第 9 节阶段 3 的剩余能力:素材启停执行、模板、固定时间汇总发送、规则列表概览/筛选/批量操作、执行记录筛选、可搜索的账户与对象选择器,并处理阶段 1/2 遗留的小问题。

**Architecture:** 全部在现有 `backend/app/modules/automation_rules` 与 `frontend/src/features/automation-rules` 内完成;新增一张表字段(规则最近汇总时间)与一个 Beat 任务;不新增服务、队列或 TikTok 调用。

**Tech Stack:** FastAPI + SQLModel + Alembic + Celery;React + TanStack Query/Router + shadcn/ui;pytest(真实 PostgreSQL/Redis)、Playwright。

**Spec:** `docs/superpowers/specs/2026-10-14-automated-rules-design.md`

## Global Constraints

- 指标只用 TK-ADA 已同步的数据;执行只走广告管理现有通道,不新增 TikTok 调用。
- `AUTOMATED_RULES_ENABLED` 默认 false;开发期间不在真实账户写入。
- 非平凡业务逻辑写中文注释;不留兼容旧逻辑的分支与死代码;复用现有 helper。
- 前端文案中文全角标点;后端错误码沿用 `automation_*` 前缀。
- 只租户管理员可保存自动执行规则;确认需要 `ads_manage`。

## Review Focus

1. 汇总发送在租户时区切换、夏令时、规则停用期间不重复发送也不丢失(按 `last_summary_at` 与汇总时刻计算)。
2. 待确认卡片在汇总模式下仍立即发送(批次在下次运行即失效,延迟发送会让确认失去意义)。
3. 批量删除/停用只作用于本租户规则;自动执行规则的批量开启仍要求管理员。
4. 选择器搜索只返回本租户、仍可访问的账户和对象,结果有上限。
5. 素材启停:非 Smart+ 或缺少广告内素材 ID 的素材记为「不支持」而不是「失败」,并在卡片与明细中说明。

---

### Task 1: 素材启停执行与「不支持」结果

**Files:** Modify `executor.py`、`runner.py`(`_execution_lines`)、`frontend/.../DryRunPanel.tsx`(`OUTCOME_LABELS`)、`RunHistory.tsx`;Test `test_executor.py`

**Interfaces:**
- 执行摘要新增 `unsupported` 计数与 `unsupported_details: [{target_key, reason}]`(最多 20 条);命中结果新增 `UNSUPPORTED`,优先级在 `NO_CHANGE` 与 `SKIPPED_FREQUENCY` 之间。
- 预览项 `execution_result == "UNSUPPORTED"` 时记 `UNSUPPORTED`(原因取预览项 `reason`),其余非 PENDING/NO_CHANGE 记 `FAILED`。
- 运行状态:只有不支持、没有提交时为 `SUBMITTED` 之外的 `SKIPPED`(新增状态,卡片标题仍为【已执行】,颜色 `grey`)。

- [ ] Step 1 写失败测试:
  - Smart+ 广告(`ad_type="UPGRADED_SMART_PLUS"`)带 `AdMaterialReference`(含 `ad_material_id`)与 `set_material_status` 能力时,素材暂停规则提交管理任务,台账键 `material:acc:vid`。
  - 普通广告素材记 `UNSUPPORTED`,`summary["unsupported"] == 1`,`run.execution_status == "SKIPPED"`。
- [ ] Step 2–4 RED → 实现 → GREEN。
- [ ] Step 5 提交 `automation-rules: 素材启停执行与不支持结果`。

### Task 2: 规则模板

**Files:** Create `backend/app/modules/automation_rules/templates.py`;Modify `api.py`、`schemas.py`;Create `frontend/.../TemplatePicker.tsx`;Modify `RuleList.tsx`、`queries.ts`;Test `test_templates.py`、`automation-rules.spec.ts`

**Interfaces:**
- `TEMPLATES: tuple[RuleTemplate, ...]`,`RuleTemplate(key, name, description, definition: dict)`;六个模板与规格 7.3 一致,阈值为示例值,模式均为「仅通知」(由用户确认后再改执行模式):
  - `drama_stop_loss` 剧级止损:DRAMA,今天消耗 > 100 且今天广告收益 ROAS < 0.8 → 暂停广告组。
  - `adgroup_stop_loss` 低效广告组止损:EACH 广告组,近 3 天消耗 > 50 且 ROAS < 0.8 → 暂停广告组。
  - `scale_budget` 跑量加预算:EACH 系列,今天 ROAS > 1.5 → 预算提高 20%,上限 1000,每 24 小时一次。
  - `material_off` 低效素材下线:EACH 素材,近 3 天消耗 > 30 且 ROAS < 0.6 → 暂停素材,运行频率 1H。
  - `spend_breaker` 总花费熔断:ALL,今天消耗 > 5000 → 仅通知(动作暂停系列)。
  - `zero_spend` 零消耗预警:EACH 广告组,创建天数 > 2 且近 3 天消耗 = 0 → 仅通知。
- `GET /tenants/{t}/automation-rules/templates` → `TemplatePage{items: [TemplatePublic{key,name,description,definition: RuleDefinition}]}`(read 权限)。
- 前端「从模板新建」按钮(有写权限):对话框列出模板卡片,选中后以模板名与定义打开编辑器(`EditorTarget{kind:"new", name, definition}`)。

- [ ] Step 1 写失败测试:每个模板通过 `RuleDefinition.model_validate` 与 `_check_mode`;接口返回 6 个;Playwright「从模板新建 → 选剧级止损 → 保存请求体含模板条件」。
- [ ] Step 2–4 RED → 实现 → GREEN。
- [ ] Step 5 提交 `automation-rules: 规则模板`。

### Task 3: 固定时间汇总发送

**Files:** Modify `schemas.py`(`NotificationSetting`)、`models.py`、`runner.py`、`tasks.py`、`celery_app.py`;Create `summary.py`、迁移 `0028_automation_summary.py`;Test `test_summary.py`;Frontend `RuleEditorSheet.tsx`、`ruleForm.ts`

**Interfaces:**
- `NotificationSetting.timing: Literal["EVERY_RUN", "SUMMARY"]`,`summary_times: tuple[time, ...]`(SUMMARY 时 1–6 个,去重排序;EVERY_RUN 时必须为空)。
- `AutomationRule.last_summary_at: datetime | None`(迁移 0028)。
- 汇总模式下:运行命中时 `run.notify_status = "SUMMARY"`(等待汇总),不立即入队;待确认批次仍立即发送待确认卡片;执行结果也并入汇总(`notify_execution` 同样置 `SUMMARY`)。
- `summary.due_slot(times, timezone, now) -> datetime | None`:租户时区下不晚于 now 的最近一个汇总时刻(UTC)。
- `summary.scan_summaries(session, now)`:对启用规则、timing=SUMMARY、`last_summary_at < due_slot` 的规则入队 `automation.send_summary`(task_key `automation-summary:{rule}:{slot}`),并把 `last_summary_at` 设为 slot;首次(为空)只记录 slot 不发,避免启用即补发历史。
- `deliver_summary(session, rule_id, slot)`:取 `notify_status="SUMMARY"` 且 `slot_at <= slot` 的运行,合成一张卡片(标题【汇总】{规则名},运行 N 次、命中 M 个、提交/冲突/失败合计,命中对象按 subject 去重取最近一次,最多 10 条),无命中且 `notify_without_hits=false` 时不发;发送后这些运行置 `SENT`/`FAILED`。
- Beat `scan-automation-summaries` 每 60 秒(`control` 队列)。
- 前端:飞书通知区新增「发送时机」单选(每次运行都发 / 固定时间汇总)与时刻列表(最多 6 个);`validateDefinition` 增加「汇总发送至少设置一个时刻」。

- [ ] Step 1 写失败测试:`due_slot` 跨日、跨夏令时(America/New_York 2026-11-01);首次扫描不发;到点入队且同一 slot 不重复;汇总卡片合并多次运行;待确认卡片立即发送;schema 校验。
- [ ] Step 2–4 RED → 实现 → GREEN(含 `alembic check`、降级再升级)。
- [ ] Step 5 提交 `automation-rules: 固定时间汇总发送`。

### Task 4: 规则列表概览、筛选与批量操作

**Files:** Modify `service.py`、`api.py`、`schemas.py`;Frontend `RuleList.tsx`、`queries.ts`;Test `test_api.py`、`automation-rules.spec.ts`

**Interfaces:**
- `GET /automation-rules/overview` → `RuleOverview{actions_7d: {PAUSE,ENABLE,BUDGET,ROAS}, pending_confirmations, failed_7d, conflicts_7d}`;动作数来自台账(按动作键前缀归类),失败与冲突来自近 7 天运行的执行摘要。
- `POST /automation-rules/batch`,`BatchRequest{rule_ids (1–100), operation: ENABLE|DISABLE|DELETE}` → `BatchResult{updated, skipped: [{rule_id, reason}]}`;逐条复用单条开关/删除逻辑与权限(自动执行规则开启需管理员),不属于本租户的 ID 记为 `not_found`。
- 前端:顶部概览卡片;筛选(状态、执行模式、操作、执行层级)在前端对已加载列表过滤;表格行可勾选,工具栏「批量开启/批量停用/批量删除」(删除二次确认)。

- [ ] Step 1 写失败测试:概览计数;批量操作含他租户 ID 与操作员开启自动规则被跳过;Playwright 批量停用请求体与筛选。
- [ ] Step 2–4 RED → 实现 → GREEN。
- [ ] Step 5 提交 `automation-rules: 规则概览、筛选与批量操作`。

### Task 5: 执行记录筛选

**Files:** Modify `service.list_runs`、`api.py`;Frontend `RunHistory.tsx`、`queries.ts`;Test `test_api.py`、`automation-rules.spec.ts`

**Interfaces:**
- `list_runs` 新增查询参数:`result`(`HIT`/`NO_HIT`/`FAILED`/`EXECUTED`/`CONFLICT`)、`since`/`until`(日期,租户时区解释)、`advertiser_id`、`drama`(包含匹配)。后两者通过命中表 `EXISTS` 过滤。
- 前端筛选栏:规则、结果、日期范围、账户 ID、剧名。

- [ ] Step 1 写失败测试:各筛选组合;他租户数据不出现。
- [ ] Step 2–4 RED → 实现 → GREEN。
- [ ] Step 5 提交 `automation-rules: 执行记录筛选`。

### Task 6: 可搜索的账户与对象选择器

**Files:** Create `backend/app/modules/automation_rules/pickers.py`;Modify `api.py`、`schemas.py`;Frontend Create `Pickers.tsx`,Modify `RuleEditorSheet.tsx`;Test `test_pickers.py`、`automation-rules.spec.ts`

**Interfaces:**
- `GET /automation-rules/pickers/accounts?q=&limit=50` → `[{advertiser_id, name, bc_id, currency, timezone}]`:本租户、`BCAccountAccess.active and authorized` 的账户,按 ID 或名称包含匹配。
- `GET /automation-rules/pickers/objects?level=&q=&advertiser_ids=&limit=50` → `[{value:"账户ID:远端ID", label, advertiser_id, kind}]`:系列/广告组/广告来自 `ad_object`(未删除),素材来自 `ad_material_reference`(主素材 ID 去重)。
- 前端:指定账户、指定对象改为可搜索多选(输入即搜、已选标签可删除),仍支持粘贴多行 ID 批量添加。

- [ ] Step 1 写失败测试:租户隔离、上限、按名称搜索、素材去重;Playwright 搜索并选择账户后保存请求体。
- [ ] Step 2–4 RED → 实现 → GREEN。
- [ ] Step 5 提交 `automation-rules: 可搜索的账户与对象选择器`。

### Task 7: 遗留小问题

**Files:** `settings_service.py`、`feishu.py`、`runner.py`、`RuleEditorSheet.tsx`、`schedule.py`;对应测试

- M3:租户运行时区变更后,重算所有启用规则的 `next_run_at`。
- M5:「仅一次」时间按租户运行时区输入与显示(当前按浏览器时区)。
- M7:卡片因体积截断后,「其余 N 个」按实际展示条数重算。
- M8:无命中、只有跳过账户的运行,跳过账户集合与该规则上一次已发送运行相同时不再发卡片。
- M9:每天固定时刻与自定义时段在夏令时切换日不重复、不跳过(`next_run_at` 测试覆盖 America/New_York 2026-03-08 与 2026-11-01)。

- [ ] Step 1 每项先写失败测试;Step 2–4 RED → GREEN;Step 5 提交 `automation-rules: 时区、卡片截断与重复跳过卡片修正`。

### Task 8: 回归与进度记录

- [ ] 后端:`tests/modules/automation_rules tests/modules/ad_management tests/modules/reporting tests/jobs tests/test_migration_graph.py`;`alembic check`;前端 Playwright 自动规则用例、`tsc`、`vite build`、biome(仅改动文件)。
- [ ] 更新 `docs/implementation-progress.md` 阶段 3 条目;提交 `docs: 记录自动规则阶段 3`。
