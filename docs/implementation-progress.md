# 实施进度

## 当前状态

2026-09-09：持续在用户指定仓库 `feat/platform-implementation` 实施。P01/P02 已交付并独立复核通过；P03 版权方后端及工作区已接通；P04 上传、回查与分发已独立复核通过，素材页面开发中；P05 策略后端和页面已实现，草稿已接通并正在完成独立复核；P06 官方场景证据和权限验证实现中。冻结预览、广告执行和最终验收仍在继续，未执行真实广告操作。

| 范围 | 状态与证据 | 主要集成提交 |
| --- | --- | --- |
| P01 Task1 基线与官方 SDK | 冻结安装/构建通过；11 个离线契约，独立审查 PASS | 10ee3ee, dc4d3b0 |
| P01 Tasks2/3 公共契约与进程配置 | API、安全错误/日志、真实数据库隔离、JSON任务、控制队列；独立审查 PASS | 6221f52, 6a66c24, ea18b79 |
| P01 Tasks4/7 可靠投递与共享准入 | 52 个真实 PostgreSQL/Redis 测试；公平轮次、崩溃重投、并发锁、六项原子配额，独立审查 PASS | a01b13a, 34e151c |
| P01 Task5 登录、工作台、回调入口 | API客户端再生成；16 个工作台浏览器测试；构建/TS通过；1024断点与焦点修复独立复核 PASS | 1f75633, 3097c72, 192eeab, b619f9a, 6b5cb77 |
| P01 日志审查修复 | 删除会采集 OAuth 查询串的继承 Sentry 初始化；独立回归与复审 PASS | 8ef97a8 |
| P01 Task6 运行与部署包 | 实际本地管理员登录、静态页面/健康/回调检查、Beat→Worker no-op均通过；Compose两套配置通过；GitHub CI镜像构建通过；没有公网环境 | f192a30 |
| P02 Tasks1/2 租户模型、管理与审计 | 34 项租户回归独立审查 PASS；初始管理员搜索支持完整 ID | e46930c, 05ad598, 183ab6c, 78274e7 |
| P02 Task3 OAuth、加密、回调 | 一次性 state、候选凭据、失败保留旧连接、子进程硬截止；62 项专项独立审查 PASS；实际 callback 已接通 | 7eb5170, 318dadb |
| P02 Task4 完整账户目录发现 | 41 项独立测试及迁移 roundtrip/check PASS；逐页持久化、失败恢复、完成后才撤销旧关系 | be5e44b, aa8ada2 |
| P02 Tasks5/6 权限、目录、批量解析 | 38 项独立回归 PASS，含实际 PostgreSQL 10,001 账户无重无漏分页；配置与停用接口通过 | 0e7b20f, 5ea7018, 4b7f1bc |
| P02 Task7 租户、成员、账户、连接 UI | 全部页面已接实际 API；目录分页、完整字符串 BC ID、登录恢复、未保存表单保护及发现完成刷新已修复，最终独立21项+2个请求边界探针 PASS | 0367ead, 9495a1b, 4be7820, 6052bbe, 0177a69 |
| P03 Tasks1/2 模型与版权方协议 | 87项独立复核 PASS；租户应用与链接身份、嘉书配置对账、网眼公开前端协议证据；没有真实业务联调 | e46ec86, 509397e, d30a8d3 |
| P03 Tasks3/4 取链恢复与 API | 154项协议/工作流测试、381项相关跨模块测试通过；205行队列积压饥饿已复现并修复，最终独立50项复核 PASS | 1d5bcdb, 16322eb, 405937e |
| P04 Task1 目录与映射 | 26项独立复核 PASS；字面匹配、稳定分页、实际素材账户映射、Unicode空VID约束与迁移修复 | 5ac7b9e, 993cdf7 |
| P03 Task5 版权方工作区 | 19 项版权方浏览器测试及实际本地 API 空态通过；应用/历史/候选/安全配置差异；新增后端目录独立3项 PASS | 8c09e40, be9a497 |
| P04 Tasks2/3 原件与SDK上传 | 独立复核 PASS；修复真实PG发送前/回读落库锁序死锁及首次撤权无法恢复；未知结果不直接重传 | b50282f, fd26d5d, 49c793d, 755f7f4, 62129be, e1e8dc1 |
| P04 Task4 就绪与分发 | 独立99项相关测试 PASS；本地只读、共享唯一操作、目标实际VID、撤权后同消息只读恢复；60秒控制队列修复已注册 | 8581955, 5125ba0, 415a878 |
| P05 Tasks1–3 策略基础 | 45 项及额外并发/迁移独立复核 PASS；重复保存准确返回原版本 | 6d583fd, 60b106c |
| P05 Task4 草稿与API | 24 项通过；全行反馈、分批解析、字面素材匹配、可追溯编辑、只读分页；已修复独立审查3类P2，待最终复核 | 415a878, 24448d5, e0b3d37, 3d369d7 |
| P05 策略页面 | 26 项策略浏览器测试，连同其他工作区共128项通过；保存/冲突/回查/租户切换及三种屏宽 | 9d60a15 |

