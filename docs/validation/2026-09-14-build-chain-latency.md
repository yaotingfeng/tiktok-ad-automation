# 搭建链路耗时：诊断与本地修复

## 真实省略封面实验：结论已纠正

2026-09-14 后续用户明确要求实测。官方 SDK 的 optional **不能用于当前 Minis SINGLE_VIDEO 场景省略封面**：在原账户/原组进行独立 DISABLE 诊断请求，TikTok 返回 **40002**，消息为 `Invalid param(s): video material should have 1 image_info as the video cover.`。真实 request_id 为 `20260914193137CF248326AD1E1D73B5B8`，完整请求与工具回执仅存测试服 root 私有目录。未取得该诊断广告 ID，未投放诊断广告；错误结果仍不伪造为 NOT_SENT。

首轮发布 `ebd7e90736fff5c3a2a5ca88bdbb984f09922fe9` 后，通过正常恢复 API 只调度原失败 CTA 一次：11:26:14 UTC 受理，11:26:20 CTA 成功，11:26:27 系列成功，11:26:35 组成功。由于视频证据已超过 15 分钟，23 个现有目标视频进行只读核验，最后一项 11:27:38 完成；没有再次共享/上传视频。两条缺省封面的原广告请求分别在 11:27:56/59 返回泛化业务错误并保持 UNKNOWN，不能写为成功。

旧错误处理未保存平台业务码/原文，无法将后续独立诊断的原文倒填成这两次请求的证据。正常 RECONCILE 11:32:50 查询原组完整空页，仍是 `readback_inconclusive`，不能据此认定必然没有副作用或盲重试。当前原批次 6/8 广告成功；原 6 条 ID 和请求摘要保持不变。原 CTA/系列/组已恢复为 4/4，14/16 回读完成。

原两次请求关联号仍保存，供平台进一步查证：`20260914192756806E0D0CD9FE8B83642A`、`20260914192759BCA6C52C11523B3ACCCD`。它们与独立诊断请求不同，不能混用。后续若在原组补建须形成明确的新广告意图，并保留原 UNKNOWN 与重复风险说明；现有恢复入口不会改写原正文或从空列表推定可重新创建。

因此撤销默认省略封面的实现，保留通用编译器 optional 字段支持，但当前自动搭建必须准备并传入已验证封面；未知历史与冻结请求不改写。共享完整证据直接发布、同任务会话复用、独立 control 和周期 tick 过期等优化保留。平台业务码的安全保留与封面默认纠正正在补充验证，最终运行版本另记。

首轮完整备份 `/var/backups/tt-ada-staging/20260914T112414Z/`：PG/Redis/项目含前端/私有配置证书五份归档 SHA256 通过，隔离 PG 恢复与迁移通过，24 条加密响应可解密且摘要一致，独立 Redis 装载 PING 通过，项目/前端/配置独立解压 cmp 通过。无异地复制。迁移 CLI 曾因复用虚拟环境的入口加载旧模块而中止，未切版本；改 `python -m alembic` 校验 current=head 后才继续切换。7 个 API/业务 Worker/控制 Worker/Beat 进程的真实 cwd、私有配置、开关 true/true 和调用额度一致；2 个 Worker ping、HTTPS 双角色登录及隔离通过。内存 available 515 MiB，swap 已用 234 MiB，未提高业务并发。

后文“服务器只读诊断”及其后章节保留首轮诊断历史，不代表最终实测结论。

### 最终测试服版本与未完成事项

- 最终运行 `68e18c6ddc87ebf787faaeafa2f397105b37cc58`，已恢复当前构建必需封面；没有迁移、新开关或配置替换。新增安全平台错误码留存，不根据非零码自动重发。
- 第二轮完整备份 `/var/backups/tt-ada-staging/20260914T114539Z/`：五份归档校验通过；恢复库 `tt_ada_chain_restore_68e18c6d_test` current=head=`provider_display_drama_id`；24条加密响应恢复解密和摘要通过；Redis隔离装载、项目/前端/私有配置独立解压校验通过。旧版本和备份保留，未配置异地复制。
- API、业务Worker及2子进程、控制Worker及1子进程、Beat共7进程真实工作目录均为最终版本，私有配置一致，导入/清理开关保持true/true，额度及必需封面代码检查通过。HTTPS两角色登录、租户/平台隔离、静态资源和MCP配置READY通过；这部分不等于新广告成功。
- 两个Worker ping通过，备份/证书timer正常，resources/builds/control三队列均为0；最终内存available 530 MiB、swap已用75 MiB，无当前队列卡死证据。
- 最终只读复查仍为6条广告SUCCEEDED/ENABLE，2条UNKNOWN；原6条ID和请求摘要与发布前基线一致。CTA/系列/组4/4成功，回读14/16成功。无封面独立诊断未取得广告ID；最终发布没有再次创建广告。浏览器不可用，本次实际验收使用认证API、数据库及平台回执。
- 原2条UNKNOWN未解决，不把故障定位/代码修复报告为整批完成。后续需要独立明确的新广告补建意图或原平台无副作用裁定；现有系统没有安全改写原UNKNOWN正文的入口。不能承诺90个冷目标素材秒级完成；批量共享仍未实施。

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
