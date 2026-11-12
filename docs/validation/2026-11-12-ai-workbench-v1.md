# AI workbench v1 validation — 2026-11-12

Task 7 adds evidence retention, a tenant/optimizer scoped daily report and
transport-double acceptance coverage.

Deterministic checks cover protected evidence cleanup, retention cutoffs,
frozen-evidence IDs, daily-report route scope and explicit AUTO/MANUAL proposal
paths. The frontend E2E contract uses mocked optimizer/report responses; no
external provider credentials or advertising writes are enabled.

The acceptance boundary does not prove automatic drama selection, automatic
copy scaling, real provider/material order completion or production deployment.
Those require separately authorized integrations and live validation.

## 2026-11-12 实测结果（隔离 PostgreSQL 17 / Redis 7，无外部凭据）

此前各任务的复审没有实际运行数据库测试和前端测试，本轮第一次在真实数据库和浏览器里跑通：

- 后端 `tests/modules/ai_workbench`、`tests/acceptance/test_ai_workbench_flow.py`：全部通过。
- 后端回归 `tests/modules/automation_rules`、`tests/modules/builds`：与 main 同环境对比，
  分支没有新增失败。两边共有 26 个失败、225 个错误（如“请先更新可用投放身份”），
  main 上同样存在，不属于本分支。
- 前端：`tsc` 与 `vite build` 通过；Playwright `ai-workbench.spec.ts`、
  `ai-workbench-e2e.spec.ts` 共 13 个用例通过（登录、租户和接口全部是 transport double）。

本轮修复：DSH 复盘迁移的版本号超过 `alembic_version` 32 字符上限（改为 `0123_ai_dsh_reviews`），
否则任何环境都无法升级；修正测试种子插入顺序、留存清理用例、路由搜索参数类型、
E2E 未登录和未纳入 Playwright 项目、旧迁移测试写入新列等问题。

仍未验证：真实版权方剧目查询、素材中台真实下单、真实 TikTok 搭建写入、DSH 真实复盘任务，
以及生产部署。这些需要单独授权后实测。