完整后端在 `415a878` 为 **710 passed**（47.70秒）；后续草稿审查新增与修复的 24 项聚焦测试通过。Ruff与新增模块 strict mypy/ty 通过；仅继承的 Starlette/httpx 弃用提示。最新策略 UI 整套 workspace 浏览器 **128 passed**；SDK/接口测试替身不代表真实平台联调通过。

## 远端与新增验证

- `be9a497` 的 [GitHub Platform CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34280164998) 后端、前端及容器镜像构建全部成功。该结果对应这个提交，不自动代表后续提交已过 CI；后续提交继续推送并核对精确 SHA。
- P01 UI独立复审补跑900/1023/1024px三项，通过；P01 Task6部署包独立审查通过。遗留模板部署指南已改为当前手册入口，旧Compose移除。
- 本地实际 API/数据库的浏览器流程已走通管理员登录、新建租户、进入租户工作台、读取初始管理员与角色。没有接入真实 BC，也没有创建广告。
- P02 Tasks1/2、Task3、Task4、Tasks5/6 独立审查全部 PASS。界面初次独立审查发现候选搜索冒泡误提交和长表表头滚走；已用 8 项先失败后通过的回归修复，并由 root 执行全部 42 项页面回归。
- TikTok SDK同步响应实际是已校验的`{data,request_id}`字典，计划示例中的统一`.to_dict()`假设不成立；OAuth实现已用实际SDK传输测试发现并更正，后续适配器复用同一解析契约。
- P03独立负载审查复现205行健康批次1800秒仿真后只完成74行，原因是恢复任务不断替换尚未发布的revision。`405937e`保持当前delivery ID/revision，未发布记录保留时间与退避、已发布丢失记录重新待发；修复后600秒仿真全部完成。没有改变外部写结果未知时先回查的规则。
- P05验证见[策略](validation/p05-strategies.md)与[草稿](validation/p05-drafts.md)；P04见[原件上传](validation/p04-object-uploads.md)与[SDK素材合约](integrations/tiktok-materials-contract.md)。

## 执行安排

- 所有执行与审查 sub agent 使用用户指定 `gpt-6-astra`、`high`；不会因耗时长而打断。
- 当前会话主线程持续负责集成、生成客户端、迁移、验证与提交。独立任务使用隔离 worktree、明确文件所有权；共享文件由主线程整合。
- 执行七份阶段计划，对应五批交付。P02完成后并行P03版权方、P04素材、P05策略逻辑；P06只读场景契约先于预览资源集成；再完成广告执行恢复与P07跨模块验收。
- 阶段提交推送到用户 origin 的功能分支；不强推。原资料目录不修改，不复制运营凭据。

## 已确认的实现裁定

- 用户仓库为空，导入固定官方模板快照并保留许可证，origin保持用户仓库。
- 实际模板为根目录 workspace/锁文件，计划中的旧锁路径按真实结构更正。
- 素材上传不匹配剧目；搭建时按完整剧名包含匹配文件名。所有剧目铺同一批全部账户，SP为相同素材不同文案的N条创意。
- 预览提交后三级广告创建直接ENABLE；实现阶段不发真实广告请求。
- 本地P01诊断Worker使用macOS solo验证消息链路，不能把它当作后续任务硬截止证据；真实调用需硬截止小于共享租约有效期。
- App、对象存储和配额未配置时相关业务明确阻止使用，登录/基本租户管理仍能运行。
- 官方SDK视频multipart在内部整读文件；原文件入库可以分片传输，SDK上传另设明确的工程容量上限，默认256MiB及1个同时上传。它不是TikTok官方文件限额，超过时保留原文件并说明阻塞原因。
- 已交付迁移保持不可变；P05先交付`0005_strategies`，草稿/预览后续追加迁移，不回头改已提交的策略迁移。

