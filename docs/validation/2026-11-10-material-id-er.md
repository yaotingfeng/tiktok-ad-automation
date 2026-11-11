# 素材报表 ID 关联 ER 图与同步流程

依据：官方文档（Get Upgraded Smart+ Ads、Creative Overview/Breakdown、Basic Reports 维度）与 2026-11-10 生产只读核验。2026-11-11 已补入最新批准目标（§7）及完整官方文档调查的口径修正（§8）。实线 = 官方接口直接返回的字段关联；标”定位”的是我们靠筛选推断出来的归属，不是平台返回的字段。

## 报表类型说明

| 缩写 | 全称 | 中文说明 | 数据粒度 |
| --- | --- | --- | --- |
| RPT_AD_V2 | Basic Report (ad_id_v2) | 基础报表广告维度 | 整条广告汇总，按天 |
| RPT_CREATIVE | Basic Report (ad_id) | 基础报表创意维度 | 创意级明细，按天 |
| RPT_OVERVIEW | Material Overview | 素材概览报表 | 广告层级×组件分组，不按天；支持收入，具体指标有限制 |
| RPT_BREAKDOWN | Material Breakdown | 素材拆分报表 | 主素材或设置组件组合；支持小时/天/周/月，带 onsite 等收入 |
| RPT_VIDEO | Basic Report (video_material_id) | 基础报表视频素材维度 | 按MID汇总，受专门的指标与组合限制 |
| RPT_CREATIVE_API | Video Insights (/creative/report/get/) | 视频洞察报表 | 以视频为行，跨账户/区间汇总，含付费、自然及获得的页面互动 |

## 1. 平台对象与报表行 ER 图

```mermaid
erDiagram
    CAMPAIGN ||--o{ ADGROUP : "campaign_id"
    ADGROUP ||--o{ SMART_AD : "adgroup_id"
    SMART_AD ||--o{ CREATIVE : "creative_list"
    VIDEO ||--o{ CREATIVE : "video_info.video_id"
    IMAGE }o--o{ CREATIVE : "image_info (轮播)"
    CREATIVE ||--o| POST : "Spark 时平台发帖，目录不返回帖子ID"

    CAMPAIGN {
        string campaign_id PK
    }
    ADGROUP {
        string adgroup_id PK
        string campaign_id FK
    }
    SMART_AD {
        string smart_plus_ad_id PK "基础报表叫 ad_id_v2"
        string adgroup_id FK
    }
    CREATIVE {
        string smart_plus_creative_id PK "= 基础报表 ad_id"
        string ad_material_id UK "广告内素材ID，重建会换"
        string smart_plus_ad_id FK
        string video_id FK "轮播为空"
        string tiktok_item_id "实测 1217 个视频创意全空"
    }
    VIDEO {
        string video_id PK "VID"
        string material_id UK "MID，与 VID 成对返回"
    }
    IMAGE {
        string web_uri PK
    }
    POST {
        string post_id PK "Spark 报表的 main_material_id"
    }
```

```mermaid
erDiagram
    SMART_AD ||--o{ RPT_AD_V2 : "ad_id_v2"
    CREATIVE ||--o{ RPT_CREATIVE : "ad_id = smart_plus_creative_id"
    SMART_AD ||--o{ RPT_OVERVIEW : "smart_plus_ad_id"
    CREATIVE ||--o{ RPT_OVERVIEW : "ad_material_id"
    POST ||--o{ RPT_OVERVIEW : "main_material_id"
    POST ||--o{ RPT_BREAKDOWN : "main_material_id (Spark)"
    VIDEO ||--o{ RPT_BREAKDOWN : "main_material_id = MID (非 Spark)"

    RPT_AD_V2 {
        string ad_id_v2 FK "整条广告"
        date stat_time_day
        decimal spend
        decimal revenue
    }
    RPT_CREATIVE {
        string ad_id FK "创意级，我们未使用"
        date stat_time_day
        decimal spend
        decimal revenue
    }
    RPT_OVERVIEW {
        string smart_plus_ad_id FK
        string main_material_id FK
        string ad_material_id FK
        decimal spend "区间汇总；支持多类收入"
    }
    RPT_BREAKDOWN {
        string main_material_id FK "无广告/创意/视频ID"
        string main_material_type
        date stat_time_day
        decimal spend
        decimal revenue
    }
```

