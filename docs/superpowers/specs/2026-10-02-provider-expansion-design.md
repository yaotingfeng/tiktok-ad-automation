# 版权方来源扩展设计：兑吧、刚刚好、容量

**目标**：在 TK-ADA 的版权方连接、剧目解析、推广链接准备和投放搭建链路中增加兑吧、刚刚好、容量，同时保持租户隔离、可恢复写入和已有网眼/嘉书行为不变。

## 现状与边界

- 现有正式接入是 Python adapter：`backend/app/modules/providers/adapters/jiashu.py` 和 `wangyan.py`。
- `projects/*-drama-link-tool` 是协议证据和人工 CLI，不作为后端运行时依赖；后端不得 shell out、读取 CLI 的本地账号文件或复用账号缓存。
- 连接凭据必须继续通过 `encrypt_credentials` 加密保存；访问令牌/会话只写入加密凭据，不写日志、公开 API 或 `channel_config`。
- 推广链接的技术身份、显示编号、归因基础名和 TikTok Mini 目标保持分离；不能把版权方 app ID 直接当成 `tiktok_minis_id`。

## 稳定 provider kind

| kind | 展示名 | 凭据字段 | 运行时账号锚点 |
| --- | --- | --- | --- |
| `wangyan` | 网眼 | `email`, `password` | `x-ds-admin-token` |
| `jiashu` | 嘉书 | `username`, `password` | `session` |
| `duiba` | 兑吧 | `account`, `password` | JWT `token` |
| `gangganhao` | 刚刚好 | `portal_id`, `username`, `password` | portal JWT |
| `rongliang` | 容量 | `email`, `password` | `dist_token` cookie |

`other` 继续作为手动版权方的内部 kind，不出现在自动连接创建的 `ProviderKind` 中。

## 统一 adapter 契约

在现有 `ProviderClient` 之上增加显式能力契约，避免在 `link_steps.py` 继续堆叠按 kind 的分支：

- `login(http, credentials) -> AuthenticatedClient`：返回客户端和更新后的凭据。
- `discover_applications() -> list[ApplicationDescriptor]`：返回外部应用 ID、名称、可选的公开配置和可选 Minis ID。
- `search(title, cursor) -> SearchPage`：游标必须能证明分页是否完整，候选只保留精确标题。
- `lookup_link(drama, config, cursor) -> LinkLookupPage`：返回已有链接、下一游标和 `complete`；不能证明历史完整时禁止新建。
- `create_link(drama, config) -> LinkReceipt`：声明是否幂等、远端 ID、URL、归因和规范化配置。
- `read_link(remote_id, context) -> LinkReceipt`：未知写入只允许通过只读回查恢复。
- `verify_link(receipt, drama, config) -> VerifiedLink`：由 adapter 验证远端 ID、剧目、参数、URL 和归因。
- `capabilities(application) -> LinkConfigSchema`：返回投放页需要的配置字段、默认值、枚举和必填条件。

保留现有 durable effect、remote scope、claim token 和一请求一步骤的事务边界。对已有网眼/嘉书先包一层兼容 adapter，再逐步移除 orchestration 中的 `if kind == ...`。

## 三个新渠道的协议映射

### 兑吧（`duiba`）

- 登录：`POST /api/dist/auth/login`，请求 `account/password`，响应 JWT；401 自动重新登录。
- 应用发现：`GET /api/dist/miniapp/myList`；应用外部 ID 使用 `miniapp.id`，额外国家/语种写入 `channel_config`，不冒充 Minis ID。
- 剧目搜索：`GET /api/dist/drama/page`；详情和集数使用 `/drama/preview`，可提链应用使用 `/drama/copy-miniapps`。
- 链接参数规范化为 `episode`、`card_point_episode`、`miniapp_id`，同时保存对应集的 `lc671EpisodeId`。
- 复用优先调用 `/link/page`；该接口当前可能持续 500，因此 adapter 必须声明“查询不可用时可安全幂等建链”。未知 POST 不做无条件重放，只有在固定请求摘要和兑吧已核实的幂等契约下才允许恢复性重试；否则进入 `result_unknown`。
- `/link/create` 返回 `minisLink` 和 link 标识时直接回读验证；URL 没有独立归因基础名时 `protected_base` 使用空字符串并把原始 linkNo/参数放入 `attribution`，不伪造广告命名。

