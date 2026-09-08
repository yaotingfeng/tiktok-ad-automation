# 实施进度

## 当前状态

2026-09-09：在用户指定仓库的 `feat/platform-implementation` 持续实施。P01 工程基础、P02 Tasks1–6 后端已实现并通过整合测试和独立审查。P02 租户/成员页面已接入实际 API，账户/连接/BC 页面正在实施；P03 版权方模型和协议适配器并行开发。完整广告业务流程仍在开发中。

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
| P02 Task7 租户与成员 UI 部分 | root 42 项浏览器回归、生成客户端、TS/build通过；搜索 portal 误提交与长表固定表头已修复；账户部分继续实施 | 0367ead, 9495a1b |

当前 P02 整合后后端 **332 passed**（root 78274e7，对应 P02 Tasks1–6 和完整 ID 搜索），仅继承的 Starlette/httpx 弃用提示。root 9495a1b 的前端工作台、租户和成员 **42 项浏览器回归通过**，包括真实修复前复现失败的 8 项 portal/长表用例。SDK/接口测试替身不代表真实平台联调通过。

## 远端与新增验证

- `3dcbe1d` 已推送 origin 功能分支；[GitHub Platform CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34266735739) 后端、前端及容器镜像构建全部成功。该结果对应这个提交，不自动代表后续提交已过 CI。
- P01 UI独立复审补跑900/1023/1024px三项，通过；P01 Task6部署包独立审查通过。遗留模板部署指南已改为当前手册入口，旧Compose移除。
- 本地实际 API/数据库的浏览器流程已走通管理员登录、新建租户、进入租户工作台、读取初始管理员与角色。没有接入真实 BC，也没有创建广告。
- P02 Tasks1/2、Task3、Task4、Tasks5/6 独立审查全部 PASS。界面初次独立审查发现候选搜索冒泡误提交和长表表头滚走；已用 8 项先失败后通过的回归修复，并由 root 执行全部 42 项页面回归。
- TikTok SDK同步响应实际是已校验的`{data,request_id}`字典，计划示例中的统一`.to_dict()`假设不成立；OAuth实现已用实际SDK传输测试发现并更正，后续适配器复用同一解析契约。

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

## 待外部条件

没有实际部署主机/域名与 TikTok App，公网 HTTPS 回调、真实 OAuth、官方SDK真实账户发现和广告试投尚未验证。没有以示例 URL、账户或测试替身冒充这些结果。缺少这些条件不阻止后续本地模块实现。详见 [部署手册](runbooks/bootstrap-deployment.md)。

## 下一步

完成 P02 账户/授权/BC 界面与复审，接着完成 P03 版权方连接与可恢复取链。P03 首个迁移稳定后衔接 P04 素材，继续 P05 策略预览、P06 执行与 P07 联调。各阶段持续提交并推送；不会把依赖外部凭据的联调记为已完成。

### P02 Task 4: directory discovery

- Schema contract `596c1ae`: six directory models, tenant composite keys, one active connection generation, external ownership constraints; migration `02c_directory` after `02b_connections`.
- Added page-atomic resumable discovery, candidate promotion after complete BC/account enumeration, per-call admission, durable recovery/outbox, and the bounded prefork resource task. Unknown capabilities and incomplete metadata remain blocked.
- Verification: 189 account/tenant/jobs tests passed, including 41 Task 4 cases; independent 41-test review and migration roundtrip passed. Root added pytest importlib mode (`0befa24`) to resolve duplicate test filenames in the standard command. All TikTok transport was fake; full evidence and P07 boundaries are recorded in [discovery validation](validation/p02-discovery.md).
- `sent_count` is a durable attempted-send counter after admission, not proof the HTTP transport started if the process crashed immediately afterward. Recovery relies on persisted page/claim/version state, not this counter.
