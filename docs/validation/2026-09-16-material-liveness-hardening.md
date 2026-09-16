# 素材与搭建流程可恢复性复审

日期：2026-09-16。针对用户要求的“整体复审、减少卡死与无法继续的流程”，检查租户选材、上传与转存、同 BC 分发、封面、冻结预览、提交恢复和后台队列。范围是本地代码与发布候选验证；没有推送、服务器部署或真实平台写入。

后续已完成新加坡部署及 Linux 专属补验，见[发布记录](2026-09-16-staging-tenant-library.md)；以下保留本地收口时的验证边界。

## 修复结果

| 可复现问题 | 修复与边界 |
| --- | --- |
| 首次跨 BC 转存明确失败后，后续新准备一直复用失败 seed | 增加准备代数；仅明确未生效、无在途 claim、无成功 VID 的终态允许新准备。沿用原主账户，保留旧任务与消费者；UNKNOWN 不换代 |
| 目标权限或绑定在发送前变化，只阻断分发却留下 pending 操作 | 在原素材→操作锁序下，终止当前、明确未发送的操作并记录未生效证据；已 armed、在途或有上传尝试的操作不改写 |
| 目录按文件名选中不可用代表，隐藏实际可用别名；旧名称无法用于手动选材 | 去重优先真实可用副本，其次原件；自动与手动选材共享租户内可信内容判断。SQL 别名的摘要、大小、ID 均引用同一来源，防止错误合并 |
| 同主账户别名的延迟等待者，将过期副本直接标为 ready | 复用已有 existing_target 只读核实和队列；使用真实 VID，不重新上传 |
| 已成功封面因 BC 默认连接变化无法恢复原搭建 | 按原预览冻结路由校验权限、绑定代数及账户；更改默认连接不影响原任务，真实撤权和重绑仍拒绝 |
| 延迟恢复遇到过期封面，只显示过期但不继续核实 | 对同一个封面任务安排一次只读核实，状态变为 VERIFYING 后退出重复候选；更新证据后继续原搭建，不新增上传 |

关键生产改动集中在 `materials/bc_seeding.py`、`distribution.py`、`repository.py`、`content_identity.py`、`seed_models.py`，以及 `builds/drafts.py`、`cover_execution.py`。选材删除重复可用性判断，恢复复用既有执行器、outbox 和封面核实服务，没有增加另一套重试调度器。设计同步至 [统一素材库设计](../superpowers/specs/2026-09-16-tenant-material-library-design.md)。

## 数据与发布约束

新增 Alembic head `material_seed_generations`，前置为 `material_target_scope`。已有 seed 仅补 generation=1，原分发、消费者和上传身份不变；唯一键增加代数，正数约束保持模型一致。真实历史行升级、无后续代数时往返降级、已有第二代时拒绝丢弃历史的降级均有回归。

发布时前后端、API、所有 Worker 与 Beat 必须使用同一版本。遵循现有部署手册排空、完整备份及独立恢复演练后迁移；不能直接回退到不理解新代数的版本。此次没有新增或改变功能开关。

## 本地验证证据

测试使用独立 PostgreSQL 数据库和 Redis 测试库，平台替身仅位于 HTTP 传输边界。以下分组有重叠，不累计成“全仓通过总数”。

| 分组 | 结果 |
| --- | --- |
| 最终 API/MCP 完整链路、失败 seed、数据库锁并发与租户封面 | 35 passed，79.66 秒（最终类型收口后复验） |
| 最终批量共享、迁移、小程序选择与恢复候选组合 | 71 passed，129.74 秒（最终类型收口后复验） |
| 内容键、租户目录、自动/手动选材与草稿关联 | 37 passed，8.54 秒 |
| 封面冻结路由及过期恢复、搭建接口、历史迁移与场景恢复 | 43 passed，46.16 秒 |
| 租户素材库及准备代数历史迁移 | 8 passed，3.29 秒 |
| 历史命名迁移、恢复候选与通用查询计划 | 分别 1 passed / 0.62 秒、13 passed / 8.09 秒 |
| 1,000 原始记录、500 种重复内容、5 页完整目录 | 1 passed；本地 5 页查询 0.912 秒，无漏项，全部选中可用代表 |
| 前端素材、R2 导入、上传基础、搭建准备/预览/详情/通道七组 | 首轮 232 passed / 1 failed；修正旧 BC 查询参数断言后详情组 43 passed，对应 233 个场景均已有通过结果 |
| TypeScript/Vite 构建、变更前端测试 Biome | 通过 |
| CI 后端全量 `ruff check app tests` / `ty check app` | 通过，包含警告视为错误 |
| 24 个 Python 文件 Ruff/格式、11 个生产模块 mypy | 通过 |
| 专用测试库 Alembic upgrade head / check | 通过，无模型差异 |

