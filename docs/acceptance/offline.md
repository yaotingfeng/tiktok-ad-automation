# 离线验收记录

## 当前记录

2026-09-09，集成提交 `7dddbe4`。环境为 macOS、Python 3.14.6、PostgreSQL 17.5、Redis 8。数据库测试使用独立 `*_test` 数据库；需要跨事务可见的测试创建并清理自己拥有的临时数据库。Redis 测试使用与业务不同的非零数据库，仅清理所属 key。

| 证据 | 实际结果 | 范围 |
| --- | --- | --- |
| `uv run pytest -q --tb=short` | 1,059 passed，136.11 秒 | 当时完整后端 |
| `test_execution_tasks.py` + `test_execution_dispatch.py` | 10 passed，4.74 秒 | 队列、创建、回读、消息恢复 |
| P05 UI 工作区 Playwright | 210 passed | 含 46 条搭建和提交场景；P06 完整详情尚未计入 |
| 两份 Compose `config --quiet` | 通过 | 使用合成配置；未启动容器 |

完整执行链路测试使用真实策略、草稿准备、冻结预览、提交、展开、数据库和 outbox。官方 SDK 的远端传输被替身接管；账户 Scene 在这组执行专项中为固定已核实 fixture。因此该组证据不覆盖真实授权、Scene 自动获取或版权方线上接口。

## 执行与故障证据

`backend/tests/modules/builds/test_execution_tasks.py` 从持久 outbox 逐个推进依赖。素材本地映射核实后创建 CTA、Campaign、Ad Group、两条不同文案的 SP，然后通过官方 GET 接口回读。正常流程共有 5 次 create（包含 CTA），三层广告均为 ENABLE。

远端 Campaign 已创建但响应丢失时，记录 UNKNOWN；模拟原租约确已过期后，真实恢复服务通过 GET 找回 ID，再继续其余广告。创建次数仍是 5 次。查询返回空时只发生 CTA 与 Campaign 两次 create，Campaign 保持 UNKNOWN，任务为 NEEDS_REVIEW；不会循环核查或发出第二次 create。

`test_review_execution.py` 使用真实 PostgreSQL deferred constraint trigger，在远端 CTA 已返回后使首次本地 receipt commit 失败；新的事务保存 `LATE_CREATED` 实际 ID，后续 portfolio GET 恢复。这与 SDK 抛出异常但远端状态未知的场景分开验证。

消息恢复专项覆盖已提交成功 receipt 后、唤醒下游之前进程退出。相同 delivery ID/revision 再次送达只执行持久续调；已成功回读的源步骤保持 READ 投递模式，不能误改为 create。结果确认和 unit 唤醒在同一数据库事务提交。

## 尚在进行的本地验收

共享 Scene 自动准备、两版权方全流程验收、恢复入口、完整任务 UI、100,000 目录和大批计划容量仍在继续。本文件只记录已有运行证据；后续完成后补充精确提交与指标。

Linux prefork 硬截止测试位于 `test_prefork_deadline.py`：官方 SDK 连接本地停滞 HTTP 服务，由实际 Celery prefork 硬截止终止子进程，再验证 UNKNOWN、保留请求和无重复 POST。macOS 跳过不算通过，Linux CI 结果单独补录。

真实 TikTok、版权方、S3、OAuth 与试投状态见 [真实联调记录](live-sdk.md)。
