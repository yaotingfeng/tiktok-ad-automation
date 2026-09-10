# Repository Guidelines

## 仓库与 Git 操作
- `tiktok-ad-automation` 是独立仓库，代码、测试和运行文档必须在本仓库提交。保留 `origin` 为 `git@github.com:yaotingfeng/tiktok-ad-automation.git`。
- 每轮代码修改完成并验证后，做一次聚焦的提交；使用明确文件路径，只暂存本轮改动，提交前检查暂存差异。
- 合并、清理、提交、推送前先执行 `git status -sb`；提交或推送前还须执行 `git rev-parse --show-toplevel`，确认仓库根目录。
- 禁止自动创建新分支，也不能直接把当前工作区切到临时分支开发。需要隔离时只能使用绑定已有分支的独立工作树，并先说明路径、基准和合并计划。
- 已完成并验证的功能分支应尽快合并回 `main`，优先快进合并；合并前确认整个分支的改动范围及验证状态。
- 只有用户明确要求时才推送，默认将本地 `main` 推送到 `origin main`，禁止强制推送。禁止自动创建或更新合并请求。
- 禁止使用 `git add .`；禁止用 `git reset --hard` 或 `git checkout --` 处理无关改动。保留其他任务及用户的未提交改动。
- 对外汇报优先使用“提交、推送、分支、合并请求”等中文术语。

## Execution contract
- Follow docs/superpowers/specs and stage plans. Current approved visual design is the strategy-page refinement extended to all pages with the original neutral black primary button, per docs/superpowers/plans/2026-09-10-workspace-visual-rollout.md. It supersedes the dashboard-01 header-title/uncontained-table layout and the pilot's blue primary color.
- All subagents use gpt-6-astra with high reasoning. Do not interrupt a subagent merely because it takes a long time. Root coordinates integration, migrations and shared contracts.
- User preferences override optional Superpowers ceremony; use skills proportionately. Independent workers may run in parallel in isolated worktrees with explicit file ownership. Do not spawn child agents from implementation workers.
- Preserve tenant and BC scope in every data path. Use the official TikTok Python SDK; no MCP or custom HTTP TikTok gateway. No real ad, authorization or provider writes during development without a concrete authorized operation.
- Product behavior: Campaign, Ad Group and Ad create directly ENABLE after the user submits the concrete preview. No second activation workflow. All dramas target all pasted accounts; no rotation. Upload does not bind drama. Source advertiser IDs are actual recorded upload locations.
- Never commit credentials, runtime env files, sessions, local databases, real reports, or uploaded videos. Demo and live integration evidence must remain distinct.
- Run relevant behavioral tests and build checks before claiming task completion. Use real PostgreSQL and Redis for locking/concurrency tests; external service doubles belong at the transport boundary.
- Record completed tasks, tests, decisions and commits in docs/implementation-progress.md so another session can continue without repeating work.

## Tooling
Python uses uv in backend; frontend uses Bun and the official template lockfile. Keep shadcn components and TanStack routing. Check actual package scripts before running them. Use four-space Python and existing two-space TS/TSX formatting.

## 代码与设计要求
- 写代码时添加必要的中文注释，尤其是业务逻辑：说明业务规则、关键流程以及设计原因；逻辑调整时同步更新注释。
- 相同功能优先复用并封装现有方法，避免多个方法重复实现同一操作。及时清理本轮改动涉及的废弃代码、无效分支和过时引用，控制维护成本。
- 业务逻辑设计、修改和调整不保留兼容旧行为或回退旧逻辑的代码。修改前评估调用方、数据、接口、后台任务与测试的影响范围；影响较大且调整方案不明确时，先向用户确认。
- 前端页面必须使用 `shadcn/ui`，遵循已批准的统一视觉规范及现有组件约定。
- 新功能或重构范围过大时，将设计文档和实施计划拆分为多个职责清晰、可独立验证的阶段，明确依赖关系与验收标准。

## 环境与发布
- 明确区分本地开发和生产环境，不能将本地验证视为生产验证。
- 发布必须遵循 `docs/runbooks/deployment.md`；首次部署同时参考 `docs/runbooks/bootstrap-deployment.md`。
- 修改服务器、数据库或自动化开关前，先阅读目标环境说明；涉及生产时必须先读生产环境说明，确认目标、影响范围和授权。缺少环境说明时先补齐信息，再执行变更。
