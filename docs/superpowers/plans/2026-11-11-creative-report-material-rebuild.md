# 素材维度改用创意日报：改造计划

状态：已授权实施 · 2026-11-11；本轮包含开发、合并、推送、分阶段生产发布。

## 1. 目标

素材维度（素材页、广告详情素材列表、素材级自动规则）的消耗和收入，改为来自基础报表创意级日报（`AUCTION_AD` + `ad_id` × 天），通过本地目录里的创意直接归到视频 VID 或图片 `web_uri`。不再同步 Smart+ 素材拆分报表和概览报表，删掉帖子补链整条链路。

不改的部分：广告、广告组、系列、账户、剧维度继续由广告日报（`ad_id_v2`）汇总；小时数据、账户对账、操作日志不变。

## 2. 依据

证据见 `docs/validation/2026-11-10-material-id-er.md` 第 5、6 节，`docs/validation/2026-11-10-creative-level-report-verification.md`。

| 结论 | 数据 |
| --- | --- |
| 创意级合计 = 广告级 | 新骏伯前 10 账户 14 天 392 个广告组日：消耗 6,130.14 = 6,130.14，每个广告组日差 0；收入差 $0.01 |
| 创意级带完整收入 | `native_growth_*` 全部有值，与广告日报同口径；视频维度、创意报表、素材概览拿不到收入 |
| 创意能 100% 归到素材 | 432 个创意中 407 个在当前目录，25 个是已删除创意，本地 `ad_material_reference` 都有 VID |
| 轮播结构简单 | xuyin 223 个轮播创意都是 1 创意 1 图；没有平台“轮播组”ID |
| 已知缺口 | 素材重建后旧创意会从创意级报表消失：xuyin 一个账户 7 天少 3.8%（集中在较早日期，单日最多少 95%）；新骏伯样本没有出现 |
| 素材拆分不可靠 | 同账户同期拆分报表只有 $0.35 / $253.18；junbo 另一账户 $0.17 / $101.94 |

## 3. 已确认的决定

1. 两份事实：`ad_report_day` 保留为总账，所有非素材维度不变；`creative_report_day` 只给素材维度。
2. 两者按“租户 × 账户 × 广告组 × 当地日”逐指标对账。未关联创意独立保留；广告总账减全部创意事实的有符号差额单列，NULL 不当零，非完整或过期来源不宣称已确认缺口。
3. 不保留旧素材链路的回退代码（AGENTS.md：不兼容旧行为）。

**执行决定**：回补目标与广告日报现存日期一致、近35天优先；切换后旧表保留至少7天；本期仅展示差额，不自动追回。

## 4. 数据模型

### 4.1 新表 `ad_creative`（目录最下一层）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| tenant_id, advertiser_id, creative_id | PK | `creative_id` = `smart_plus_creative_id`；手动广告/旧 Smart+ 为广告 ID 本身 |
| ad_remote_id | str | 所属广告，FK → `ad_object`（kind=ad） |
| ad_material_id | str null | 广告内素材 ID |
| material_type | str | `VIDEO` / `IMAGE` / `CAROUSEL` / `TIKTOK_POST` / 未解析；多图整项记账 |
| platform_material_id | str null | 唯一素材身份；多图使用组合身份，另存完整有序 assets；未解析不伪造 VID |
| operation_status | str null | 平台素材状态 |
| deleted | bool | 仅完整且有版本保护的观察或显式平台状态可确认；部分读取未出现不是删除，不删历史行 |
| observed_at, published_version | | 与 `ad_object` 同语义 |

索引：`(tenant_id, platform_material_id, advertiser_id)` 供素材页按素材反查；`(tenant_id, advertiser_id, ad_remote_id)` 供广告详情。

来源：从目录发布阶段已规范化、通过完整性与版本保护的素材/创意证据写入；同时覆盖 `creative_list` 和普通创意目录补回数据。保存来源、首次/最近观测、冲突证据及完整 assets。回填不得覆盖并发新证据；身份冲突保持未解析，不用新身份重写旧归属。手动广告报表ID按官方语义作为创意键；多图保留组合整项金额，首图仅用于缩略图。

`ad_material_reference` 保留，仍负责“广告用了哪些素材”和本地文件关联；`creative_ids` 列删除，创意关系改由 `ad_creative` 承担。

### 4.2 新表 `creative_report_day`（创意日报，按月分区）

| 列 | 说明 |
| --- | --- |
| tenant_id, advertiser_id, stat_date, creative_id | PK；`creative_id` 存报表 `ad_id` |
| adgroup_id, campaign_id, campaign_automation_type | 报表属性列，用于对账与过滤 |
| 指标列 | 与 `ad_report_day` 同一套 `_Fact` 指标列和元数据（currency、timezone、fetched_at、sync_job_id） |

只存平台原始数据；归属在读模型投影时关联 `ad_creative` 得到。

