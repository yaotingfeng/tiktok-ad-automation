# TikTok Ad Automation

## Execution contract
- Follow docs/superpowers/specs and stage plans. Current approved visual reference: official dashboard-01, scoped by docs/superpowers/plans/2026-09-10-shadcn-official-alignment.md. It supersedes the original 2026-09-08 prototype for layout, typography and theme.
- The user requested git commits and implementation in this repository. Keep origin git@github.com:yaotingfeng/tiktok-ad-automation.git. Work on feature branches/worktrees, commit reviewed task changes, push feature branches without force.
- All subagents use gpt-6-astra with high reasoning. Do not interrupt a subagent merely because it takes a long time. Root coordinates integration, migrations and shared contracts.
- User preferences override optional Superpowers ceremony; use skills proportionately. Independent workers may run in parallel in isolated worktrees with explicit file ownership. Do not spawn child agents from implementation workers.
- Preserve tenant and BC scope in every data path. Use the official TikTok Python SDK; no MCP or custom HTTP TikTok gateway. No real ad, authorization or provider writes during development without a concrete authorized operation.
- Product behavior: Campaign, Ad Group and Ad create directly ENABLE after the user submits the concrete preview. No second activation workflow. All dramas target all pasted accounts; no rotation. Upload does not bind drama. Source advertiser IDs are actual recorded upload locations.
- Never commit credentials, runtime env files, sessions, local databases, real reports, or uploaded videos. Demo and live integration evidence must remain distinct.
- Run relevant behavioral tests and build checks before claiming task completion. Use real PostgreSQL and Redis for locking/concurrency tests; external service doubles belong at the transport boundary.
- Record completed tasks, tests, decisions and commits in docs/implementation-progress.md so another session can continue without repeating work.

## Tooling
Python uses uv in backend; frontend uses Bun and the official template lockfile. Keep shadcn components and TanStack routing. Check actual package scripts before running them. Use four-space Python and existing two-space TS/TSX formatting.
