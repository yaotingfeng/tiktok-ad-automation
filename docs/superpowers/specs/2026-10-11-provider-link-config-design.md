# 版权方取链参数对齐设计

**目标**：让搭建页的「自动取链参数」与各版权方后台、`projects/*-drama-link-tool` CLI 的实际建链行为一致。大多数搭建不再需要填写参数；长期默认值放在版权方连接的推广应用上，随剧变化的值按剧自动取，搭建页只保留可收起的「本次单独调整」。

## 现状问题

参数契约在 `backend/app/modules/providers/capabilities.py`，前端 `ProviderLinkConfigFields` 原样渲染，整批剧共用一份 `link_config`。与 CLI/后台对比后的问题：

| 版权方 | 问题 |
| --- | --- |
| 网眼 | 界面有「推广名称」，但后端只接受 `episode`，填了即报 `config_unverifiable`；后端又强制传 `ytf-<effect id>` 作为 `promote_name`，CLI 不传、由网眼自动生成。 |
| 兑吧 | 卡点集默认 1；CLI 与后台默认取剧自身 `freeEpisodeCount`。 |
| 刚刚好 | 免费集数默认 1；CLI 默认取剧自身 `freeEpisodeCount`（缺失时 3）。支付模板是无选项的 select，必填条件依赖前端拿不到的 `delivery_mode`。 |
| 容量 | 「剧集」要求填写内部 `episodicDramaId`，整批共用无意义；「别名」整批共用会让所有剧的计划名剧名部分相同，留空时后端传 `""`，而后台网页默认取剧自己的别名/原名。 |

## 证据

- 容量后台 `TT小程序链接管理`（`src/views/placement/link/index.vue`，2026-10-11 读取线上前端包）：新增弹窗只显示「所属小程序、投放平台、合集ID」。选择合集后网页自动设置 `episodicDramaId = link/episodic_dramas 第一项`、`compilationsAlias = aliasList[0] || originalTitle`；`handleClientChange` 固定 `deliverPlatform=1, deliveryType=1, linkType=2`。网页从不传第一集以外的剧集。
- 网眼 CLI `createPromoteLink` 请求体仅 `{app, drama_id, chapter_index, promote_platform}`。
- 兑吧 CLI `cardPointEpisode` 默认 `drama.freeEpisodeCount`；刚刚好 CLI `free` 默认 `drama.freeEpisodeCount ?? 3`，支付模板来自 `GET {portal}/payment-templates?authorizerAppId=`。

## 参数归属

三层，优先级：本次单独调整 > 按剧自动 > 应用默认 > 系统默认。

| 版权方 | 推广应用默认值（连接设置） | 按剧自动 | 搭建页本次调整 | 内部固定、不展示 |
| --- | --- | --- | --- | --- |
| 嘉书 | 起播集（1） | — | 起播集 | channel、vid |
| 网眼 | 起播集（1） | 推广名称由网眼生成 | 起播集 | app、platform=tiktok |
| 兑吧 | 起播集（1） | 卡点集 = 剧 `freeEpisodeCount` | 起播集、卡点集 | 小程序 = 所选应用、集 lc ID |
| 刚刚好 | 跳转集（1）、支付模板 | 免费集数 = 剧 `freeEpisodeCount`，缺失为 3 | 免费集数、跳转集、支付模板 | authorizerAppId、seriesId |
| 容量 | — | 剧集 = 第一项、别名 = `aliasList[0] \|\| originalTitle` | 无 | clientId、平台 1、交付 1、链路 2 |

规则：

- 本次调整只填写的字段覆盖下层，未填写的字段继续逐剧解析；整批统一值只来自用户显式填写。
- 按剧自动值在取链流程「剧目解析后、查重前」逐剧计算，冻结进该剧的 effective config（与现有 `duiba_config`/`ggh_config` 同位置），之后查重、建链、核验均使用冻结值。
- 刚刚好校验沿用 CLI/后台：免费集数 0～总集数；免费集数为 0 时跳转集只能 1；跳转集 ≤ max(免费集数, 1)。按剧自动值与统一跳转集冲突时，该剧进入 `config_conflict` 并提示原因，不静默改值。
- 兑吧卡点集/起播集不在该剧剧集范围内时沿用现有 `provider_request_invalid`。

## 组件改动

### 后端

