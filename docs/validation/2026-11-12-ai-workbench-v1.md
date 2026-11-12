# AI 投放工作台 v1 交付验收 — 2026-11-12

本轮基线 `612d0b43`，交付代码提交 `e33764c3`，工作分支 `codex/ai-ad-workbench-v1`。七项计划的代码和本地验收补完；迁移 head 为 `0126_ai_plan_retry_fairness`。尚未推送、合并到主工作区或部署，不把本地验收视为真实投放成功。

## 本轮交付

- 正式页面复用现有认证客户端与 API origin；补齐优化师创建、编辑、暂停、恢复、BC/账户、单策略/规则绑定、时区、复盘时间、素材配置和 AI 字段/数值授权。
- 每日剧单保留稳定行 ID、修订与只读边界；到期才冻结，异步准备先等待再恢复，未知不重建，执行前复验暂停/到期/当天截止。
- 有界调度排除终态和已投递任务；持久重试水位与错误原因保证等待/撤权行让出扫描窗口。解绑规则后恢复普通调度，保留非 AI 规则行为。
- 素材订单冻结请求/提交时间，使用租户公司 Key、同键同体重试、原 job 查询及逐剧交付证据；缺配置只保留意图，日报不把勾选当成已下单。
- 独立 DSH 原生插件、服务领取、短期任务 JWT、心跳、取消/恢复 fence、结构化提案、AUTO/MANUAL 发布与审计。修正旧复盘导入不存在报表模型的问题；冻结当前报表行指标，经营证据严格按可证明的账户范围筛选。
- 提案展示前后值、依据、授权和版本，人工逐条采纳/拒绝；自动越界保留待确认，具体人工确认不扩大长期自动授权。

## 实际执行的验证

| 验证 | 结果与边界 |
| --- | --- |
| 后端 AI 全部、自动规则全部、AI 接受测试及搭建交接专项 | **422 passed，2 warnings**；全新独立 PostgreSQL 17 数据库，无真实外部写入 |
| PostgreSQL 并发领取 | 上述 422 项内包含两个重叠事务：首事务持行锁，另一事务 SKIP LOCKED，最终一个发生记录和一条 outbox；不使用数据库替身 |
| 前端 Playwright | **22 passed**，覆盖 Bearer、创建/编辑/暂停/恢复、提案、切换隔离、冻结网格/TSV/撤销、390/1024/1440 视口；外部/业务响应模拟 |
| 本地真实浏览器 + FastAPI + PostgreSQL | 无业务接口模拟，完成优化师创建、暂停/恢复、剧单保存及刷新读回稳定 ID/revision；账户及授权事实均为隔离测试种子 |
| DSH 原生运行接口适配测试 | **2 passed**，验证任务专属工具、目录、token 分离和 HTTPS/重定向约束；宿主/模型使用确定性替身，不宣称真实模型运行 |
| 数据库迁移 | 全新库升至 head；`0126 → 0123 → 0126` 成功；所有 AI 表的 SQLModel/Alembic 比对通过 |
| 静态与构建 | Ruff、Python 编译、TypeScript、正式 Vite 和体验版 Vite、差异检查通过 |
| 独立代码复审 | `gpt-6-astra/high` 只读审查提出 3 个 P2；新增用例先 3 failed 后通过；定向复审全部 ADDRESSED，无新增重要问题 |

后端命令在 `backend/` 执行，使用本工作树 `.venv`，并显式指向独立的 `_test` 数据库：

```bash
PYTHONPATH="$PWD" ../.venv/bin/python -m pytest tests/modules/automation_rules tests/modules/ai_workbench tests/acceptance/test_ai_workbench_flow.py tests/modules/builds/test_ai_workbench_handoff.py -q --tb=short --disable-warnings
```

前端使用已有依赖树的 TypeScript/Vite/Playwright Node 入口执行；本机没有 Bun，下载失败，因此没有宣称执行过 Bun。DSH 适配测试为 `node --test integrations/dsh-ai-workbench/runtime.test.mjs`。

原 Docker PostgreSQL 因 Docker 磁盘耗尽反复恢复失败，本轮改用独立本机 PostgreSQL，未清理用户既有容器/卷。整体 `alembic check` 仍会报告原分支旧素材 `ad_material_reference.creative_ids` 及约束的既有差异；本轮没有篡改该独立退役任务，AI 范围比对已独立通过。此前大范围 builds 回归的历史失败/错误未在本轮冒报为全通过，当前只报告上述实际完成的选择集。

## 尚未执行的外部操作

未配置或调用真实 DSH 模型、真实公司素材下单、版权方写入、TikTok 创建/开启，也未部署生产。部署需要落实三份新增迁移、运行凭据和 DSH 模型/插件，以及目标租户素材绑定；参见[运行手册](../runbooks/ai-ad-workbench.md)。自动选剧、复制放量、多方案和 A/B 明确不属于 v1。

## 初次验收记录（历史，以下不代表本轮结果）

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
