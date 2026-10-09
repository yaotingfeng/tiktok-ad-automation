# 2026-10-09 本地验收修复

## 范围与环境

接续公司容器化部署改造后的本地验收。基线为 `f2eed0c`；此前的 TCR/Kubernetes 改造及本地部署修正已位于公司 Git `main`。本轮不变更数据库类型、部署拓扑、对象存储配置或回调地址。

使用本机 PostgreSQL 和 Redis DB 2 执行事务、锁、队列和恢复测试；TikTok、版权方和对象存储只在传输边界使用合成响应。测试不构成真实外部平台联调或生产发布验收。

最初复用了上一会话的专用测试库，发现中断留下的 `original_use` 引用阻止 outbox 测试删除自己的调度数据。保留原库，随后创建独立的 `tiktok_regression_resume_20261009_test` 重跑；验收使用独立测试库及逐用例新建的 `strategy_*_test` 库，未清理开发或远程数据库。

## 修复内容

- 容量脚本补齐合成 BC 连接、绑定和默认路由，继续经过真实准入与作用域核验。
- R2 验收补齐 MID、文件名、分页证据，按实际调用区分已有来源 MID 与上传丢回执后的重新核验，避免顺序响应错位。
- 素材目标流程使用 VIDEO 原生共享；封面通过真实来源成功事件、SOURCE 上传、IMAGE 共享和目标回读完成。断言一份视频原件只上传一次，视频与封面各共享一次，且消费完全部模拟响应。
- 千文件会话汇总仍不做聚合扫描；分页接口允许一次限定会话的 COUNT 以返回准确 `total`，继续验证千个身份完整、不重复及查询次数上限。
- 批量策略改用当前显式结构：每组最多十份素材、一个基础广告、两份创意，保持原来的 6 个系列 / 18 个组 / 36 条广告。预览允许只读预算能力查询，仍禁止广告写入。
- 批量合成传输补齐 VIDEO/IMAGE 共享、来源视频库存、MID 搜索和历史来源操作的冻结路由；目标映射仍由正式 Worker 生成。大批量等待预算调整为 360 秒，保留正式调度和恢复间隔。
- 双通道联调补齐第六项场景资源（预算读取），显式限定单基础广告，避免测试只执行一个广告却遗漏另一个；历史迁移夹具仅使用目标历史版本已有字段。
- 修复手动链接实际准备死循环：Minis 目录任务不要求完整场景的预算证据；已完成目录先解析链接，再创建有明确 Minis 的完整场景。API/MCP 两通道各补一项回归，验证目录保留 COMPLETE、只请求一次目录并复用后续场景任务。
- 修复实际预算校验：非有限 Decimal 在比较前转为字段验证错误；系列预算要求大于零，广告组预算模式继续允许系列预算零值。覆盖 NaN、sNaN、正负 Infinity。

## 验证

验收目录共 55 项用例，按 R2（11）、其他验收（35）与隔离恢复（9）分组执行；失败项修复后单项复测，全部已有通过结果。以下统计包含重叠复测，不相加为全仓通过数量。

- R2 管线、千文件故障恢复、容量及夹具清理：`11 passed`。
- 广告请求合同（含广告组预算模式的空值/零值）：`32 passed`。
- API/MCP × 同 BC/跨 BC 的完整离线链路：`4 passed`。
- 两项历史链路迁移：`2 passed`。
- 共享场景/草稿/素材夹具回归首轮 `114 passed, 4 failed`；修复旧表读取后，相关迁移文件复测 `5 passed`（包含原 4 项失败）。
- Minis 地区未核实阻断冻结的用例复测：`1 passed`。原用例按旧排除账户规则预期继续冻结，现按已批准的共同地区规则核验整批阻断。
- 修改的 18 个 Python 文件 Ruff 格式、lint、compileall 通过；`git diff --check` 通过。
- 批量/手动链接/R2 工具/运行诊断/报表验收首轮：`33 passed, 2 failed`。两项失败分别为旧 Minis 冻结断言和手动链接目录预算证据死循环；两项均已单项复测通过，手动链接还补齐了当前命名要求的数字剧目编号，并保留仅原件输入的上传验收。
- API/MCP 目录解析回归：`2 passed`。修复目录循环后的场景与小程序选择回归：`82 passed`。
- 手动链接完整准备、冻结、创建及 URL 逐值保留：`1 passed`（77.17 秒，仅原件输入）。
- 跨租户隔离、丢回执恢复、歧义阻断、重复投递、授权撤销、晚到回执租约保护、平台代理：`9 passed`（1658.97 秒，保留正式调度和恢复等待间隔）。

### 复现入口

从 `backend/` 使用 `uv run pytest`，先显式设置本机专用 `DATABASE_URL`（库名以 `_test` 结尾）、应用所需合成环境值、`REDIS_URL` 与独立 `TEST_REDIS_URL`，不得使用运行环境数据库。

```bash
uv run pytest -q tests/acceptance/test_r2_pipeline.py tests/acceptance/test_r2_capacity.py
uv run pytest -q tests/acceptance --ignore=tests/acceptance/test_r2_pipeline.py --ignore=tests/acceptance/test_r2_capacity.py --ignore=tests/acceptance/test_isolation_recovery.py
uv run pytest -q tests/acceptance/test_isolation_recovery.py
uv run pytest -q tests/contracts/test_tiktok_build_contract.py
uv run pytest -q tests/integrations/tiktok/test_dual_channel_flow.py tests/integrations/tiktok/test_dual_channel_migration.py
uv run pytest -q tests/modules/builds/scene tests/modules/builds/test_mini_selection.py
```

## 边界

全仓回归另发现账户授权、目录同步及历史测试夹具失败，不能把本轮验收修复等同于全仓已通过，也不能据此宣布可以发布生产。扩展扫描在约 39% 主动停止，除已独立修复的 7 项外，尚有 50 项 call 失败和 5 项 teardown 错误报告待复核。完整清单及统计边界见 [扩展回归待处理项](2026-10-09-regression-followups.md)。
