# 实施进度

## 2026-09-10：骏伯生产发布与基础验收

- 已按用户授权提交并推送现有代码，生产运行 SHA `016217f65a39330b4b715ab043fe866eea87810c`。入口 `https://manjuad.gzjunbo.net:8000/`，HTTP 自动跳转；原站 80/443 和 8 个原有容器保持正常。
- 生产独立 PostgreSQL/Redis，Alembic head 为 `r2_part_receipts`，API、3 个 Linux prefork Worker 与唯一 Beat 同镜像。平台管理员 admin、租户 junbo、租户管理员 junbo 已建立，密码未写入仓库。
- 真实 API 登录/隔离/策略版本测试、19 项完整生产浏览器检查、18 项重启后只读复验、outbox→Beat→Worker no-op、备份及临时库恢复通过。每日备份定时器已启用并实际试跑成功。
- 专用生产 Compose、Nginx、版本入口脚本、备份脚本与 systemd 单元已提交。构建镜像源调整保留锁定版本/hash，受限 GitHub 下载使用同一官方 SDK commit 的已验证 Git 缓存。后续文档提交记录首发事实，不表示运行镜像自动更新。
- [生产部署/更新/数据库规则](runbooks/production-junbo.md) 已登记在 AGENTS.md，[本次发布验收](validation/2026-09-10-production-release.md) 记录具体版本、证据与限制。相关实现提交：`7c11176`、`f39292d`、`5e1ecda`、`016217f`。
- TikTok API 出站探测两次连接超时，需在真实授权前处理；App、BC、R2 和版权方仍未配置，未执行真实上传或广告操作。该限制与基础上线通过分别记录。以下原有“未推送/未部署”描述是当时阶段记录，不代表当前状态。

## 2026-09-10：产品统一命名为 TT ADA

- 用户要求产品改名为 `TT ADA`，所有实际页面不再使用“短剧投放”“TikTok 工作台”或“工作台”作为名称。已覆盖登录页及页脚、侧栏品牌及移动导航、所有已有路由标题、设置页、根路由兜底和返回首页提示；功能页继续使用素材库、广告搭建等业务名称。
- 同步 README、`.env.example` 与 Compose 默认产品名，并更新既有浏览器测试的文案定位器。技术包名、仓库路径及历史设计记录保留。
- 验证：`bun run build`、改动 TS/TSX 的 Biome 检查及 `git diff --check` 通过；`bunx playwright test --project workspace --workers 4 --reporter line` 全部 **318 项通过（2.1 分钟）**。独立静态复核未发现业务改动或实际页面旧名称遗漏。
- 本地 `http://127.0.0.1:8011` 已更新，私有配置仅同步 `PROJECT_NAME` 并重启 API；OpenAPI 标题为 `TT ADA`，bootstrap 检查通过。使用实际本地登录只读检查登录、平台管理、账号设置、租户功能及 404 共 12 个页面，标题与页面文案通过；截图和检查结果保存在忽略的 `.runtime/tt-ada-*`，不提交凭据或本地数据。
- 本轮提交以 `ui: rename product to TT ADA across all pages` 为标识；未推送。

## 2026-09-10：仓库协作约束更新

- 按用户要求更新 `AGENTS.md`：明确独立仓库、聚焦提交、已有分支工作树、合并与显式推送规则，替换旧的功能分支开发及推送约束。
- 补充中文业务注释、复用封装、旧代码清理、禁止旧业务逻辑兼容/回退、影响评估、统一 shadcn/ui 设计、设计与计划拆分及生产发布要求。
- 验证：检查文档差异、发布手册路径和 `git diff --check`；仅文档修改，无需运行应用测试。提交以 `docs: update repository contribution constraints` 为标识，可通过 Git 历史定位；未推送，未改动已有未跟踪目录。

## 当前状态