## 待外部条件

没有实际部署主机/域名与 TikTok App，公网 HTTPS 回调、真实 OAuth、官方SDK真实账户发现和广告试投尚未验证。没有以示例 URL、账户或测试替身冒充这些结果。缺少这些条件不阻止后续本地模块实现。详见 [部署手册](runbooks/bootstrap-deployment.md)。

## 下一步

完成素材页面、草稿独立复核及工作区整体验收；以已核实的 P06 只读场景证据生成冻结预览，再实现广告执行、恢复与 P07 跨模块验收。各阶段持续提交并推送；不把依赖外部凭据的联调记为已完成。

### P02 Task 4: directory discovery

- Schema contract `596c1ae`: six directory models, tenant composite keys, one active connection generation, external ownership constraints; migration `02c_directory` after `02b_connections`.
- Added page-atomic resumable discovery, candidate promotion after complete BC/account enumeration, per-call admission, durable recovery/outbox, and the bounded prefork resource task. Unknown capabilities and incomplete metadata remain blocked.
- Verification: 189 account/tenant/jobs tests passed, including 41 Task 4 cases; independent 41-test review and migration roundtrip passed. Root added pytest importlib mode (`0befa24`) to resolve duplicate test filenames in the standard command. All TikTok transport was fake; full evidence and P07 boundaries are recorded in [discovery validation](validation/p02-discovery.md).
- `sent_count` is a durable attempted-send counter after admission, not proof the HTTP transport started if the process crashed immediately afterward. Recovery relies on persisted page/claim/version state, not this counter.

### P04 workspace material read APIs

- Added tenant/BC upload-request recovery, lightweight upload batch pagination and on-demand 300-second private original preview URLs. GET routes do not enqueue work; viewers can read, and missing original/configuration/scope are explicit errors.
- Batch file counts use bounded parent-page SQL aggregation; original preview uses local SigV4 GET signing with inline allowlisted video MIME and no-store. No bucket permission change or external SDK write.
- Verification: 9 new regressions plus affected upload/progress/retry tests total 58 passed; Ruff, strict mypy and ty passed. See [material read API contract](validation/p04-material-read-apis.md). Root generates the client and independently reviews integration; no frontend files changed here.

## 2026-09-09：冻结预览与素材工作区集成

- 素材只读补充接口独立审查 PASS（64 项）；素材 UI 已合并，工作区 157 项浏览器测试通过，最后补充权限场景后素材完整 31 项通过。
- Scene/OAuth 独立审查 PASS（118 项）；发现的单页/跨页重复 ID 完整性问题已关闭。官方 SDK 创建编译器 26 条离线契约验证通过。
- P05 草稿独立复核 PASS（25 项）；冻结预览实现与 HTTP 接口已就绪，builds 模块合计 124 项通过。冻结预览独立审查、全工作区 UI 独立审查进行中。
- BC 账户权限引导任务正在开发，用完整只读能力检查把 UNKNOWN 授权事实转为可验证的创建/上传权限，避免依赖广告链接或逐剧重复扫描 BC。
- 后续继续搭建工作区 UI、提交执行、结果核查/恢复及 P07 验证。未进行真实广告创建或真实外部业务联调。

### P06 link-free BC capability bootstrap

- Added isolated capability jobs/request aliases/pages/account-role evidence with migration `0005e_account_capabilities` (schema commit `2c107db`).
- Explicit tenant/BC/connection refresh command, read-only status/evidence APIs, 50-row official current-token BC reads, complete-list publication in 100-grant transactions, durable claim/dispatch recovery. Details and integration registration contract: `docs/validation/p06-account-capabilities.md`.
- Dedicated engineering age setting defaults to four hours from first observation; no per-link BC scan and no capability inferred from account visibility. Root owns Scene/draft/preview integration.
