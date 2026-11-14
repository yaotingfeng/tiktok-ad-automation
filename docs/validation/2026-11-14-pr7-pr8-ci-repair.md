# PR #7 / #8 冲突与 CI 修复验证

## 范围与基准

- PR #7：原 head `83bbbc5b`，已有树 `fix-jb3-business-export-pr`；合入远端 main `456a2c86` 后修复基准 `ae923c8b`。
- PR #8：原 head `2995ade2`，已有树 `fix-jb6-historical-log-pr`；合入同一 main 后修复基准 `00adb938`。
- 两次合入仅冲突于 `docs/implementation-progress.md`，双方追加记录均保留。主工作树及其未提交内容未修改。
- 原最小目标是消除文档冲突并修实际 CI 红项。确认红项包含共同基线欠账后，范围扩展到 SQLModel/SDK 类型合同、链接恢复状态及前端回归夹具；不合并两份独立产品功能。情景收入保持既有四位格式。

## 修复

1. SQLModel 查询使用既有 `col()`，原始 SQL 复用 SQLAlchemy Session 类型视图；多列结果按 tuple 解构，边界模型复用 Pydantic 验证。固定三元 Decimal 返回保留原公式，SDK 严格成员形状保持。
2. datetime 字段显式表达等价 Column。两个脚本分别执行可重复机械修复和运行时边界验证；21 列的 SQL 类型、时区、nullable、default 表达式/形状、index、unique 均不变。0115 仅增加 typing.cast，去除类型视图后的迁移 AST 与基准一致，无 DDL 行为变更。
3. provider 可继续状态归 `link_steps` 所有。未知写入仍沿原任务只读找回；明确多候选转 `manual_review` 并保留 Unknown。执行器、直接领取、有界查询和 watchdog 共用终态/停止阶段定义，不再重复 POST 或空投人工任务。
4. 草稿仅等待未被手动链接替代的自动输入；跨页保留 pending。VIDEO/CAROUSEL 人工补链可结束，原未知 checkpoint 不改写。
5. 素材页跨 BC 保留租户级筛选及绑定原 BC 的传输；新上传使用新顶栏 BC。其他旧 BC 标签清除，当前非素材页重建，隐藏未保存表单仍参与切换守卫。完成页缩减冗余间距，保留首屏结果。
6. 修正旧布局/文案/守卫断言、准备页按钮、V2 创意单元查询、SOURCE outbox 精确身份及 VideoCreative 编译夹具。管理员 DSH 正向断言回到管理员用例。冗余 AI 源码拼写检查由既有真实 MANUAL/AUTO 服务回归覆盖。GitHub pytest 与本地门禁同样排除已明确退役的 mcp marker，不排除有效业务用例。

DRY record:
- scope：查询类型、provider 恢复、素材 BC 生命周期与 CI 夹具。
- searched：现有 SQLModel col/SQLAlchemy Session 用法，providers tasks/link_steps/Wangyan，drafts/manual_links，WorkspaceTabs/MaterialsPage，V2 编译与封面 outbox owner。
- reused：现有类型视图、Pydantic 边界、TERMINAL/STOPPED_STAGES、原任务和 outbox、页面本地上传守卫、VideoCreative。
- remaining duplication：Python 状态判断与 SQL 筛选共处同一 owner 并共享常量；测试保留独立行为预期。

## 动态证据

所有数据均为独立本地 PostgreSQL18/Redis7 合成数据；环境不加载生产配置，不调用真实广告或版权方服务。

- 当前 main 合入后类型检查：`337 diagnostics` → 两树 `All checks passed!`。两树 `ruff check app tests` 通过。
- `scripts/verify-sqlmodel-type-boundaries.py ae923c8`（PR7）及 `00adb938`（PR8）：`col identity preserved; 21 datetime fields unchanged; migration AST unchanged`。机械脚本再次运行零改动。
- 原前端完整 workspace：`750 passed / 2 failed`，两失败均为普通用户错误期待管理员入口；修后仅补跑两 viewport 及管理员正向用例：`3 passed (4.0s)`。复用其余750项结果，没有重复全量。此前18项定向及新增跨 BC 隐藏脏标签回归通过。
- 真实本地 API browser 原失败场景：`1 passed (2.5m)`，4 Campaign / 12 Ad Group / 24 Ad，failed/unknown 均0；原准备页错误按钮已越过并完成执行。
- 原后端两剧/王眼反例：`2 failed, 2 passed`；R2 最初六例均因 SOURCE/BUILD 同名事件查询歧义失败。修事件身份后暴露旧 dict 创意夹具，改用 VideoCreative 后六参数均通过（首参数在5项检查中通过，其余在后续13项检查中通过）。一次 ffmpeg 15秒 setup 超时在原配置重跑通过，没有扩大 timeout。
- 恢复/AI 聚焦轮：39项通过；SDK、日志 planning/daily、AI MANUAL/AUTO、invalid 恢复等 owner 回归：`137 passed, 36 deselected`（退役 MCP）。旧长 batch 轮因已加载过时 fixture/歧义逻辑中断，12通过与1失败保留，不算整轮通过。
- PR7 实际导出/查询/公式：`91 passed in 226.72s`。PR8 独立库历史日志/重建：`14 passed in 3.69s`。
- M1 有限验证：5000 实际 Decimal 输入、27快照状态及负控通过。M2：78732 转换及5个负控通过。模型不是全系统证明。

复现入口（从 backend/，沿用独立测试环境）：

```sh
python -m pytest tests/modules/business/test_export_integrity.py tests/modules/business/test_queries.py tests/modules/business/test_exports.py tests/modules/business/test_calculation.py -q --tb=short
python -m pytest tests/modules/operation_logs/test_historical_evidence.py tests/modules/business/test_historical_log_rebuild.py -q --tb=short
python -m pytest tests/modules/builds/test_manual_links.py::test_manual_override_does_not_wait_for_reused_unknown_provider tests/modules/providers/test_preparation_tasks.py::test_watchdog_does_not_republish_stopped_unknown -q --tb=short
```

## 独立审查与修复

R1：Standards PASS，发现 P2 人工状态仍被 watchdog 重投；Spec BLOCK，发现 P1 人工补链被旧未知任务阻塞。主线补真实 PG 回归，先得到：

```text
3 failed, 1 passed in 8.28s
```

修复后：

```text
5 passed, 1 warning in 22.16s
13 passed, 1 deselected, 5 warnings in 65.21s
```

前一轮包含两种媒体人工覆盖、两种停止阶段及 R2 首参数；后一轮包含正常恢复的正向对照、双媒体状态与 R2 其余参数。保留既有 SQLAlchemy 表循环警告，不将其当新产品失败。

R2：独立 Standards PASS、Spec PASS，无 P0/P1；旧 P1/P2 已关闭。跨页混合输入由静态控制流核对，未新增专门动态回放。108个公共修复文件在两树逐字节一致，原M1/M2独有产品差异保留。

## 交付边界

本记录证明本地实现及所列检查。没有重跑全仓后端、Linux镜像或远端新CI，没有部署、生产BC数据对账、真实平台投放或远程审批。两份PR的远端head在明确推送前保持原值；源分支使用普通合入，不重写已有历史，不强推。
