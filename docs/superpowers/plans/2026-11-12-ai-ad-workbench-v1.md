# TK-ADA AI 投放工作台 v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在 TK-ADA 内交付多租户 AI 投放工作台 v1：每个租户可创建多个独立 AI 优化师，每位优化师绑定一条投放策略和多条自动规则，按每日剧单定时调用现有广告搭建，复盘按自动优化或人工确认模式更新配置。

**Architecture:** TK-ADA 保存租户、优化师、目标、BC/账户范围、单一投放策略引用、规则绑定、每日剧单、搭建发生记录、提案和证据，是唯一业务事实来源。DSH 作为独立复盘进程，通过受控 TK-ADA 领域 API 读取证据并提交结构化提案；它不直连 TikTok、版权方或素材中台，也不直接写数据库。现有策略、自动规则、素材、版权方、搭建、广告管理和报表模块继续作为执行能力，AI 工作台只编排和呈现。

**Tech Stack:** FastAPI、SQLModel/PostgreSQL、Celery/Redis/Beat、现有官方 TikTok SDK/MCP 通道、React/TypeScript、TanStack Router、shadcn/ui、DSH 受控任务服务。

**Spec:** `docs/superpowers/specs/2026-11-11-ai-ad-workbench-v2-design.md`

## 2026-11-12 交付状态

Task 1–7 的代码与本地验收已完成，当前迁移 head 为 `0126_ai_plan_retry_fairness`。本轮在 `612d0b43` 基础上补齐真实登录请求、优化师配置界面、稳定行和异步搭建、素材订单持久化/幂等提交/查询、DSH 原生运行接入，以及提案证据和受控发布。独立复审提出的扫描公平性、暂停订单和经营证据账户隔离问题均已修正并复审通过。

清单勾选表示相应交付产物及行为验收已完成；最初的 RED/提交步骤属于实施过程记录，不代表本轮逐字重跑所有历史示例命令。实际执行的 Python、Node、TypeScript、Vite、Playwright 命令和边界见[验收记录](../../validation/2026-11-12-ai-workbench-v1.md)。本机 Bun 不可用，使用同一依赖树的 Node 工具验证，不宣称运行过 Bun。

真实外部素材订单、真实 TikTok 写入、真实模型复盘与生产部署仍不属于本地验收；接入配置和上线迁移要求见[运行手册](../../runbooks/ai-ad-workbench.md)。

## 2026-11-13 复盘业务能力范围更正

上节“完成”仅对应原分支实现和记录中的协议/页面/本地测试，不代表专业复盘的数据、知识、Skill 和经验反馈已齐全。用户已确认采用统一主 Skill 加专项方法的方案；固定数据包、方法正文、租户知识与多轮反馈均待实施，见[后续专项计划](2026-11-13-ai-review-methods-and-learning.md)。原任务历史勾选不作为新范围已完成的证据。

## Global Constraints

- 每个租户可创建多个 AI 优化师；每个优化师只绑定一条投放策略、自己的自动规则绑定、目标、BC、账户范围、排期、复盘和授权。
- AI 工作台是 TKADA 最左侧菜单的独立一级入口，位于“打开体验版”正上方；进入后使用第二列 AI 侧栏，内容区不再使用工作台横向页签。
- 自动复制放量、多策略编排、A/B 实验、自动选剧暂不实现；现有预算、ROAS、启停自动规则继续使用。
- 排期只保存剧名、时间、版权方/应用、链接/剧目 ID、是否下单素材等搭建输入；不提前选择素材。执行准备阶段按剧匹配素材，账户继承优化师范围。
- 自动优化模式只允许预授权字段、数值、账户和预算范围内自动发布；人工确认模式下所有 AI 策略或规则调整逐条采纳/不采纳，拒绝不改变生效版本。
- 所有 TikTok 写入继续经过现有 TKADA 搭建/广告管理能力；不新增自定义 TikTok HTTP 网关，不把 DSH 会话当执行器。
- 所有查询、写入、任务、证据和规则范围必须携带 tenant_id、optimizer_id；不得跨租户、跨优化师或跨 BC 复用状态。
- 开发与验收不得调用真实广告写入、真实素材订单或真实版权方写入；真实联调另按具体授权进行。

## Review Focus