## 2. 本地表 ER 图

```mermaid
erDiagram
    ad_object ||--o{ ad_material_reference : "ad_remote_id"
    ad_object ||--o{ ad_report_day : "ad_id (= ad_id_v2)"
    material_report_day }o--o{ material_post_link : "main_material_id + type"
    material_post_link }o--|| ad_object : "ad_remote_id (概览或定位得出)"
    material_post_link }o--o| ad_material_reference : "ad_material_id"

    ad_object {
        string remote_id PK "广告/组/系列"
        string kind
        string parent_remote_id
    }
    ad_material_reference {
        string ad_remote_id FK
        string ad_material_id
        jsonb creative_ids "smart_plus_creative_id"
        string platform_material_id "VID 或图片 web_uri"
        string material_type "VIDEO / IMAGE"
    }
    ad_report_day {
        string ad_id FK "存的是 ad_id_v2"
        date stat_date
        decimal spend
    }
    material_report_day {
        string main_material_id "帖子ID 或 MID"
        string main_material_type
        date stat_date
        decimal spend
    }
    material_post_link {
        string main_material_id PK
        string main_material_type PK
        string ad_remote_id "所属广告"
        string ad_material_id "桥接到目录"
        string platform_material_id "最终 VID"
    }
```

## 3. 素材报表同步流程

```mermaid
flowchart TD
    BEAT["Beat 周期规划"] --> MD["material_day 任务"]
    MD -->|"拆分报表 main_material_id + 天"| MRD[("material_report_day")]
    MRD --> NEED{"有消耗/收入的帖子缺完整关联?"}
    NEED -->|否| DONE["素材维度可直接查询"]
    NEED -->|是| ML["material_links 任务（补链）"]

    ML --> NS["非 Spark：MID 查素材库<br/>官方成对返回 MID+VID"]
    NS --> LINK[("material_post_link")]

    ML --> OV["Spark：概览报表<br/>smart_plus_ad_id × main_material_id"]
    OV -->|"查到：拿到所属广告 + ad_material_id"| LINK
    OV -->|"查不到（旧帖子）"| LOC["按广告筛选拆分报表定位<br/>每批 ≤200 广告、二分、翻页<br/>历史累计均值约14次请求/帖，非固定次数"]
    LOC -->|"单广告命中"| LINK

    LINK -->|"只知所属广告、缺 ad_material_id 或 VID"| DIR["定向目录补读（含已删除）<br/>ad_directory_run"]
    DIR --> AMR[("ad_material_reference")]
    AMR -->|"ad_material_id 对上 → 写 VID"| LINK

    LINK --> CHK{"仍有缺口?"}
    CHK -->|否| DONE
    CHK -->|是| WAIT["WAITING 退避<br/>10 分钟 → 最长 6 小时后重查"]
    WAIT --> ML

    DSYNC["周期目录同步<br/>/smart_plus/ad/get/"] --> AMR

    classDef cost fill:#fde2e2,stroke:#c0392b,color:#000
    class LOC,DIR,WAIT cost
```

红色节点是积压来源：旧帖子定位（请求最多）、定向目录补读（占目录队列约 55%）、等待退避重查（与目录队列互相等待）。

## 4. 哪些关联是官方直接给的

| 关联 | 来源 | 官方直接返回 |
| --- | --- | --- |
| 创意 → 广告、视频、`ad_material_id` | `/smart_plus/ad/get/` 的 `creative_list` | 是 |
| 非 Spark 报表行 → 视频 | 报表 `main_material_id` 即 MID，素材库成对返回 VID | 是 |
| Spark 帖子 → 广告 + `ad_material_id`（当前素材） | 概览报表 `smart_plus_ad_id × main_material_id` | 是 |
| Spark 旧帖子 → 广告 | 按 `smart_plus_ad_ids` 筛选拆分报表，看帖子是否出现 | 否，靠筛选推断 |
| 创意报表行 → 创意 | 基础报表 `ad_id` = `smart_plus_creative_id`（官方文档原文） | 是 |

## 5. 实测结论（2026-11-10，新骏伯 BC，`tiktok-ads-new-junbo`，只读）

账户 `7696728186688061460`，2026-11-02 至 11-08，全部分页取回。原始回执：`.runtime/report-dimension-audit-20261110/evidence.json`。

