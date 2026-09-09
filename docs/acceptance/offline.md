# 离线验收记录

## 最新集成验证

本地集成 `3e72e94` 的模块回归：`PYTHONPATH=. uv run pytest --ignore=tests/acceptance -q --tb=short --show-capture=no`，**1,317 passed、1 skipped，731.72 秒**。唯一跳过项是在 Linux 上运行的真实 prefork 截止测试；同一提交的 [Linux 模块 CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34316838207/job/102354717712) 已通过。此计数对应该次集成，不包含后续新增的网眼与容量专项。最终提交的完整 CI 按 SHA 查看[分支工作流记录](https://github.com/yaotingfeng/tiktok-ad-automation/actions/workflows/ci.yml?query=branch%3Afeat%2Fplatform-implementation)。

前端工作区早期完整 **248 项通过（1.7 分钟）**；补充 BC 空态后，`c433723` 对应修正由独立 agent 以 CI 单 worker、关闭重试验证 **250 项全部通过（4.9 分钟）**，生产构建、完整 TypeScript 与相关 Biome 检查通过。真实后端浏览器原三项已在本地及上述 CI 的 browser 任务通过；网眼第四项实际准备/冻结另跑 **1 项通过（40.9 秒）**。各场景、页面覆盖和合成数据截图见[功能交付对照](functional-delivery.md)。

嘉书与网眼完整主链各一次均通过（两项合计 279.19 秒）：真实草稿准备、权限与 Scene 任务、冻结和提交，每个场景 138 个目标视频与 138 个封面经过实际 SDK 序列化和独立回读；最终 6 Campaign、18 Ad Group、36 Ad，三层全 ENABLE、Campaign 日预算合计 USD 600。业务实现、PostgreSQL、Redis 和持久任务真实执行，只有外部 HTTP/S3 传输使用替身。

目标封面在创建广告时重新核实，最终发送前再次核对当前视频/封面与证据期限；已确定成功的节点保持原状。`47f108b` 的执行/恢复/回读相关 **75 项通过**，另有独立 **73 项回归与 1 项双素材作用域探针通过**。具体规则见[封面契约](../contracts/tiktok-covers.md)。

`0014_recovery_candidates` 已完成真实 pg_dump/pg_restore 演练，所有表行数、合成凭据解密、对象元数据和待发任务均一致；没有启动消费者或对象存储调用。结果见 [backup-restore.json](backup-restore.json)。

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

两版权方的 18 项跨模块验收已分别自然完成：两项主链、两项网眼未知取链恢复、五项预览/输入/任务驱动、五项隔离/权限/请求别名及四项 SDK 创建丢响应。原 SDK 故障四变体等待实际租约和 Beat（1,206.24 秒），没有修改时钟或缩短生产租约。`790fe0f` 的网眼可选名称修正后，完整版权方回归 233 项通过；只有持久化的真实创建 ID 回执允许精确回读省略可选名称，未知历史匹配仍要求稳定名称。

`backend/tests/modules/builds/test_execution_tasks.py` 从持久 outbox 逐个推进依赖。素材本地映射核实后创建 CTA、Campaign、Ad Group、两条不同文案的 SP，然后通过官方 GET 接口回读。正常流程共有 5 次 create（包含 CTA），三层广告均为 ENABLE。

远端 Campaign 已创建但响应丢失时，记录 UNKNOWN；模拟原租约确已过期后，真实恢复服务通过 GET 找回 ID，再继续其余广告。创建次数仍是 5 次。查询返回空时只发生 CTA 与 Campaign 两次 create，Campaign 保持 UNKNOWN，任务为 NEEDS_REVIEW；不会循环核查或发出第二次 create。

`test_review_execution.py` 使用真实 PostgreSQL deferred constraint trigger，在远端 CTA 已返回后使首次本地 receipt commit 失败；新的事务保存 `LATE_CREATED` 实际 ID，后续 portfolio GET 恢复。这与 SDK 抛出异常但远端状态未知的场景分开验证。

消息恢复专项覆盖已提交成功 receipt 后、唤醒下游之前进程退出。相同 delivery ID/revision 再次送达只执行持久续调；已成功回读的源步骤保持 READ 投递模式，不能误改为 create。结果确认和 unit 唤醒在同一数据库事务提交。

## 容量与最终集成

集成 `3e72e94` 的[完整七项 CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34316838207)和后续 `1672967` 的[完整七项 CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34319896781)均已通过，包括模块、跨模块验收、两组故障恢复、真实后端浏览器、前端与镜像。后续的网眼可选名称修正、第四项浏览器验收及容量优化均已集成并各自通过专项与独立复核；最终整套 CI 结果按上述分支页面的精确 SHA 核对。已完成的 100,000 账户目录、200,000 Campaign / 600,000 Group / 1,200,000 Ad 冻结计划和 10,200,000 执行步骤有实际数据库记录；详情汇总实测 11.642 秒，单个巨型批次的列表页 17.302 秒；恢复计数的约 22 秒开销降至约 15 毫秒。完整性能与恢复副本证据见[容量记录](capacity.md)，不能解释为已创建真实广告。

Linux prefork 硬截止测试位于 `test_prefork_deadline.py`：官方 SDK 连接本地停滞 HTTP 服务，由实际 Celery prefork 硬截止终止子进程，再验证 UNKNOWN、保留请求和无重复 POST。macOS 跳过不算通过，Linux CI 结果单独核对。

真实 TikTok、版权方、S3、OAuth 与试投状态见[真实联调记录](live-sdk.md)。

最后新增的汇总、恢复计数与网眼工作流集成回归为 **49 passed，19.40 秒**；后续独立恢复资格探针另有 2 项通过（涵盖 112 种状态组合）。`0014` 的升级、降级与模型比对均已验证，最新备份演练保留精确迁移版本。

## 最后一次 CI 问题定位

`821525f` 的前端工作区有两个严格定位失败：新增空态使标题文字同时出现在页面标题与卡片标题。测试已改为按 heading 精确定位；本地先复现两项失败再验证通过，最终 250 项全量通过，没有改 UI 行为或该组测试的等待时间。

同一 CI 的两个完整浏览器执行场景超过原 180 秒等待预算，另外两个准备/预览场景通过。本地保留 180 秒预算的单场浏览器复现通过（2.6 分钟）；独立任务时序探针完整通过（140.53 秒），其中提交后执行为 103.46 秒，不能把准备时间计入提交阶段。138 次目标上传后首次视频 GET 在提交后 64.27 秒发生，封面与广告随后正常完成。本机没有复现 CI 超时，不能仅凭本地成功断定 CI 当时的具体停留状态。

浏览器执行阶段等待预算现与后端 `Runtime.drive_until` 统一为 240 秒；准备与预览仍为 180 秒，完整单场景仍为 600 秒，生产租约、延后读取及重试规则均未缩短。增加阶段耗时、当前场景状态与步骤计数、最后状态变化时间和租户范围队列诊断，供最终精确 SHA 的 CI 核对；这里不以自动重试代替失败分析。

配套范围诊断集成后，四项真实后端浏览器场景整组 **4 passed（6.6 分钟）**；两个完整提交后的执行阶段分别为 102.2 秒和 102.9 秒。随后针对测试证据归属补充了独立红绿回归：Smart+ POST 只计当前场景目标账户与素材源账户，排除其他租户仍在后台推进的任务，并保留当前租户误向素材源账户发广告的检测能力。新增范围/HTTP/证据三项回归 **3 passed（1.15 秒）**；未修改生产业务逻辑。最终版本的整组结果仍按精确 SHA 的 CI 记录核对。