- **租户与优化师串数据：** 同名剧、同名策略和同名规则在不同租户/优化师之间不可读取或发布；由 Task 1 API/权限测试覆盖。
- **优化师切换残留状态：** 切换优化师必须清理草稿、绑定选择、待发布提案和返回上下文；由 Task 6 前端回归覆盖。
- **未知或重复搭建：** 同一排期发生记录、策略版本、请求编号重复触发不得重复提交；未知结果先核对；由 Task 3/4 任务测试覆盖。
- **人工确认绕过：** 人工确认模式下即使字段在授权范围内也不能自动发布；拒绝不改生效版本；由 Task 5 提案状态测试覆盖。
- **证据与规则范围：** 自动规则应监控优化师分配账户内符合条件的全部广告，历史运行必须保存规则版本、指标快照和动作回执；由 Task 4/5 数据与保留测试覆盖。

---

### Task 1: AI 优化师与租户隔离领域模型

**Files:**
- Create: `backend/app/modules/ai_workbench/models.py`
- Create: `backend/app/modules/ai_workbench/schemas.py`
- Create: `backend/app/modules/ai_workbench/service.py`
- Create: `backend/app/modules/ai_workbench/api.py`
- Create: `backend/app/alembic/versions/<revision>_ai_workbench_optimizer.py`
- Modify: `backend/app/api/main.py`
- Modify: `backend/app/models.py`
- Test: `backend/tests/modules/ai_workbench/test_optimizer.py`
- Test: `backend/tests/modules/ai_workbench/test_optimizer_permissions.py`

**Interfaces:**
- Produces `AIOptimizer`, one active `AIOptimizerStrategyBinding` per optimizer, `AIOptimizerVersion`, `AIOptimizerRuleBinding` and API operations `GET/POST/PATCH /tenants/{tenant_id}/ai-optimizers`.
- `AIOptimizerVersion` stores `daily_spend`, `target_roas`, `priority`, `bc_id`, advertiser scope, `optimization_mode`, authorization limits and the binding reference; the binding has a database uniqueness constraint on `(tenant_id, optimizer_id, active)` and points to exactly one tenant-owned `StrategyVersion`.

- [x] **Step 1: Write failing isolation tests** for tenant mismatch, optimizer mismatch, viewer write rejection, invalid BC/account scope, missing strategy binding and invalid optimization mode.
- [x] **Step 2: Run the focused tests** with `uv run pytest backend/tests/modules/ai_workbench/test_optimizer.py backend/tests/modules/ai_workbench/test_optimizer_permissions.py -q`; expect missing module/schema failures.
- [x] **Step 3: Implement models, migration, schemas and service** using `TenantContext` and existing tenant permission helpers. Enforce exactly one active strategy binding per optimizer, immutable optimizer versions, and a shared-reference guard: if a strategy is used by another optimizer, AI publication must reject it unless an explicit optimizer-owned strategy version is created through the existing strategy append service. Create/update must record actor, request id, digest and audit event.
- [x] **Step 4: Register the router** in `backend/app/api/main.py` and add OpenAPI response contracts for list/detail/create/update.
- [x] **Step 5: Re-run focused tests** and the relevant tenant permission suite; expect all pass.
- [x] **Step 6: Commit** `feat: add tenant-scoped AI optimizer model`.

### Task 2: Daily drama plan and manual Excel-style input contract

**Files:**
- Create: `backend/app/modules/ai_workbench/plans.py`
- Create: `backend/app/modules/ai_workbench/plan_schemas.py`
- Create: `backend/app/alembic/versions/<revision>_ai_workbench_daily_plans.py`
- Modify: `backend/app/modules/ai_workbench/api.py`
- Reuse: `frontend/src/features/builds/dramaGrid.ts`
- Create: `frontend/src/features/ai-workbench/DramaPlanGrid.tsx`
- Create: `frontend/src/features/ai-workbench/AIWorkbenchPage.tsx`
- Test: `backend/tests/modules/ai_workbench/test_daily_plans.py`
- Test: `frontend/tests/ai-workbench.spec.ts`

**Interfaces:**
- `POST/PATCH/GET /tenants/{tenant_id}/ai-optimizers/{optimizer_id}/daily-plans`.
- Plan row fields: `business_date`, `scheduled_time`, `title`, `provider_connection_id/application_id`, optional `promotion_url`, optional string `external_drama_id`, `order_materials`, `optimizer_id`; no material selection fields and no row-level advertiser override.