2026-09-10 全工作区视觉统一：用户认可策略试版并要求主按钮恢复原黑色、推广全部页面。共享正文22px标题/灰底白卡/16与24px内外距、管理页、素材上传、策略编辑、广告搭建/任务与设置辅助页均已统一，整合源码`27ea0aa`，回归提交`47b074a`。完整workspace **318项通过（2.0分钟）**，TypeScript/生产构建及独立审查通过；实际8011应用已更新并核对。已推送回退标签`ui-before-workspace-rollout-20260910`（`601c181`），主目录与推广分支同步。[实施计划](superpowers/plans/2026-09-10-workspace-visual-rollout.md)及[验收记录](validation/2026-09-10-workspace-visual-rollout.md)记录覆盖范围与联调边界。以下视觉记录保留为历史。

2026-09-10 策略列表视觉试版：用户要求先保留代码再试改，已推送回退标签 `ui-before-refinement-20260910`（`4c732a2`）。本轮仅策略列表恢复22px正文标题、灰底白卡分组、16/24px内外距、固定短列与蓝色主按钮；其他页保留上一版。工作区/租户账户93项、策略36项及生产构建通过，独立评审发现的列宽和长数覆盖问题已修复。实际8011页面截图与跨页恢复已核对；[变更与验收记录](validation/2026-09-10-strategy-ui-refinement.md)保留细节及回退方式，等待用户评价试版阅读体验。

2026-09-10 视觉调整：用户确认后已按官方 dashboard-01 统一中性色主题、288px inset侧栏、16px顶栏标题、自适应内容区及单边框列表，代码 `eb613a4`。原有310项workspace回归、新增5项几何回归及生产构建通过；实际8011应用已更新并核对标题、24px边距与菜单跳转。[实施计划](superpowers/plans/2026-09-10-shadcn-official-alignment.md)与[验收记录](validation/2026-09-10-official-ui.md)覆盖本轮变更，以下保留业务功能交付历史。

2026-09-10：普通账号密码登录、邮箱移除和租户版权方自动续登已交付；运行时使用独立Python协议适配器，不调用网眼/嘉书CLI或共享其账号文件。账号身份保留迁移、自动续登和本地浏览器证据见[账号与续登验收](validation/2026-09-10-account-session-integration.md)，该基线 `a176fd8` 的 [CI 34385153117](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34385153117) 七组通过。

本轮继续实现[独立R2上传计划](superpowers/plans/2026-09-10-r2-batch-video-upload-plan.md)：Task1～7代码已形成，Task8已具备离线浏览器、完整业务链、10k/20k元数据和1,000文件故障验收；最终集成回归与本地更新记录在下节。真实R2/TikTok、CORS、当前媒体域名、生产prefork和日吞吐仍按[运行手册](runbooks/r2-video-upload.md)验收，新导入、自动清理默认均关闭。

| 范围 | 当前行为与证据 | 主要提交 |
| --- | --- | --- |
| 临时对象和预算 | 对象代次不可变；tenant/BC隔离；全局8GiB、租户2GiB为应用暂存窗口，预留和已存不重复相加；清理证据释放一次 | 83adf1e, 3beba10, 0b2cdc1 |
| 导入API与恢复 | 最大20k文件；≤200一批，≤100明细分页；稳定请求回执、发送前持久意图、响应丢失只读回查 | d521929, 7bc1912, eacc3a0, 8642e78 |
| 浏览器批量上传 | 4文件×2分片有界传输；IndexedDB保存恢复元数据；暂停、重选核对、跨范围停止；上传不绑定剧目 | 7582056, 9696fb5, 5dc4135 |
| 分片权限及取消 | 每次权限独立身份；completed实际回查、unused受控浏览器声明、unknown保留；实际签名期限不越过台账；取消竞态不能继续Complete | 301a46f, 2d199cb |
| 校验与来源入库 | 流式MD5/SHA256与ffprobe；事务outbox交接；合法来源公平分流；官方SDK URL上传和强回读，记录实际广告账户 | 0b2cdc1, b745e2e, 2df5e85, 2a440ba |
| 删除后素材使用 | 即时授权TikTok URL接力、新目标实际VID/封面、只读预览；新代次不退回文件上传 | e005bd1, 02c0ee8 |
| 自动清理与回收 | 源强回读成功后即安排删除；exact代次Delete/HEAD、Abort/ListParts/HEAD；消费者或结果未知保留预算；定时100项扫描与审计去重 | 8186adc, bc1c9ba, 76cd455, 856b83f, ce0e12f |
| 部署与验收 | 默认零网络检查；显式probe仅自建key；真实API浏览器3文件/4PUT、6条完整业务链、1,000文件400次清理；独立审查通过 | 20d8fed, dc3f5c7, 304a231, 865158c |