1. **应用默认值**：`ProviderApplication` 增加 `link_defaults`（JSONB，默认 `{}`），与版权方发现的 `channel_config` 分开，重新发现应用不得覆盖。新增 `PATCH /connections/{connection_id}/applications/{application_id}/link-defaults`，按 capabilities 契约校验字段与类型，需取链管理权限并保持租户隔离。
2. **capabilities 契约**：`capabilities_for_kind` 返回每个字段的 `source`（`application_default` / `per_drama` / `override_only`）、应用默认值和按剧规则说明，移除网眼 `promote_name`、容量 `episodic_drama_id`/`alias`。刚刚好支付模板字段的 `options` 由新接口提供，`required` 直接按应用 `delivery_mode`（iap/mixed）计算并下发，不再依赖前端判断。
3. **刚刚好支付模板**：adapter 增加 `payment_templates(authorizer_app_id)`，对应只读 `GET /payment-templates?authorizerAppId=`；路由 `GET .../applications/{id}/payment-templates` 返回 `{id, name}` 列表。
4. **按剧自动值**：兑吧、刚刚好 adapter 的剧目候选/详情读取剧自身 `freeEpisodeCount`（兑吧取剧单行，刚刚好取剧单或 `series_detail`），在 `duiba_preview` / `ggh_detail` 阶段合并到 effective config。容量 `rl_detail` 阶段调用 `link/compilations_alias`，按网页规则填 `alias`；剧集固定第一项，不再读取用户值。
5. **网眼去自定义名称**：删除 `ytf-` 命名、`promote_name` 写入与基于名称的恢复匹配。超时恢复改为 ID 差集：
   - 建链前完成查重时，已经通过 `_observed` 记录全部同参数（app＋剧＋集＋tiktok）链接 ID；
   - 写入结果未知时重新完整扫描，新增 ID 恰为 1 条即采用并进入 verify；0 条判定未建成，按现有可重试路径重试；≥2 条转 `result_unknown` 人工核实，不自动新建；
   - 远端范围锁（`claim_remote_scope`）保证同租户同参数不会并发建链，扫描不完整时与现在一样禁止新建。
6. **合并顺序**：搭建草稿提交时由服务端计算「应用默认 ⊕ 本次调整」并写入 `LinkPreparation.config`，按剧自动值仍在取链步骤内逐剧计算。

### 前端

- **版权方连接 → 已发现应用**：每个应用增加「取链默认值」编辑，字段由 capabilities 驱动；刚刚好支付模板为下拉。
- **广告搭建页**：「自动取链参数」卡片改为只读摘要，显示每个字段的生效来源，例如“起播集 1（应用默认）”“卡点集：按剧免费集数”。「本次单独调整」默认收起，展开后只显示 `override` 允许的字段，留空即不覆盖。容量显示“参数与版权方后台一致，无需设置”。
- 刚刚好付费/混合应用若应用默认与本次调整都没有支付模板，提交前提示必填，指引去连接设置。

## 数据迁移

- Alembic 迁移新增 `provider_application.link_defaults`。
- 未提交草稿（`BuildDraft.status = 'DRAFT'`）的 `link_config` 删除已废弃键（网眼 `promote_name`、容量 `episodic_drama_id`、`alias`），并把与系统默认值相同的旧默认（例如兑吧 `card_point_episode=1`、刚刚好 `free_episode_count=1`，以及嘉书/网眼/兑吧 `episode=1`、刚刚好 `episode_seq=1`）移除，使其改走按剧自动或应用默认。已开始准备或已完成的草稿与 `LinkPreparation`、`PromotionLink` 历史记录保持原样。
- 不保留旧参数兼容分支：新代码拒绝已废弃键。

## 错误处理

- 应用默认值保存时校验失败返回字段级错误；版权方不可达时支付模板列表返回可重试错误，不缓存空列表。
- 按剧自动值缺失：兑吧剧集预览缺少免费集数时回退为起播集（与 CLI 一致）；刚刚好回退 3。

## 测试

- `capabilities`：五家字段、来源、必填计算的单元测试。
- 合并优先级：应用默认、本次调整、按剧自动的组合用例；刚刚好校验边界（免费 0、跳转超范围、冲突进入 `config_conflict`）。
- 兑吧/刚刚好：不同剧免费集数不同时，同批生成的 effective config 各自正确。
- 容量：别名取 `aliasList[0]`、无别名取原名；忽略用户传入的剧集/别名键（拒绝）。
- 网眼恢复：新增 ID 0/1/≥2 三种情况、扫描不完整、并发锁；请求体不含 `promote_name`。
- 迁移：草稿清理只影响 `DRAFT`，历史准备记录不变。
- 前端：应用默认值编辑、搭建页摘要与本次调整、刚刚好支付模板下拉与必填提示（Playwright）。
- 开发期不对真实版权方后台执行建链；外部调用在传输层使用替身。

## 不在范围内

- 容量非第一集起播（后台网页未开放，行为未经验证）。
- 按剧分别设置不同参数（同一批仍为统一覆盖 + 按剧自动）。
- 其他手动版权方（`other`）流程。