目录容量数字只反映本机合成 PostgreSQL 查询，不代表生产容量或 TikTok 吞吐。新增种子恢复、目录代表、SQL 别名、历史迁移及封面过期场景均先复现失败，再验证修复。

扩展搭建/队列首次运行在 214 passed / 5 failed 后停止。五个失败来自旧测试契约：已确认权限仍按 24 小时过期、素材响应缺新字段、旧数据库 schema 用当前 ORM 播种。已按既有设计纠正测试，43 项组合复验通过；没有增加生产兼容分支或放宽权限/未知结果检查。剩余组另发现历史命名夹具混入后续列、通用恢复计划仍传旧 cutoff 两项测试问题：按历史 schema 播种/回读，取消过时参数，保留冻结不可变、候选集合及索引断言；分别 1 项、13 项复验通过。剩余 54 个搭建文件及 jobs 共 512 项，最终为 506 passed / 2 failed / 4 skipped（819.88 秒）；两个失败均为上述已纠正并复验通过的测试，4 项为 Linux 专属。与首次已执行部分和新增恢复用例组成当前搭建/队列 733 项收集范围，分段执行后 729 项有通过结果、4 项待 Linux 验证；没有把失败首轮标为全绿。

CI 全量类型检查另发现 25 条诊断（包含按 CI 设置视为错误的警告），已收口为零：别名复制拆为同值的字典更新，账户动作标注为已有能力枚举，SQL 原语使用 SQLAlchemy 会话类型，批次路由补类型声明。既有 `tenant_material_library` 迁移仅补约束名的类型声明，revision、SQL 与数据语义不变；未重新定义历史迁移。没有关闭类型检查或添加忽略规则。

独立复审覆盖锁序、冻结授权、迟到等待者、UNKNOWN、迁移和任务续跑。发现的 SQL 别名引用问题已补负向回归修复；最后封面恢复增量复审未发现新的确定性 P1/P2。

## 复现与实际环境边界

先按 `config/README.md` 定位本地配置；从 `backend/` 执行，设置专用 `DATABASE_URL`、`TEST_REDIS_URL`、`OBJECT_STORAGE_PROVIDER=s3`、`S3_REGION=us-east-1`。不得连接业务库跑测试。

```bash
uv run --frozen pytest tests/modules/materials/test_bc_seed_recovery.py tests/modules/materials/test_bc_seeding_concurrency.py tests/modules/materials/test_tenant_library_covers.py tests/integrations/tiktok/test_dual_channel_flow.py -q
uv run --frozen pytest tests/modules/builds/test_tenant_library_builds.py tests/modules/builds/test_cover_recovery_routes.py tests/modules/materials/test_tenant_library_migration.py tests/modules/materials/test_tenant_library_capacity.py -q
uv run --frozen pytest tests/modules/builds tests/jobs -q -ra
uv run --frozen alembic upgrade head
uv run --frozen alembic check

# frontend/
bunx playwright test --project=workspace tests/materials.spec.ts tests/r2-ingest.spec.ts tests/upload-foundation.spec.ts tests/build-preparation.spec.ts tests/build-preview.spec.ts tests/build-task-pages.spec.ts tests/build-channels.spec.ts
bun run build
```

现有 `.github/workflows/ci.yml` 的 Ubuntu 后端模块组覆盖这些 Linux 专属测试，本轮未推送，未将未运行的 CI 计为通过。当前本机是 macOS，没有 Docker；Linux 专属的真实 prefork 硬超时、杀进程后迟到结果核查和队列隔离仍需在发布目标或独立 Linux 验证环境执行，线程消费者结果不能替代它。服务器版本、实际配置和真实 TikTok 链路也须在明确目标及具体业务授权后验收。本地检查不等于已上线或实际平台联调完成。