本轮证据按边界区分：[10k/20k元数据](acceptance/r2-ingest.md)、[浏览器→真实API](acceptance/r2-browser.md)、[API→校验→源→清理→目标](acceptance/r2-pipeline.md)。浏览器完整上传3个文件、16,782,295字节，终点是stored及3个待发校验任务；后端6条完整链使用真实数据库/校验/业务逻辑和外部传输替身，不冒称Cloudflare或TikTok实际成功。1,000文件混合故障中400个对象实际经清理worker回收，预算归零；它不是1,000次视频平台入库。

20,000文件选择测量：准备好FileList后16.4ms出现可用界面，DOM100行；合成FileList构造另用1,568.1ms，不能称整个选择过程小于1秒。JS堆为离散样本，未测真实峰值、Worker RSS、慢机P95或生产日量。

### 本轮最终集成验证

以下为合并工作树的实跑证据，外部服务均为传输替身；不沿用账号基线的通过数字。

- 完整后端模块（不含独立acceptance目录）：**1,687 passed、1 skipped，730.06秒**。唯一skip为已有Linux prefork测试；该次启动于 `92cf783`，随后SQLModel类型修复另有34项回归；没有把中途改动冒称已被同一次旧进程载入。
- 最终 `742f94a` 的源选择/源上传、分片权限回归、新validator进程测试、R2 probe/capacity以及6条全链/1,000故障批次联合运行：**60 passed、1 skipped，41.10秒**。skip为新增Linux实际硬终止测试，本机只运行其PG用途归属companion；实际Linux结果由CI提供。
- 前端TypeScript和生产构建通过；完整workspace **310 passed，1.8分钟**。首轮309通过/1失败为退出登录导航时读取旧JS上下文；`d29c699` 等待实际登录导航，保留全部停止传输/不再Complete/清除登录断言，连续5次通过后重跑上述全套。
- 合并分支真实浏览器→FastAPI/JWT→loopback HTTPS R2边界：**1 passed，38.8秒**（场景10.1秒），3文件/4PUT/16,782,295字节，3个stored和3个待发校验outbox，刷新零重传，哈希一致。该测试没有启动素材worker。
- 全量Ruff、Ty无诊断通过，Alembic check无新差异。Compose缺失的11个新R2环境字段已在 `153e704` 补齐，独立CLI实际渲染默认/覆盖×本地/预发布四种配置，API/Worker/Beat/prestart全部一致；未启动容器。
- 代码提交 `153e7045eb1e25fd28a9f9bf0bc75b53aee76118` 的 [CI 34429426415](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34429426415) 包含7个独立任务和新增实际API浏览器验收。链接对应确切代码版本，结果以该运行记录为准；文档追加不替代代码验证。

集成中修复了校验初次投递继承未来时间、签名实际期限超台账、取消与Complete竞态、放弃扫描重复告警，以及离线任务调度器缺失真实prefork上下文等问题。Linux validator测试使用原生产handler/guard和真实独占PG/Redis，3秒测试硬期限杀死子进程后检查旧用途保留；它不证明源上传UNKNOWN全链故障或进程被杀后的临时磁盘回收。

### 主目录与本地应用更新

代码已快进到实际目录 `projects/tiktok-ad-automation/`，主目录和隔离开发分支的代码提交同为 `153e704`。原15份路径整理文档先单独提交为 `aedeacf` 再合并，未覆盖；原有未跟踪 `docs/design-history/` 仍保留。后续文档提交继续同步两个分支。

对实际开发库做私有备份，先恢复到新建独占 `_test` 库，执行三次新迁移和Alembic check，核对用户/密码哈希/租户/成员/策略/已有连接与素材记录保持一致。之后仅停止已登记且工作目录匹配的API、Worker、单Beat，再做最终备份；实际库升级至 `r2_part_receipts`，原记录再次核对一致。测试恢复库已删除，业务数据库和Redis保留。