- [x] **Step 1: Write failing backend plan tests** for TSV payload validation, duplicate title rejection within a day, leading-zero drama IDs, tenant/optimizer ownership, and saving order intent without selecting materials.
- [x] **Step 2: Add frontend behavior tests** for ten blank rows, direct multi-cell TSV paste, Tab/Enter navigation, undo, date switching, and no material picker/selected-count before execution.
- [x] **Step 3: Implement plan models and API** with revisioned rows and optimistic concurrency; copy the parsing/validation semantics from `dramaGrid.ts` without binding uploads to dramas.
- [x] **Step 4: Implement `DramaPlanGrid`** with existing shadcn table/input patterns and route it under the AI workbench side rail. Show inherited optimizer BC/accounts/strategy context as read-only.
- [x] **Step 5: Run focused backend and Playwright tests**; expect plan rows to persist and remain isolated.
- [x] **Step 6: Commit** `feat: add AI optimizer daily drama plans`.

### Task 3: Scheduled build handoff to existing TKADA builds

**Files:**
- Create: `backend/app/modules/ai_workbench/build_handoff.py`
- Create: `backend/app/modules/ai_workbench/build_tasks.py`
- Modify: `backend/app/jobs/celery_app.py`
- Modify: `backend/app/jobs/outbox.py`
- Reuse: `backend/app/modules/builds/drafts.py`, `backend/app/modules/builds/previews.py`, `backend/app/modules/builds/submissions.py`, `backend/app/modules/builds/reconciliation.py`
- Modify: `backend/app/modules/builds/models.py` only if a nullable optimizer/plan occurrence reference is required
- Test: `backend/tests/modules/ai_workbench/test_build_handoff.py`
- Test: `backend/tests/modules/builds/test_ai_workbench_handoff.py`

**Interfaces:**
- `POST /tenants/{tenant_id}/ai-optimizers/{optimizer_id}/plans/{plan_id}/run` for manual dry dispatch and an internal Beat task `ai_workbench.dispatch_due_plans`.
- `BuildHandoff.create_occurrence(plan_row_id, request_id) -> BuildOccurrence`; the service resolves the optimizer's active strategy binding and freezes its current `StrategyVersion`.

- [x] **Step 1: Write failing tests** for inherited advertiser scope, rejection of a caller-supplied cross-optimizer/expired strategy, exact strategy version freeze from the optimizer binding, provider/link resolution, execution-time material matching, idempotency on `(optimizer, plan_row, resolved_strategy_version, scheduled occurrence)`, and unknown-result reconciliation.
- [x] **Step 2: Implement occurrence and outbox records** with a stable request digest, frozen optimizer/BC/account/strategy snapshot resolved only from the active binding, and explicit statuses `PENDING`, `WAITING_MATERIAL`, `DISPATCHED`, `PARTIAL`, `UNKNOWN`, `COMPLETE`, `FAILED`.
- [x] **Step 3: Implement the handoff** by creating an existing `BuildDraft`, passing the plan drama input without material selection, invoking existing preparation/matching, freezing preview, submitting through existing builds service, and importing readback/created object evidence.
- [x] **Step 4: Add Beat/Celery dispatch** that claims due plan rows with tenant-safe locks, never re-submits an unknown occurrence, and records skipped/blocked reasons.
- [x] **Step 5: Run builds recovery, concurrency, and focused handoff tests**; expect duplicate dispatch and cross-scope tests to pass.
- [x] **Step 6: Commit** `feat: schedule AI optimizer builds through TKADA`.

### Task 4: Optimizer-scoped automatic-rule binding and evidence

**Files:**
- Create: `backend/app/modules/ai_workbench/rule_binding.py`
- Create: `backend/app/modules/ai_workbench/evidence.py`
- Modify: `backend/app/modules/automation_rules/inputs.py`
- Modify: `backend/app/modules/automation_rules/runner.py`
- Modify: `backend/app/modules/automation_rules/executor.py`
- Modify: `backend/app/modules/automation_rules/models.py` only for optimizer binding/version/retention references
- Create: `backend/app/alembic/versions/<revision>_ai_workbench_rule_bindings.py`
- Test: `backend/tests/modules/ai_workbench/test_rule_binding.py`
- Modify/Test: `backend/tests/modules/automation_rules/test_inputs.py`, `test_executor.py`, `test_tasks.py`