| 查询 | 行数 | 消耗 | Native Growth 总收入 |
| --- | ---: | ---: | ---: |
| 基础报表 `ad_id_v2` × 天（整条广告） | 66 | 253.18 | 167.90 |
| 基础报表 `ad_id_v2` + `video_material_id` × 天 | 117 | 253.18 | 平台拒绝（40002，维度与指标无关联） |
| 基础报表 `ad_id` × 天（创意） | 117 | 253.18 | 167.91 |
| Smart+ 素材拆分报表 | — | 0.35 | — |

- `video_material_id` 能拿消耗，但拿不到 `native_growth_*`，`onsite_*` 收入也被拒绝。商务中心能按视频看消耗，不代表 API 能按视频拿收入。
- `video_material_id` 是素材库 MID，不是上传返回的 VID，仍要经素材库映射。
- 创意级 `ad_id` 同时有消耗和收入，61 个创意全部能经目录 `creative_ids` 接到视频。
- 本地 `material_post_link` 允许一帖对应多条广告，此前“主键导致丢关联”的判断是错的，已撤回。
- “每帖 14 次请求”是历史任务累计请求数 ÷ 已检查帖子数，不是每帖固定开销。

结论：素材经营数据的主链路应改为“基础报表创意日报（`ad_id`） → 目录创意 → 视频/轮播”。素材拆分报表和帖子补链只保留给创意级报表里已经消失的重建旧创意（xuyin 样本约 3.8% 消耗）。改造前仍需在更多账户上核验历史覆盖、轮播和删除创意。

## 6. 扩大样本核验（2026-11-10，新骏伯 BC，Codex 只读）

范围：2026-10-26 至 11-08，Smart+ 消耗前 10 账户，392 个广告组日。证据：`.runtime/creative-coverage-audit-20261110/summary.md`。

- 创意级（`ad_id`）与整条广告（`ad_id_v2`）：消耗 6,130.14 = 6,130.14，每个广告组日差额都是 0；总收入差 $0.01，单个广告组日最大差 $0.04。较早日期和近期没有区别。
- 创意 → 目录：VIDEO 占 96.46%（407 个创意）；MCP 目录查不到的 3.54%（25 个创意，$216.79）在 4 个 daihua 账户的 4 个广告组。
- 这 25 个创意已核实（2026-11-11，生产 Pod 只读，`probe_notfound.txt`）：都是已删除的创意（`/ad/get/` 返回 `secondary_status=AD_STATUS_DELETE`，仍带 `video_id`），当前 `smart_plus_ad_get` 的 creative_list 里已没有。本地 `ad_material_reference` 25 个全部有记录并带 VID。所以按本地目录映射，覆盖率 100%。
- 轮播（2026-11-11，xuyin BC 7673415075352576016 两个账户，11-03 至 11-09，`probe_carousel.txt`）：223 个有消耗的轮播创意全部是 `ad_format=CAROUSEL_ADS`、**每个创意只有 1 张图**，本地目录 1 行、1 个 `ad_material_id`；平台没有“轮播组”ID，曲目都是同一首。同一张图被 2 个创意（分属不同广告）复用的有 28 张。结论：轮播按 `web_uri`（图片）汇总即可，与视频按 VID 汇总同一个做法。样本当周消耗只有 $33.81，结构结论可信，金额占比不代表常态。

## 7. 最新批准方案（2026-11-11）

依据 [改造计划](../superpowers/plans/2026-11-11-creative-report-material-rebuild.md) §3–§13。以上 §1–§4 保留历史结构；以下是批准目标，不表示生产已经切换。

- 广告总账保留 `ad_id_v2`；素材来自 `ad_id` 创意日报，通过 `ad_creative` 做 LEFT JOIN。连接始终限定租户、账户，日期按账户当地日。
- `ad_material_reference` 保留当前位置及文件关联，`creative_ids` 由独立后续迁移删除。目录部分缺失不能判删除，历史身份和冲突证据保留。
- 新增独立 `creative_material_entity_day`，发布1影子运行，发布2切换。多图保存完整有序资产，整项金额只记一次。
- 未关联创意和总账差额分开：广告100、全部创意100、映射90时，素材90 + 未关联10 + 差额0。逐指标保留正负值、NULL和来源未就绪状态。
- 回补覆盖广告日报现存日期，近35天优先；本期仅展示差额，不自动补数。发布2稳定至少7天后，独立迁移B删除旧三表及 `creative_ids`。