从主目录重建前端并重启 `http://127.0.0.1:8011`；健康、静态登录、回调业务错误和未知API边界检查通过。用户可见浏览器确认已有admin会话可读用户管理，新增用户表单无邮箱；进入原租户后，原策略v2仍显示USD120、ROAS1.08、10素材/组、3创意，版权方独立凭据表单及自动恢复提示可用。未创建测试业务数据、未验证版权方凭据、未请求TikTok授权或写广告。

本地配置选择R2，两个部署开关保持false，尚未提供实际桶/凭据或TikTok BC。macOS Worker仍为solo基础诊断，素材生产任务需要Linux prefork。因此当前本地页面可访问、数据已保留，不代表本机可以完成真实视频入库与投放。实际部署条件继续按运行手册处理。

### 2026-09-09交付基线（历史）

2026-09-09：用户指定仓库 `feat/platform-implementation` 的 P01～P06 功能与页面已完成；P07 本地业务、浏览器、容量、故障与备份恢复验收已完成。最终代码包括网眼/嘉书持久取链、共享 Scene 准备、目标封面准备和发送前检查、大批汇总与恢复计数优化。没有执行真实广告操作。服务器、域名、App 与租户授权后的外部验收仍按单独清单推进。

最新测试与截图见[离线验收](acceptance/offline.md)、[功能页面对照](acceptance/functional-delivery.md)及[容量记录](acceptance/capacity.md)。下表及后续按阶段保留历史提交，不能用历史计数替代最新提交的验证。

用户实际使用后补充验收：代码已迁入当前工作区的 `tiktok-ad-automation/`；修正平台菜单点击返回原页，以及已在租户内却提示“尚未接入租户”的问题。`35a033b` 的完整工作区回归 260 项通过，`813e119` 补充空白新建策略回归；实际本地浏览器已完成策略创建、编辑与刷新回读。目录迁移、页面验证和运行限制见[首次使用验收](validation/local-usability.md)。这次反馈说明原自动化验收对首次使用路径的覆盖不足，不应把“本地模块通过”表述为“当前环境可以正常完成真实投放”。

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

集成 `d6684c1` 的后端模块回归（不含单独运行的 `tests/acceptance`）为 **1,224 passed、1 skipped**，211.72 秒。跳过项为 macOS 不支持的真实 prefork 测试；该测试已在 Linux CI 实证通过。P06 UI 的生产静态工作区运行 **239 passed**，一个仅在开发服务器提供的输入 fixture 测试另行通过；不能把这两次运行合称一次 240 项全绿。SDK/接口替身不代表真实平台联调通过。

- `1660e86` 的 [Platform CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34297735517) 后端、前端和镜像三个任务全部成功。`d6684c1` 的后续 CI 曾发现测试调度器缺少 Beat 推进、合法任务数量超过单轮测试上限等问题，已补入实际控制任务和有界分块；当前精确提交状态见顶部离线验收链接。
- `d6684c1` 的嘉书完整验收成功：真实模块准备、Scene 获取、冻结、提交、138 次目标视频上传与回读、138 次目标封面上传与回读，最终 6 Campaign / 18 Ad Group / 36 Ad，全部 ENABLE，预算 600 USD。外部传输为严格替身，业务模块与 PostgreSQL、Redis、outbox 使用真实实现。
- 100,000 账户目录与能力准备、200 剧 × 1,000 目标账户的完整冻结计划及 10,200,000 执行步骤已实测完成；原汇总查询缺陷及备份副本接续验证过程保留在容量报告中。
- 最新 `0014_recovery_candidates` 数据库已重新完成真实 `pg_dump` / `pg_restore` 演练；合成凭据解密、对象元数据与待发任务保持一致，没有启动消费者。

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

本地工程与部署包已形成，后续按真实联调清单接入部署环境和租户授权。发布时使用对应提交通过的 CI 与同版本镜像，外部试投和实际消耗分别记录。以下条目保留各阶段当时的验证记录，以本页顶部当前状态为准。

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

## 2026-09-09：搭建执行与任务查询

