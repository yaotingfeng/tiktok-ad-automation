# 广告创建排期与状态实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在投放策略和广告搭建页面支持 UTC 排期与创建状态 ENABLE/DISABLE，并将配置安全地冻结、发送到 TikTok Smart+ 创建接口并回读验证。

**Architecture:** 排期与初始状态属于 StrategyConfig，随策略版本冻结到预览；创建编译器只发送 TikTok 支持的字段。页面直接填写 UTC，后端严格校验开始/结束时间和层级能力，Campaign/Ad Group/Ad 共用创建状态，排期发送到 Ad Group。

**Tech Stack:** FastAPI/SQLModel/Pydantic、React/TypeScript、TanStack Query、TikTok 官方 SDK 与 MCP。

**Spec:** 本计划由用户 2026-10-05 确认：页面直接使用 UTC；策略可选创建状态启用/停用；可配置立即开始或指定开始/结束时间。

## Global Constraints

- 保留 tenant 与 BC 隔离，禁止真实 TikTok 写入联调。
- 不提交凭据、会话、真实投放数据。
- 现有旧策略默认行为保持为 ENABLE + 立即开始。
- 结束时间必须晚于开始时间；只允许 UTC `YYYY-MM-DD HH:MM:SS`。

## Review Focus

- 旧策略缺少新字段时仍能读取并按旧默认值运行；由策略 schema 回归测试覆盖。
- DISABLE 状态必须同时作用于 Campaign、Ad Group、Ad；由 request compiler 测试覆盖。
- START_END 缺结束时间、结束早于开始、格式非法均阻断；由 schema 测试覆盖。
- FROM_NOW 不发送结束时间；由编译器测试覆盖。
- 页面展示 UTC 标签并提交新字段；由现有策略表单测试覆盖。

### Task 1: 策略与 TikTok 合同

**Files:**
- Modify: `backend/app/modules/strategies/schemas.py`
- Modify: `backend/app/integrations/tiktok/contracts/builds.py`
- Test: `backend/tests/modules/strategies/test_schemas.py` and existing build contract tests

- [ ] Add `creation_status`, `schedule_type`, `schedule_start_time`, `schedule_end_time` with backward-compatible defaults and validation.
- [ ] Allow ENABLE/DISABLE and SCHEDULE_FROM_NOW/SCHEDULE_START_END in creation contracts; keep schedule fields on Ad Group.
- [ ] Add tests for defaults, invalid ranges and both statuses.

### Task 2: 预览冻结与创建编译

**Files:**
- Modify: `backend/app/modules/builds/execution.py`
- Modify: `backend/app/modules/builds/request_compiler.py`
- Modify: `backend/app/modules/builds/preview_schemas.py` or related frozen metadata DTOs
- Test: `backend/tests/modules/builds/`

- [ ] Copy schedule/status from frozen strategy config into execution snapshots.
- [ ] Remove hardcoded ENABLE and current-time schedule; compile the configured values.
- [ ] Ensure campaign/ad/adgroup status is consistent and schedule fields are emitted only for Ad Group.
- [ ] Add compiler/execution tests without network calls.

### Task 3: 策略编辑页面

**Files:**
- Modify: `frontend/src/features/strategies/StrategyForm.tsx`
- Modify: `frontend/src/features/strategies/validation.ts`
- Modify: `frontend/src/features/strategies/StrategyList.tsx` and `StrategyVersionList.tsx`
- Test: `frontend/tests/strategies*.spec.ts`

- [ ] Add UTC-labeled status and schedule controls.
- [ ] Validate required start/end fields and serialize empty optional values as null.
- [ ] Show status and schedule in strategy summaries/lists.

### Task 4: API/client and verification

**Files:**
- Regenerate or update `frontend/src/client` types and SDK from the backend OpenAPI contract.
- Modify: `docs/implementation-progress.md`

- [ ] Run backend focused tests, frontend typecheck/build and relevant Playwright tests.
- [ ] Run repository regression commands required by AGENTS.md.
- [ ] Record decisions, tests and commit in implementation progress.