### 目标表关系

```mermaid
erDiagram
  ad_object ||..o{ ad_creative : "同租户账户内 ad_remote_id"
  ad_object ||..o{ ad_material_reference : "广告当前使用位置"
  ad_object ||..o{ ad_report_day : "ad_id 存 ad_id_v2"
  creative_report_day }o..o| ad_creative : "LEFT JOIN creative_id; 未匹配保留"
  creative_report_day ||..o| creative_material_entity_day : "每个创意事实只记一次"
  ad_creative o|..o{ creative_material_entity_day : "身份及完整有序 assets"
  ad_material_reference }o..o| material_file : "local_material_id"

  ad_object["广告目录 ad_object"] {
    uuid tenant_id PK "租户ID"
    string advertiser_id PK "账户ID"
    string kind PK "campaign-adgroup-ad"
    string remote_id PK "平台对象ID"
    string parent_remote_id "上级ID"
  }
  ad_creative["持久创意身份 ad_creative"] {
    uuid tenant_id PK "租户ID"
    string advertiser_id PK "账户ID"
    string creative_id PK "报表ad_id"
    string ad_remote_id "所属广告ID"
    string ad_material_id "广告内素材ID; 可空"
    string platform_material_id "素材或组合ID; 可空"
    jsonb assets "有序资产; 多图整项"
    string identity_status "解析-组合-未知-冲突"
    boolean deleted "有删除证据仍保留历史"
    bigint published_version "目录发布版本"
  }
  ad_report_day["广告总账 ad_report_day"] {
    uuid tenant_id PK "租户ID"
    string advertiser_id PK "账户ID"
    date stat_date PK "账户当地日"
    string ad_id PK "整条广告ID"
    string adgroup_id "广告组ID"
    string campaign_id "系列ID"
    decimal metrics "消耗与各收入列"
  }
  creative_report_day["创意事实 creative_report_day"] {
    uuid tenant_id PK "租户ID"
    string advertiser_id PK "账户ID"
    date stat_date PK "当地日; 月分区"
    string creative_id PK "来源基础报表ad_id"
    string adgroup_id "广告组ID"
    string campaign_id "系列ID"
    decimal metrics "同口径; 保留NULL"
  }
  creative_material_entity_day["独立素材投影 creative_material_entity_day"] {
    uuid tenant_id PK "租户ID"
    string advertiser_id PK "账户ID"
    date stat_date PK "账户当地日"
    string fact_key PK "创意事实或差额唯一键"
    string row_key "素材或未知或差额键"
    string creative_id "创意ID; 差额为空"
    string ad_remote_id "所属广告ID; 可空"
    string adgroup_id "广告组ID"
    string attribution_status "已关联或未知或差额"
    string source_status "来源就绪状态"
    jsonb assets "多图整项金额只记一次"
  }
  ad_material_reference["当前素材使用 ad_material_reference"] {
    uuid id PK "本地主键"
    string ad_remote_id "所属广告ID"
    string ad_material_id "当前操作位置"
    string platform_material_id "素材身份"
    uuid local_material_id "本地文件关联"
  }
  material_file["本地文件 material_file"] {
    uuid id PK "本地素材文件ID"
  }
```

### 目标同步与查询

```mermaid
flowchart TB
  DIR["周期目录发布<br/>完整性及来源版本保护"] --> ID[("ad_creative<br/>持久创意身份与完整资产")]
  DIR --> REF[("ad_material_reference<br/>当前使用位置与本地文件")]
  AD["基础报表 ad_id_v2 × 当地日<br/>ad_day"] --> LEDGER[("ad_report_day<br/>广告总账")]
  CR["基础报表 ad_id × 当地日<br/>creative_day"] --> FACT[("creative_report_day<br/>全部创意事实")]
  FACT --> JOIN["LEFT JOIN 创意身份<br/>未匹配创意仍保留全部金额"]
  ID --> JOIN
  JOIN --> PROJ[("creative_material_entity_day<br/>先按作用域筛选再按素材汇总")]
  LEDGER --> REC["同租户 × 账户 × 广告组 × 当地日<br/>逐指标：广告总账 − 全部创意事实"]
  FACT --> REC
  REC -->|"正负差额单列; NULL及来源未就绪保持未知"| PROJ
  PROJ --> UI["素材页 · 广告详情 · 趋势 · 导出<br/>素材金额 / 未关联创意 / 总账差额"]
  FACT --> RULE["素材规则读取创意事实及身份<br/>缺口-冲突-缺值-不同步时跳过执行"]
  ID --> RULE
  REF --> RULE
  OFF["退役 material_day / material_links<br/>不自动拉拆分或概览补数<br/>保留非补链业务的目录读取"]
  classDef added fill:#e8f5e9,stroke:#35864a,color:#111
  classDef kept fill:#edf2f7,stroke:#73849b,color:#111
  classDef retired fill:#f4f4f4,stroke:#999,stroke-dasharray:4 3,color:#555
  class ID,FACT,JOIN,PROJ,REC,CR added
  class DIR,REF,AD,LEDGER,UI,RULE kept
  class OFF retired
```