- 冻结预览独立审查通过；批量输入、原输入完整分页、分组编辑回执、按剧聚合、预算守恒、预览提交与未知响应回查均已接实际 API，UI 集成 `c29710a`。
- 提交与分批展开 `7761232`：同一预览永久只提交一次，同一草稿的历史组合预留不重复创建；账户×剧目展开及步骤持久化均有界。GET 不触发 SDK 或新任务。
- BC 能力目录版本 `ac9bc53` / `a97a21b`：真实 PostgreSQL 验证 5,000 账户后，能力读取按目录版本主键查询，不再逐次扫描整个 BC；目录插入、移动、删除与并发事务均有版本围栏。
- 执行状态、准入与 SDK 创建 `315fe06` / `b1cfde8` / `a1b8764`：请求意图提交后才调用 SDK，已知 ID 及时持久化；Campaign/Ad Group/Ad 直接 ENABLE，SP 共用目标 VID/封面且文案不同。
- 独立审查修复 `7689b26`：CTA 放在官方 `ad_configuration.call_to_action_id`；已收到远端 ID 但 PostgreSQL commit 失败时，在新的安全事务中保留 `LATE_CREATED` 证据；Redis 暂不可用保留未发送步骤重排。文案 100 字符是明确的应用策略。
- 官方 SDK 回读 `771933a`：分页完整性、同名歧义、实际父级与目标素材、CTA、预算、ROAS、链接、文案与 ENABLE 状态均核对；找不到或无法唯一匹配保持 UNKNOWN。
- 任务查询 `ec483bd` / `9ca6d13`：列表与分层详情均分页，组内素材显示已核实的目标 VID/封面，事件仅暴露白名单；版权方/策略绑定来自冻结预览。
- 队列执行 `7dddbe4`：unit→step 锁序、结果与下一步 outbox 同事务、当前消息修复与业务退避分开；完整成功、远端成功丢响应后回读续建、回读为空停止自动重试已通过 10 条专项测试。
- Linux 进程故障测试 `a842358` 已提交，运行状态独立记录；当前新增 Scene 编排、recovery 与 UI 不在上述已通过范围中。
## P06 recovery backend — 2026-09-09

- Added permanent tenant-scoped retry/reconcile request receipts and separate mutable recovery progress (`0010_submission_recovery`); immutable intent triggers and preview/BC composite foreign keys. Added per-step evidence index (`0011_recovery_evidence_index`).
- Added 100-step durable recovery scanning, caller/original-actor separation, current account gates, dependency-aware definitely-unsent retry, existing readback queue reuse, exact current-generation outbox repair, and SQL-derived recovery counts/buttons. Historical receipt GET remains QUEUED/0 and read-only; job COMPLETED means scan scheduling finished.
- MATERIAL reconciliation now carries strict `read_only` through the original distribution verifier and all continuations. Delayed failure cannot select a new upload; successful original receipts can revalidate their existing VID. Original frozen intent and actual source history remain intact.
- Verified on isolated PostgreSQL and actual Redis with official SDK transport doubles: 476 builds/materials regressions passed; 38 focused recovery/SDK checks passed; final 20 boundary/SDK checks passed after the last strict-read changes. Ruff, mypy, ty, Alembic upgrade/check, and diff whitespace checks passed. No real provider/TikTok/S3 operations.
- Integration: include `builds.recovery_api.router`; include `app.modules.builds.recovery_tasks` in Celery; register periodic `builds.repair_recoveries` on control; map `recovery_no_candidates` to HTTP 409. Root owns bounded terminal material-result synchronization and shared API/client registration. Details: `docs/contracts/submission-recovery.md`.

## 2026-09-09：工作区目录整理

- 应用迁入 `projects/tiktok-ad-automation/`；工作区根目录旧入口保留为兼容符号链接，供现有虚拟环境与运行进程使用。
- 修复并验证 51 个 Git worktree 的访问路径，同步应用文档中的工作区绝对路径和兄弟工具路径。业务实现未改动。
- 根目录旧设计计划整体归档至工作区 `archive/workspace-design-20260909/`；应用内设计与实施进度继续作为当前开发入口。
- 验证：Git worktree 访问、`git diff --check`、Python 虚拟环境可执行文件通过；工作区批处理、VID 接力、骏伯监控离线回归通过。未运行应用全量测试，未调用外部广告接口或发送消息。
