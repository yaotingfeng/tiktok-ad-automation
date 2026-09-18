# 构建依赖与准备阶段超时修复发布验收

## 范围与结论

- 目标为新加坡测试环境；相关失败批次属于新骏伯 BC `7683817908149272592`，冻结通道为 `OFFICIAL_MCP`。本次只发布代码、前端和运行文档，不修改 BC 绑定、授权、功能开关、调用额度或 Worker 并发。
- 发布固定提交 `815161166f956e6775cb1543224cfa8f80d64493`，包含租户管理员创建普通账号、确定终态素材/封面依赖直接落定及重复唤醒缩减，以及请求正文登记前本地截止时间的有限重试。数据库无新迁移，head 保持 `draft_scene_phase`；`uv.lock`、`bun.lock` 与旧 release 摘要一致。
- 本次未调用产品 `RETRY/RECONCILE`，未发起 TikTok 写入。发布后 `step_evidence` 没有新增记录，四个队列均为 0；原失败与 UNKNOWN 未被升级过程自动改写或重发。

## 失败根因

- R627 的 ADGROUP 步骤 `aaeaab0d-76e6-5250-b1af-b16f326828b3` 为 `FAILED/tiktok_call_deadline_exceeded`，attempt 1，请求正文为空且没有 `REQUEST_ARMED`，证明截止时间发生在网关准备/准入阶段、尚未进入 TikTok 创建发送边界。旧逻辑只让 armed 后的明确 `NOT_SENT` 共享三次传输失败预算；同一个错误在 arm 前作为普通 `DomainError` 直接落为 FAILED。
- 修复将 arm 前路径显式标记为 `definitely_not_sent`，复用现有最多三次、5/10/20 秒退避计数；第三次仍失败才落终态。它不生成 `REQUEST_ARMED` 或 `NOT_SENT` 证据，也不会把已发出但结果不明的请求当作安全重试。
- R627 另有两个依赖失败广告和两个 `readback_inconclusive` 广告；6X3F 有两个 `readback_inconclusive` 广告。四条 UNKNOWN 的完整回读页只命中同广告组已成功的兄弟广告，目标名称缺失，但原创建请求已 armed/发送，负向列表不能证明远端绝对不存在。自动补建仍被禁止，只有用户另行明确接受重复广告风险后才可使用现有同组单条补建流程。

## 备份、恢复与切换

- 发布前四个 Redis 队列为 0；仅有两条历史 `materials.verify_original` 未发布 Outbox，与本次广告创建失败无关。停止 API 与 Beat、正常排空四个 Worker，并暂停备份 timer 后开始备份。
- 完整备份 `/var/backups/tt-ada-staging/20260918T191556Z/` 包含 PostgreSQL dump、Redis RDB、当前项目及前端构建、私有配置/systemd/Nginx/证书/备份脚本归档、release 指针、manifest 和 SHA-256。项目隔离解包核对 1,316 个文件；PostgreSQL 独立恢复核对 22 张表和 2,060 份加密响应，迁移演练无新增操作；Redis 独立实例 `PING` 与读取通过；私有配置逐文件比对通过。未配置异地副本。
- 第一个候选归档携带 macOS 扩展属性，Linux 解包产生 `._*` 伴随文件，预检编译因此失败；当时未停服务、未备份、未切版。重新以禁用扩展属性的归档生成候选，确认 `._*` 为 0 且配置/迁移/重试路径预检通过后才进入维护窗口。坏候选隔离目录在验收后删除，正式 release 和完整备份保留。
- `MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true`、调用策略、租约和 `Resource=2、Result=1、Build=1、Control=1、Beat=1` 保持。API、资源、结果、搭建、控制、Beat 六服务均运行新 SHA，`NRestarts=0`；backup timer 恢复 active。

## 验证与运行状态

- 本地：准备阶段截止时间回归先失败后通过；执行状态、安全未发送恢复及租户管理共 53 项真实 PostgreSQL/Redis 测试通过，租户/授权页面 106 项通过，TypeScript/Vite 构建、Ruff、格式、ty 和差异检查通过。
- 服务器：新超时边界、素材终态直接落定、封面终态直接落定、权限暂拒保护和只提前未来依赖共 7 项通过。首次专项命令因应用角色按设计没有 `CREATEDB` 而出现 6 个 fixture setup error、1 项通过；按运行手册临时启用专用测试角色后 7 项全部通过，随后恢复 `tt_ada_test NOLOGIN NOCREATEDB`，应用角色继续 `LOGIN NOCREATEDB`。恢复库和临时目录已删除。
- `check-bootstrap.py` 通过健康、构建登录页、回调业务错误和 API 边界；四个 Celery Worker 节点均 pong。数据库 head、功能开关、调用策略、备份校验再次通过；发布后 journal warning/error 为 0。
- 截止最终快照，两提交仍为 `NEEDS_REVIEW`：R627 保留 `1 ADGROUP deadline + 2 AD dependency_failed + 2 AD UNKNOWN`，6X3F 保留 `2 AD UNKNOWN`。这是有意保留真实失败事实；新代码只会作用于未来或经明确授权重新安排的安全未发送执行，不把部署本身冒充为业务恢复。
