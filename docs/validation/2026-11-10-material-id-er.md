# 素材报表 ID 关联 ER 图与同步流程

依据：官方文档（Get Upgraded Smart+ Ads、Creative Overview/Breakdown、Basic Reports 维度）与 2026-11-10 生产只读核验。实线 = 官方接口直接返回的字段关联；标”定位”的是我们靠筛选推断出来的归属，不是平台返回的字段。

## 报表类型说明

| 缩写 | 全称 | 中文说明 | 数据粒度 |
| --- | --- | --- | --- |
| RPT_AD_V2 | Basic Report (ad_id_v2) | 基础报表广告维度 | 整条广告汇总，按天 |
| RPT_CREATIVE | Basic Report (ad_id) | 基础报表创意维度 | 创意级明细，按天 |
| RPT_OVERVIEW | Material Overview | 素材概览报表 | 广告×素材分组，不按天，无收入 |
| RPT_BREAKDOWN | Material Breakdown | 素材拆分报表 | 素材级明细，按天，带收入，只有帖子ID |

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
        decimal spend "不按天、无收入"
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
    material_report_day }o--|| material_post_link : "main_material_id + type"
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
    OV -->|"查不到（旧帖子）"| LOC["按广告筛选拆分报表定位<br/>每批 ≤200 广告、二分、翻页<br/>每帖约 14 次请求"]
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