### 4.3 读模型 `material_entity_day` 重建

发布1新增独立 `creative_material_entity_day` 影子投影，不写旧 `material_entity_day`。投影 LEFT JOIN `ad_creative`，保留未匹配创意行；已匹配金额按素材身份汇总，多图整项唯一记账。保留账户/广告/广告组/日期作用域以支持先过滤再聚合。另按广告组日逐指标计算广告总账减全部创意事实的有符号差额；缺值与来源未就绪单列状态，舍入容差不改原值。发布2素材查询只使用新投影，无旧源回退。

原 `row_key` 中的帖子键（`post`）不再产生。

### 4.4 删除

| 对象 | 说明 |
| --- | --- |
| 表 `material_report_day`、`material_post_link`、`material_link_scan` | 回补验收后删除（§11-2） |
| `ad_material_reference.creative_ids` | 改由 `ad_creative` |
| `current_ad_material` 中帖子相关的刷新语句 | 随读模型改写 |

## 5. 同步任务

| 任务 | 处理 |
| --- | --- |
| `ad_day` | 不变 |
| `creative_day` | **新增**。与 `ad_day` 同账户、同日期窗口、同调度桶一起规划；单账户请求，`AUCTION_AD` + `["ad_id","stat_time_day"]`，`ad_status=STATUS_ALL`，指标为 `AD_DAY_METRICS`（含 adgroup_id、campaign_id、campaign_automation_type）。行数超过分页上限时按系列分批，复用 `_read_campaign_batches` |
| `material_day` | 删除 |
| `material_links` | 删除；目录队列里的“补链触发的定向补读”一并删除 |
| `account_day`、`campaign_hour`、`operation_log` | 不变 |

广告事实、创意事实、创意身份三个来源的发布均使独立 creative 素材桶失效。记录源修订/观测时间，旧投影不得覆盖新输入；可比完整来源后才确认差额。发布1影子投影与旧源桶完全隔离，发布2切换前验收。

请求量估算：每账户每个日期窗口多 1 次（分页另计），取代现在的 `material_day` 1 次 + `material_links` 若干次。

## 6. 查询与功能

| 位置 | 改动 |
| --- | --- |
| 素材维度（`row_set._material_rows`、`material_query.py`） | 读新 `material_entity_day`；一行一个 VID 或图片；“未归属”单独一行。删除帖子搜索、`post_ids` |
| 广告详情素材（`ad_materials.py`、前端 `MaterialsTab.tsx`） | 按本广告的创意汇总，金额为本广告内精确值；删除“多广告共用，数据为合计”标记 |
| 素材级自动规则（`automation_rules/material_inputs.py`、`inputs.py`） | 窗口值改读 `creative_report_day` + `ad_creative`；执行仍定位到当前唯一 `ad_material_id`。旧帖子问题改为创意缺口/身份歧义/指标缺失/来源不同步等 judgement_issue。可能影响候选素材的缺口必须跳过执行并审计，不把未知当0；差额行不可操作 |
| 报表状态栏（`ReportStatusBar.tsx`） | 删除“素材 x 项待关联”；“素材账户超时”改按 `creative_day` 计算 |
| 健康与诊断（`health.py`、`diagnostics.py`、`freshness.py`） | 素材新鲜度改看 `creative_day` |
| D3/D30 收入 | 创意级有 D3/D30（与广告日报同字段），去掉 `MATERIAL_UNSUPPORTED` |

## 7. 删除的代码

`material_links.py`、`link_work.py`、`link_policy.py`、`spark_identity.py`、`material_identity.py`、`material_value.py`、`directory_refresh.py` 中补链定向补读部分、`read_models/material_identity.py`；`sdk_report_reader` 及 `contracts/report_reader.py` 的 `material_day`、`material_ad_links`、`material_ad_posts`、`material_videos`、`material_video_content`（调用方只有上面要删的补链模块）；对应测试。`carousel_links.py` 只做轮播说明与跳转，不参与金额，保留。

## 8. 迁移与回补顺序

1. **迁移 A（加表，不删）**：新建 `ad_creative`、`creative_report_day` 及分区。旧链路照常运行。
2. **发布 1**：目录同步开始写 `ad_creative`；新增 `creative_day` 任务。素材页仍读旧数据。
3. **目录回填**：从 `ad_object.configuration.creative_list` 本地回填 `ad_creative`（不调 API）；当前目录已没有、但 `ad_material_reference` 里有的创意置 `deleted=true`。
4. **创意日报回补**：按选定天数（§11-1）逐租户逐账户回补，走现有历史优先级队列，控制在现有 API 配额内。
5. **对账验收**（§9），通过后进入下一步。
6. **发布 2**：先停止旧任务规划/需求/派发，正常排空已领取任务及异步发布；其余精确旧类型的持久任务与需求有审计终结，不清队列、不伪造完成。切新投影/页面/规则，删除旧业务代码；旧任务回调只作退休确认，不能重启补链。非补链来源的定向目录读取保留。
7. **迁移 B（独立后续，稳定至少7天后）**：确认无旧代码/触发器引用后删除 `material_report_day`、`material_post_link`、`material_link_scan`、`creative_ids` 列。按 AGENTS.md 走停写、排空、备份、迁移流程。

