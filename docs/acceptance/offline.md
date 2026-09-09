# 离线验收记录

## 最新集成验证

本地集成 `3e72e94` 的模块回归：`PYTHONPATH=. uv run pytest --ignore=tests/acceptance -q --tb=short --show-capture=no`，**1,317 passed、1 skipped，731.72 秒**。唯一跳过项是在 Linux 上运行的真实 prefork 截止测试；同一提交的 [Linux 模块 CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34316838207/job/102354717712) 已通过。此计数不包含其后新增的网眼测试和容量汇总优化；最终提交仍需对应 CI 验证。

前端完整工作区 **248 项通过（1.7 分钟）**，生产 TypeScript/Vite 构建通过。真实后端浏览器原三项已在本地及上述 CI 的 browser 任务通过；网眼第四项实际准备/冻结另跑 **1 项通过（40.9 秒）**。各场景、页面覆盖和合成数据截图见[功能交付对照](functional-delivery.md)。

嘉书与网眼完整主链各一次均通过（两项合计 279.19 秒）：真实草稿准备、权限与 Scene 任务、冻结和提交，每个场景 138 个目标视频与 138 个封面经过实际 SDK 序列化和独立回读；最终 6 Campaign、18 Ad Group、36 Ad，三层全 ENABLE、Campaign 日预算合计 USD 600。业务实现、PostgreSQL、Redis 和持久任务真实执行，只有外部 HTTP/S3 传输使用替身。

目标封面在创建广告时重新核实，最终发送前再次核对当前视频/封面与证据期限；已确定成功的节点保持原状。`47f108b` 的执行/恢复/回读相关 **75 项通过**，另有独立 **73 项回归与 1 项双素材作用域探针通过**。具体规则见[封面契约](../contracts/tiktok-covers.md)。

`0013_dispatch_expansion_index` 已完成真实 pg_dump/pg_restore 演练，所有表行数、合成凭据解密、对象元数据和待发任务均一致；没有启动消费者或对象存储调用。结果见 [backup-restore.json](backup-restore.json)。

## 早期执行基线

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

## 最终验收与外部条件

最新代码继续进行统一 CI、完整故障场景和容量汇总查询复验。已完成的 100,000 账户目录、200,000 Campaign / 600,000 Group / 1,200,000 Ad 冻结计划和 10,200,000 执行步骤有实际数据库记录；最终性能与恢复副本的证据由[容量记录](capacity.md)单独给出，不能解释为已创建真实广告。

Linux prefork 硬截止测试位于 `test_prefork_deadline.py`：官方 SDK 连接本地停滞 HTTP 服务，由实际 Celery prefork 硬截止终止子进程，再验证 UNKNOWN、保留请求和无重复 POST。macOS 跳过不算通过，Linux CI 结果单独核对。

真实 TikTok、版权方、S3、OAuth 与试投状态见[真实联调记录](live-sdk.md)。