**Interfaces:**
- `RuleBindingScope(tenant_id, optimizer_id, rule_id, bc_id, advertiser_ids, authorization_revision)`.
- `RuleEvidenceSnapshot` stores rule version/definition, optimizer scope, checked metrics, skipped accounts, hits and action ledger references.

- [x] **Step 1: Write failing tests** for effective scope intersection, inclusion of existing/manual ads in assigned accounts, overlap with other optimizers, version fence and retention beyond the current 30-day run cleanup for referenced evidence.
- [x] **Step 2: Implement binding and immutable rule-version references** without duplicating rule definitions; reuse existing rule evaluator and conflict/频控 logic.
- [x] **Step 3: Update rule input expansion/execution** to accept an optimizer scope and revalidate tenant/BC/account authorization immediately before action submission.
- [x] **Step 4: Persist decision-time evidence** including data freshness/publication version, rule definition, checked/hit/skipped objects, before/after values, management task and readback status; preserve referenced evidence when a rule is edited or removed.
- [x] **Step 5: Run automation-rule regression suites and focused evidence tests**; no existing non-AI rule behavior may change.
- [x] **Step 6: Commit** `feat: scope automatic rules to AI optimizer accounts`.

### Task 5: DSH review tasks, optimization mode, proposals and publication

**Files:**
- Create: `backend/app/modules/ai_workbench/reviews.py`
- Create: `backend/app/modules/ai_workbench/proposals.py`
- Create: `backend/app/modules/ai_workbench/ai_tasks.py`
- Create: `backend/app/modules/ai_workbench/knowledge_refs.py`
- Modify: `backend/app/api/main.py`
- Modify: DSH bridge/service integration files after the selected runtime contract is fixed
- Test: `backend/tests/modules/ai_workbench/test_reviews_and_proposals.py`
- Test: `backend/tests/modules/ai_workbench/test_publication_modes.py`

**Interfaces:**
- `POST /tenants/{tenant_id}/ai-optimizers/{optimizer_id}/reviews` creates a review task from a frozen evidence set.
- `GET /.../proposals` returns `PENDING`, `ACCEPTED`, `REJECTED`, `STALE` proposals with before/after, evidence, authorization and base versions.
- `POST /.../proposals/{proposal_id}/accept` and `/reject` are idempotent and require the current optimizer scope.

- [x] **Step 1: Write failing tests** for automatic-mode in-scope publication, automatic-mode out-of-scope blocking, manual-mode forced confirmation, reject preserving current versions, stale-base rejection, and no strategy/rule cross-optimizer publication.
- [x] **Step 2: Implement evidence assembly** from optimizer target versions, plan/occurrence/build records, rule evidence, reporting/business snapshots, operation logs and versioned knowledge references; do not expose credentials or model chain-of-thought.
- [x] **Step 3: Implement the DSH task contract** with tenant/optimizer task identity, short-lived authorization, cancellation/restart fencing, and structured result/proposal output only.
- [x] **Step 4: Implement publication**: strategy proposal updates an optimizer-owned strategy version through its append-version service; a shared strategy reference is rejected until explicitly copied/owned, never silently changed. Rule proposal uses rule version service; automatic mode checks authorization; manual mode always creates a pending proposal; reject only records the decision.
- [x] **Step 5: Run focused publication/review tests and existing business/reporting suites**; verify historical evidence remains readable after source version changes.
- [x] **Step 6: Commit** `feat: add DSH review and optimizer proposal modes`.

### Task 6: TKADA frontend shell, menu and reused configuration entry points

**Files:**
- Modify: `frontend/src/components/Sidebar/AppSidebar.tsx`
- Modify: `frontend/src/components/Sidebar/navigation.ts`
- Create: `frontend/src/features/ai-workbench/AIWorkbenchShell.tsx`
- Create: `frontend/src/features/ai-workbench/AIOptimizerList.tsx`
- Create: `frontend/src/features/ai-workbench/AIOptimizerConfig.tsx`
- Create: `frontend/src/features/ai-workbench/AIOptimizerOverview.tsx`
- Create: `frontend/src/features/ai-workbench/AIOptimizerReviews.tsx`
- Create/Modify: `frontend/src/routes/_layout/tenants.$tenantId.ai-workbench.tsx` and child routes as required by the existing router convention
- Reuse: `frontend/src/features/strategies/StrategyEditor.tsx`, `StrategyForm.tsx`, `frontend/src/features/automation-rules/RuleEditorSheet.tsx`, `frontend/src/features/builds/ManualLinksDialog.tsx`, `dramaGrid.ts`
- Test: `frontend/tests/ai-workbench.spec.ts`
- Modify/Test: `frontend/tests/workspace-shell.spec.ts`

