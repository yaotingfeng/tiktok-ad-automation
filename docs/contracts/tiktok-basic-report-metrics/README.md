# TikTok 基础报表支持指标（v1.3）

报表页、导出、详情等所有面向用户的指标名称，一律以本目录收录的官方文档为准，不自创名称。

## 来源

- 官方页面：[Basic report supported metrics v1.3](https://business-api.tiktok.com/portal/docs/basic-reports-supported-metrics/v1.3)，`doc_id=1751443967255553`。
- 抓取时间：2026-10-12。抓取方式：门户文档前端使用的公开接口
  `GET https://business-api.tiktok.com/gateway/api/doc/client/node/get/v2/?doc_id=1751443967255553&identify_key=<v1.3 平台 key>&language=CHINESE|ENGLISH`，
  取 `data.content_data` 原文（Markdown，含 `xtable` 表格），未做任何改写。
- 以 `#|` 开头的表格行是官方原文格式，保留不动。

| 文件 | 语言 | SHA-256 |
| --- | --- | --- |
| `v1.3.zh.md` | 中文 | `9231646d197c7ef98952c4abaaa4c89e41ea8ef5bc266d244283513e41c67799` |
| `v1.3.en.md` | 英文 | `0908f180e9046f90522140abc7600cff12b41e7d7b77b9f8f4068f0e222fd4a9` |

刷新时用同一接口重新抓取、更新哈希，并在下表核对字段名称是否变化。

## 本项目已使用字段对照

天数后缀按官方定义：`dN` 是点击后 `(N+1)×24` 小时内累计，例如 `d0`＝24 小时、`d6`＝168 小时。

### 基础报表（账户 / 系列 / 广告组 / 广告）

| 存储列 | 平台字段 | 官方中文名 | 官方英文名 |
| --- | --- | --- | --- |
| `spend` | `spend` | 消耗 | Cost |
| `impressions` | `impressions` | 展示量 | Impressions |
| `clicks` | `clicks` | 点击量（目标页面） | Clicks (destination) |
| — | `ctr` | 点击率（目标页面） | CTR (destination) |
| — | `cpc` | 平均点击成本（目标页面） | CPC (destination) |
| — | `cpm` | 千次展示成本 (CPM) | CPM |
| `ad_impression_events` | `native_growth_total_ad_impression_event_count` | Native Growth 广告展示事件总数 | Native Growth total ad impression events |
| — | `native_growth_cost_per_ad_impression_event` | Native Growth 单次广告展示事件成本 | Native Growth cost per ad impression event |
| `revenue_total` | `native_growth_total_ad_impression_value` | Native Growth 广告展示总价值 | Native Growth total ad impression value |
| — | `native_growth_ad_revenue_roas` | Native Growth 广告收益 ROAS | Native Growth ad revenue ROAS |
| `revenue_d0` | `native_growth_ad_revenue_value_d0` | Native Growth 第 0 天 广告收益金额 | Native Growth day 0 ad revenue value |
| `revenue_d3` | `native_growth_ad_revenue_value_d2` | Native Growth 第 2 天广告收益金额 | Native Growth day 2 ad revenue value |
| `revenue_d7` | `native_growth_ad_revenue_value_d6` | Native Growth 第 6 天广告收益金额 | Native Growth day 6 ad revenue value |
| `revenue_d14` | `native_growth_ad_revenue_value_d13` | Native Growth 第 13 天广告收益金额 | Native Growth day 13 ad revenue value |
| `revenue_d30` | `native_growth_ad_revenue_value_d29` | Native Growth 第 29 天广告收益金额 | Native Growth day 29 ad revenue value |
| — | `native_growth_ad_revenue_roas_dN` | Native Growth 第 N 天广告收益 ROAS | Native Growth day N ad revenue ROAS |

### 素材报表（字段名与基础报表不同）

| 存储列 | 平台字段 | 官方中文名 |
| --- | --- | --- |
| `ad_impression_events` | `onsite_total_ad_impression` | 广告曝光事件总数 |
| `revenue_total` | `onsite_total_ad_impression_value` | 广告曝光总价值 |
| `revenue_d0` | `onsite_ad_impression_ad_revenue_day0` | 第 0 天广告收入 |
| `revenue_d7` | `onsite_ad_impression_ad_revenue_day6` | 第 6 天广告收入 |
| `revenue_d14` | `onsite_ad_impression_ad_revenue_day13` | 第 13 天广告收入 |

注意：本地存储列名 `revenue_d3/d7/d14/d30` 与官方"第 2/6/13/29 天"是同一字段的不同叫法，页面展示必须使用官方天数。