### 刚刚好（`gangganhao`）

- 登录：`POST /portal/distributor/login`，请求 `portal_id/name/password`，保存 JWT 及过期时间。
- 应用发现：`GET /portal/distributor/apps`；外部 ID 使用 `authorizerAppId`。
- 剧目锚点使用 `publishId`，标题、系列 ID、语种和变现模式来自 `/series`；详情使用 `/series/{publishId}`。
- 链接参数规范化为 `free_episode_count`、`episode_seq`、`payment_template_id`、可选 `name`。IAP/mixed 必须先读取并校验支付模板；IAA 不要求模板。
- 复用流程为 `/campaign-links` 列表 + `/campaign-links/{id}` 详情精确比较免费集、跳转集和模板；创建使用 `/campaign-link`；未知创建结果只能通过 link ID/列表详情核实。
- `minisLink` 作为 URL，`name/linkCode` 作为归因证据；不得把内容侧 `seriesId` 当成投放剧目技术 ID。

### 容量（`rongliang`）

- 登录：`POST /manage/ocean/management/distribution/auth/login` 表单 `email/password`，凭据更新保存 token；请求使用 `dist_token` cookie。
- 应用发现：`GET /management/link/form_data` 的 TikTok `packageOptions`；外部 ID 使用 `clientId`，平台默认固定 TikTok（`platform=1`）。
- 剧目锚点使用 `compilationsId`，标题优先 `originalTitle`；剧集用 `/link/episodic_dramas`，链接参数包含 `episodic_drama_id`、`client_id`、`platform`、`delivery_type=1`、`link_type=2`、可选 `alias`。
- 复用调用 `/management/link/page` 精确比较 client、合集、剧集和平台，再用 `/management/link/url?batchId=` 取得 `deepLink/planName/adGroupName`。
- 创建 `/management/link/create` 成功后必须重新查询列表再取 URL；任何列表缺失、详情缺失或 URL 不完整都进入 `result_unknown`，不能猜测最新批次。

## 数据库和 API 变化

- 新增 Alembic 迁移，扩展 `provider_connection.kind`、相关历史约束及任何只允许 `wangyan/jiashu` 的检查约束；不能改写旧迁移。
- `ProviderKind`、连接创建请求、应用公开类型、连接筛选和错误映射加入三个 kind；凭据字段按 kind 校验，错误响应不回显秘密。
- `ProviderApplication.channel_config` 只保存非敏感的协议配置和能力快照；增加能力/配置 schema 的公开读取字段，供投放页按应用渲染，而不是前端硬编码三套参数。
- `ResolvedLink`/`PromotionLink` 继续要求 ready 链接有 URL、验证时间和非空 `protected_base`（无归因渠道用空字符串）；`display_drama_id` 由 `drama_identity.py` 按 provider 规则生成。

## 投放页行为

连接管理新增三种类型及对应字段。应用选择仍只看所选连接，BC 不参与版权方连接筛选。选择应用后读取能力 schema：

- 兑吧显示起播集、卡点集和可选小程序提示；
- 刚刚好显示免费集、跳转集、支付模板（IAP/mixed 必选）；
- 容量显示合集剧集、平台/交付/链路默认值和可选别名。

保存草稿时把规范化 `link_config` 原样纳入幂等摘要；后端 adapter 再做严格校验。已有网眼/嘉书的默认字段和页面行为保持不变。

## 验收边界

先完成离线协议 fixture、workflow 恢复、租户隔离和 UI 契约测试；真实版权方认证、剧目查询和建链作为独立验收记录。没有用户明确提供的账号和具体联调授权时，不调用真实建链，不把连接状态标成已验收。