### 金额对账

```mermaid
flowchart TB
  A["广告总账100"] --> D["总账差额 = 100 − 100 = 0"]
  B["全部创意事实100"] --> D
  B --> M["已归到素材90"]
  B --> U["未关联创意10"]
  M --> TOTAL["页面总额90 + 10 + 0 = 100"]
  U --> TOTAL
  D --> TOTAL
  NOTE["差额独立于身份是否命中<br/>逐指标保留正负值; NULL不当0<br/>多图创意只记整项金额，不分摊到图片"]
```

### 分阶段迁移

```mermaid
flowchart TB
  A["迁移A + 发布1<br/>新增身份-创意事实-影子投影<br/>旧素材页面与旧链路继续运行"]
  A --> B["本地身份回填<br/>历史创意日报回补：近35天优先<br/>目标覆盖广告日报现存日期"]
  B --> C["按租户账户及广告组日验收<br/>覆盖-身份-消耗-各窗口收入-未知状态"]
  C --> D["发布2<br/>切素材查询与规则<br/>停止规划旧任务并排空；退休回调不重启补链"]
  D --> E["稳定运行至少7天<br/>旧表仅保留；不作为业务回退源"]
  E --> F["独立迁移B<br/>按选定迁移流程备份后删除旧三表<br/>及 ad_material_reference.creative_ids"]
```

图源及渲染配置保存在 [diagrams/2026-11-11-creative-material](diagrams/2026-11-11-creative-material/)。本地HTML沿用 `.runtime/backlog-rootcause-20261110/material-id-er.html`，已移除旧的按金额门槛自动补数说明。


## 8. 官方完整文档口径修正（2026-11-11）

- Smart+ 素材概览和细分都引用[同一广告层级指标总表](https://business-api.tiktok.com/portal/docs/supported-ad-level-metrics-for-upgraded-smart-creative-reports/v1.3)，包括收入和ROAS。此前将概览概括成“无收入”不准确；应区分具体指标族及合法维度。
- [概览](https://business-api.tiktok.com/portal/docs/run-an-upgraded-smart-creative-overview-report/v1.3)在 `smart_plus_ad_id × main_material_id` 分组时可取 `smart_plus_creative_id`/`smart_plus_creative_name` 列表；这也是官方提供的关系，不能仅记录 `ad_material_id`。
- [VIDEO_INSIGHT指标表](https://business-api.tiktok.com/portal/docs/supported-metrics-in-video-insights-reports/v1.3)列出45个字段，除了消耗、展示、点击，还有转化/结果、播放、社交及自然/获得的页面互动；未列出 Native Growth 或 onsite 收入字段。不能再称其“只返回消耗、曝光、点击”。
- 当前[VIDEO_INSIGHT响应正文](https://business-api.tiktok.com/portal/docs/video-insights-reports-reference/v1.3)列出素材MID与 `tiktok_item_ids`，未列 `related_ad_ids` 或 `video_id`；平台图按当前已确认字段绘制，不再将旧说法画成已证实的直接广告关系。
- [基础报表视频维度](https://business-api.tiktok.com/portal/docs/basic-reports-supported-dimensions/v1.3)有独立指标限制，包括 `video_views_p100`。历史实测 Native Growth收入返回40002，只证明该请求组合，不能推广为所有收入类字段永远不可用。

完整独立调查及可搜索字段全集位于工作区 `docs/research/TikTok官方报表用途与维度指标对照-20261111.html`，不把本地工具的表结构当成官方接口设计原因。
