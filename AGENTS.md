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
- Preserve tenant and BC scope in every data path. TikTok API calls use the official Python SDK. The user has approved adding the official TikTok MCP as a second backend-code channel, with tenant-admin authorization and explicit BC binding; follow the approved docs/superpowers/specs/2026-09-11-tiktok-dual-channel-mcp-design.md and docs/superpowers/plans/2026-09-11-tiktok-mcp-implementation.md (implementation in progress; resume from docs/implementation-progress.md). No custom HTTP TikTok business gateway. No real ad, authorization or provider writes during development without a concrete authorized operation.
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
- 每次向测试或生产环境发版（含仅前端、配置或依赖变更，无数据库迁移也适用），必须在变更前备份数据库、Redis 持久状态、当前项目文件及构建产物、私有配置和证书。项目备份必须为独立归档，不能仅以 Git、旧 release 目录或 current 指针代替；具体清单和验证要求见部署手册。首次部署无历史数据时明确记录不适用项。
- 发布前核对目标域名/端口、服务用户与目录、数据库/Redis、加密密钥、对象存储、API/MCP 各自授权配置及功能开关；不得输出私密配置。备份须有版本、迁移 head、校验和及恢复验证证据；备份不完整或验证失败不得切换版本。不得把基础健康、MCP READY 或模拟测试报告为真实业务联调成功。
- 修改服务器、数据库或自动化开关前，先阅读目标环境说明；涉及生产时必须先读生产环境说明，确认目标、影响范围和授权。缺少环境说明时先补齐信息，再执行变更。
- 骏伯生产环境必须先读 `docs/runbooks/production-junbo.md`，按其中固定版本、独立端口、服务排空、备份、Alembic 迁移、验收和回滚流程发布；禁止对同机其他项目执行停机、覆盖配置或数据操作。
- 生产入口为 `https://manjuad.gzjunbo.net:8000`，独立 Compose 项目 `tt-ada-production`，版本目录 `/opt/tt-ada/releases/<Git SHA>`。必须使用已发布版本的 `deploy/production-compose.sh`，不混用本地或 staging 配置。
- 数据库变更必须通过审查后的 Alembic 迁移；迁移前冻结写入、正常排空在执行任务并完成可恢复备份。禁止直接修改生产表结构、改写历史迁移、清空队列/持久卷或未经兼容性核实直接回退数据库。所有环境凭据、备份、会话均不进 Git。
- 首发基础验收与真实 TikTok/R2/版权方联调分别记录；用户尚未配置的外部集成不得通过演示凭据或开启自动化绕过。
- 发布验收记录位于 `docs/validation/2026-09-10-production-release.md`，后续每次发版新增对应日期记录并更新实施进度。迁移期间暂停备份 timer，等待已启动的备份自然结束；发布或中止恢复处理结束后恢复 timer。Redis RDB 恢复先在新实例/卷验证，禁止覆盖仍带旧 AOF 的生产卷。
- 当前产品显示名称为 `TK-ADA`，左上角品牌18px、副标题11px“广告投放工具”，登录按钮仅“登录”。最近品牌发布记录见 `docs/validation/2026-09-10-tk-ada-brand-release.md`；技术部署标识和持久卷继续沿用 `tt-ada`，不可随显示名称重命名。
