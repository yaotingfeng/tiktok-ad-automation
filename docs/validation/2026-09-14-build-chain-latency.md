# 搭建链路耗时：诊断与本地修复

## 服务器只读诊断

目标为新加坡测试环境，运行版本 `e45decdc632b82f6df00caaf1c701fb15b0d772e`。两部剧分别 22/23 个视频，在两个账户展开为 90 个素材步骤、4 个 CTA、4 个系列、4 个组、8 条广告及 16 个回读步骤。

- UTC 09:33 提交；首个系列至 09:55 才开始。三组实际创建从 09:54:58 至 09:56:17，约 80 秒；回读至 09:56:25。主要等待发生在创建前，不能将全部时长归为广告 API 慢。
- 最终 90 个 MATERIAL 成功；3 个 CTA、3 个系列、3 个组、6 条广告和 12 个回读成功。其余一个 CTA 终止，连带 1 系列、1 组、2 广告、4 回读失败。
- CTA 原证据为 `NOT_SENT / mcp_call_failed`，没有远端 ID；同账户另一组合稍后以同一正文摘要创建成功。原始底层异常类型未保留，不能补猜成某个 HTTP 状态或超时，也不能称为平台审核拒绝。
- 大量视频已完成共享但等待封面；09:52 已有 50 个封面 READY，约 20 分钟仍无广告。90 个 MATERIAL 本地步骤把图片准备错误地绑定为强制依赖。
- 两个 prefork 同时消费 resources/builds/control；09:51 队列共超过千条，包含 546 条 flush tick 和重复执行消息。未发现数据库长锁或 Worker 死亡；封面完成数持续上升。最后三队列均为 0，不是仍在运行的死锁。
- 本次仅查询服务器进程、队列、数据库和日志；没有修改服务器、重试原广告或调用 Codex 广告 MCP。

## 本地行为调整

1. 共享目标的第一页且唯一完整结果具有实际目标 VID、可用状态和相同 MD5 时，直接发布该证据，不另排相同 VID 的详情请求。多页、歧义、不可用、缺失证据和严格规格不足仍维持原核查路径，不放宽账户及路由范围。
2. 新 `SINGLE_VIDEO` 请求支持不传 `image_info`；素材准备和创建不启动未指定封面任务。已冻结的自定义封面正文不改写，其图片身份和新鲜度仍由最后发送前检查验证。旧封面上传身份和未知结果仍能单独核查，不删除历史。
3. 明确 `NOT_SENT` 的 MCP 传输类故障最多尝试 3 次，前两次分别延迟 5/10 秒；同一冻结正文/attempt 的不同 nonce 共用计数。已发送或未知结果不进入该重试规则。
4. MCP 日志新增固定阶段、固定异常分类、发送边界与耗时，禁止异常正文、URL、请求和凭据；无法据此还原历史未记录的底层错误。
5. 周期 control tick 到下一周期过期；真实业务 outbox 不过期、不清空，仍持久恢复。部署时必须结合独立 control 消费能力，不能以过期策略代替调度资源隔离。

## 文档与验收边界

[官方 SDK CreativeInfo 合同](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/SmartPlusAdCreateBodyCreativeInfo.md) 将 `image_info` 标为 optional。这能证明字段并非所有创意的全局必填项，不能单独证明当前 Minis、BC_AUTH_TT 与 SINGLE_VIDEO 组合一定允许省略。新请求逻辑是待真实场景验收的实现，不将传输替身成功写成 TikTok 已接受或已自动生成封面。

本地回归使用真实隔离 PostgreSQL/Redis 和官方 SDK/MCP 传输边界替身。外部 TikTok、版权方和存储服务没有用于本地测试。最新测试结果在实施进度中记录。

### 本轮验证结果

- 先复现 NOT_SENT 被直接终止、无封面被阻塞、完整共享结果仍需详情读取、周期消息没有过期和传输异常无脱敏诊断，再实施修正。
- 专项组合 122 passed / 3 failed。完整链路的旧夹具没有按实际推广链接记录 Mini 选择，已补齐；API/MCP 完整链路随后 2 passed / 59.21 秒。MCP 初次停在 PENDING，不能把该现象直接归因于 Mini 夹具。日志诊断用例独立复测 1 passed / 0.62 秒，包含它的安全组合也通过；不声称首次组合全绿。
- 安全组合 87 passed / 82.12 秒：MCP 传输、执行状态、审查用例、outbox 后续执行，以及此前大矩阵发生 setup 异常的短配额租约案例。覆盖 UNKNOWN 不重发、旧 nonce 不接管、保留冻结正文和迟到回执。
- 封面/共享/调度中间组合 28 passed；显式图片最后围栏和无封面完整创建也已执行。mypy 10 个实现文件通过，Ruff/格式及 diff 检查通过。
- 全 builds 大矩阵未跑完，中途一个 setup 异常；转为上述可定位组合复测。中断时 pytest 临时目录 teardown 报错，大矩阵不计为通过；没有运行或声称全部后端回归通过。发布前应再次完成稳定的组合验收。

专项使用本地专用 PostgreSQL 测试库及 Redis 14/15；`pytest -q` 路径为 builds 的 `test_cover_execution.py`、`test_execution.py`、`test_transient_mcp_create.py`，materials 的 `test_native_distribution.py`，contracts 的 `test_tiktok_build_contract.py`，integrations 的 `test_mcp_transport.py`、`test_dual_channel_flow.py`，以及 core 的 `test_runtime_config.py`。安全组合另外执行 builds 的 `test_execution_state.py`、`test_review_execution.py`、`test_execution_tasks.py` 与上述短租约单项。

## 尚未落地

- 测试服务器未发布；需确认当前视频场景的封面条件，并按部署手册完成新批次全量备份及隔离恢复，再安排版本切换。
- 测试机约 2 GiB 内存，空闲可用约 428 MiB，不能直接大幅增加进程数。应评估独立 control 消费者及业务池容量；生产 Compose 已分离队列，不能混用其配置。
- 原终止 CTA 已保留冻结正文，现有人工重试入口只接受未冻结正文的失败步骤，不能靠清除正文/历史或盲目重置状态恢复。其安全恢复需逐 nonce 验证未发送证据并保留原提交范围，未在本轮执行。
- 源元数据缓存、同任务 MCP 会话复用、批量共享及调度去重仍是后续优化，不声称本次已实现。不能承诺 90 个尚未分发的素材秒级完成，也尚无发布后的端到端耗时数据。