发布 1 只加表加任务，可以单独上线观察；发布 2 和迁移 B 改变任务合同和表结构，按完整迁移发布流程。

## 9. 验收

全部为生产只读核对，结果记入 `docs/validation/` 新日期记录。

| 检查 | 通过标准 |
| --- | --- |
| 创意日报覆盖 | 每租户、每天：`creative_report_day` 消耗 / `ad_report_day` 消耗 ≥ 99%（近 7 天）；较早日期如实记录比例 |
| 广告组日对账 | 逐指标列出近7天正负差额与NULL、覆盖/时点差异，不预设根因；消耗与收入分别验收 |
| 创意 → 素材 | 有消耗的创意在 `ad_creative` 命中率 100%；未命中逐个列出 |
| 素材页合计 | 素材维度合计（含“未归属”行）= 广告维度合计 |
| 自动规则 | 5 条素材规则（当前全停用）在新数据上试算，输出与人工核对一致 |
| 积压 | 发布 2 后报表队列不再有 `material_links`；补链来源的目录定向补读归零；其他业务读取保留 |
| 请求量 | 每账户每轮报表请求数对比改造前，记录变化 |

## 10. 风险

1. **历史旧创意缺口**：重建前的旧创意在创意级报表里消失，素材维度只能显示为“未归属”。xuyin 样本里集中在较早日期。是否追回见 §11-3。
2. **删除旧表后不可回退**：迁移 B 后旧素材日报和帖子关联不再存在；迁移 B 前必须备份。
3. **轮播多图**：实测都是 1 图；若遇到1创意多图，保留整项金额和完整成员，首图仅作缩略图，不作为图片分项或规则经济事实。上线前在 xuyin 全量目录里再扫一次多图创意数量。
4. **非 Upgraded Smart+ 广告**：`ad_id` 维度对手动广告、旧 Smart+ 返回的是广告本身；`ad_creative` 要为这些广告各写一行（creative_id = 广告 ID），实施时需在一个有手动广告的账户上核对。
5. **分页与报表行数**：创意行数约为广告行数的 2 倍（新骏伯样本 117 / 66），大账户需按系列分批。

## 11. 本轮执行决定

1. **回补天数**：目标与广告日报现有日期窗口一致；先完成最近35天，再推进更早日期。平台无法提供的明细如实标覆盖缺口。
2. **旧表删除时点**：回补验收通过、发布2稳定运行至少7天后，独立执行迁移B；本轮不提前删除。
3. **“未归属”是否追回**：本期仅展示差额，无阈值自动补数。将来如需追回须独立设计来源互斥、身份与审计，不能承诺一次拆分请求补齐。

## 12. 工作拆分

| 序号 | 内容 | 产出 |
| --- | --- | --- |
| 1 | 迁移 A、`ad_creative` 写入、目录本地回填 | 迁移、目录同步改动、回填脚本、测试 |
| 2 | `creative_day` 读取、发布、规划、调度 | reader、publish、planner、schedule 改动与测试 |
| 3 | 发布 1 + 回补 + §9 前四项验收 | 验收记录 |
| 4 | 读模型投影、素材页、广告详情、前端 | 后端查询、前端改动、生成客户端 |
| 5 | 自动规则输入与数据问题 | 规则改动与测试 |
| 6 | 删除旧任务与代码 | 删除清单与测试 |
| 7 | 发布 2 + 迁移 B + §9 后三项验收 | 发布与验收记录 |

## 13. 必须覆盖的行为与发布合同

- 无映射创意仍计入总额；广告100、创意100、映射90时，素材90+未关联10，不生成虚假的总账差额。
- 消耗差0但收入差10、负差额、NULL、无数据、跨租户/账户同ID均测试；来源不完整/观测时点不可比时规则禁止执行。
- 发布1的影子数据不会覆盖旧页面；广告/创意/身份任一源更新均重建投影，旧修订不能覆盖新结果。
- 目录不完整/旧版本不标删除；有删除证据仍保留历史身份；冲突不任取素材；多图整笔金额只计算一次。
- 创意读取分页超限拆分必须调用creative reader，不得复用硬编码ad_day的回调；能力与租户/BC冻结路由保持。
- 迁移A同样属于DDL，按选定完整迁移发布流程执行；原部署四开关、资源和配额保持，不引入业务开关。
- 最终验收同时包含本地真实PostgreSQL/Redis行为测试、前端构建与相关浏览器验收，以及生产只读对账；生产自动规则仅试算，不擅自启用或操作真实广告。