**Interfaces:**
- AI sidebar routes: `overview`, `schedule`, `execution`, `review`, `pending`, plus optimizer selector.
- Existing strategy/rule pages receive `tenantId`, `optimizerId`, `recordId`, `versionId`, and a return context; saving returns to the optimizer record without creating a second editor.

- [x] **Step 1: Add failing Playwright tests** for AI menu placement immediately above “打开体验版”, second AI sidebar, optimizer switching isolation, single strategy binding, schedule grid, pending accept/reject and return-context preservation.
- [x] **Step 2: Add the standalone AI route and two-column shell** while leaving existing TKADA groups unchanged; do not add AI to the “广告投放” group.
- [x] **Step 3: Implement optimizer list/config/overview** with one strategy selector, rule binding, BC/accounts, goals, optimization mode and schedule controls.
- [x] **Step 4: Reuse existing strategy/rule screens** through contextual navigation or extracted content components; show bound-only vs tenant-all scope explicitly and block silent shared edits.
- [x] **Step 5: Connect schedule, execution, review and pending views** to the Task 2–5 APIs; keep material selection out of schedule and show material matching only in execution evidence.
- [x] **Step 6: Run focused Playwright tests at 390/1024/1440 widths and build checks**; verify no horizontal overflow or stale optimizer state.
- [x] **Step 7: Commit** `feat: add AI optimizer workbench UI`.

### Task 7: Retention, daily report and end-to-end acceptance

**Files:**
- Modify: `backend/app/modules/ai_workbench/evidence.py`, `reviews.py`, `api.py`
- Create: `backend/app/modules/ai_workbench/daily_report.py`
- Create/Modify: `frontend/src/features/ai-workbench/DailyReport.tsx`
- Test: `backend/tests/modules/ai_workbench/test_daily_report.py`
- Test: `backend/tests/acceptance/test_ai_workbench_flow.py`
- Test: `frontend/tests/ai-workbench-e2e.spec.ts`
- Modify: `docs/validation/<date>-ai-workbench-v1.md`

**Interfaces:**
- Daily report aggregates the same frozen evidence IDs used by execution and review; it does not recompute a second reporting metric source.

- [x] **Step 1: Write acceptance tests** for two tenants, two optimizers in one tenant, manual and automatic modes, one scheduled drama, order/no-order paths, existing builds readback, account-scoped rules, review proposal and next-version usage.
- [x] **Step 2: Implement evidence retention/cleanup** so referenced decision snapshots survive rule deletion, reporting refresh and normal short-term cleanup; unreferenced data follows configured retention.
- [x] **Step 3: Implement daily report** with target progress, schedule rows, material/order state, build/readback outcomes, rule actions, pending proposals and next trigger.
- [x] **Step 4: Run backend acceptance against isolated PostgreSQL/Redis and frontend E2E using transport doubles**; keep real external credentials and writes disabled.
- [x] **Step 5: Record limitations**: no automatic selection, no automatic copy scaling, no real provider/material order proof, no production deployment until authorized integrations are separately validated.
- [x] **Step 6: Commit** `test: accept AI optimizer workbench v1`.

## Verification Commands

Run from `projects/tiktok-ad-automation/` after each task as applicable:

```bash
uv run pytest backend/tests/modules/ai_workbench backend/tests/modules/automation_rules backend/tests/modules/builds -q
bun run --filter frontend build
bunx playwright test frontend/tests/ai-workbench.spec.ts frontend/tests/ai-workbench-e2e.spec.ts --project=workspace --workers=1 --reporter=line
git diff --check
```

The final acceptance must separately report deterministic tests, transport-double integration, DSH protocol checks, and any real tenant/provider/material/TikTok validation. Passing local tests does not prove live advertising success.
