import type { AdMaterialsPublic, CarouselCreativeIdentity } from "@/client"
// 体验版假后端：广告报表与广告管理。

import METRIC_CATALOG_JSON from "../fixtures/ads-metric-catalog.json"
import {
  DEMO_REPORT_EXPORTS,
  DEMO_REPORT_VIEWS,
  generateMetrics,
  parseDramaFromName,
} from "../fixtures/adsData"
import {
  DEMO_ADVERTISERS,
  DEMO_DRAMAS,
  demoWorld,
  materialsOfDrama,
  seeded,
} from "../fixtures/world"
import { dateDaysAgo, demoUuid, isoDaysAgo, nextId } from "../runtime/clock"
import { matchesSearch, paginate } from "../runtime/paging"
import { handle } from "../runtime/registry"

// ========== 广告报表 (ads_reporting) ==========

// 指标目录
handle("ads_reporting-metric_catalog", (_req) => {
  // JSON导入的类型与生成类型略有差异，使用any绕过类型检查
  return METRIC_CATALOG_JSON as any
})

// 同步状态
handle("ads_reporting-get_ad_sync_status", (req) => {
  const bcId = req.query.bc_id
  const advertisers = DEMO_ADVERTISERS.filter(
    (a) => a.bc_id === bcId && a.purpose === "delivery",
  )

  return {
    accounts: advertisers.map((a) => ({
      advertiser_id: a.advertiser_id,
      timezone: a.timezone,
      local_today: dateDaysAgo(0),
      last_success_at: isoDaysAgo(0, 0, 15),
      incomplete_dates: [],
      failed_dates: [],
    })),
    complete_from: dateDaysAgo(30),
    publication_version: `v-${Date.now()}`,
    plan: {
      enabled: true,
      disabled_reason: null,
      next_hot_at: null,
      slowed: false,
    },
  } as any
})

// 刷新同步
handle("ads_reporting-request_ad_sync_refresh", (req) => {
  return {
    queued: 5,
    accounts: DEMO_ADVERTISERS.filter(
      (a) => a.bc_id === req.query.bc_id && a.purpose === "delivery",
    ).length,
    status: "QUEUED" as const,
  } as any
})

// 主查询接口：六维报表
handle("ads_reporting-query_ads", (req) => {
  const bcId = req.query.bc_id
  const dimension = req.query.dimension ?? "campaign"
  const startDate = req.query.start_date ?? dateDaysAgo(0)
  const endDate = req.query.end_date ?? dateDaysAgo(0)
  const query = req.query.query ?? ""
  const limit = Number(req.query.limit ?? 50)
  const cursor = req.query.cursor

  // 筛选该 BC 下的 Campaign
  const campaigns = demoWorld.launched.filter((c) => c.bc_id === bcId)

  // 按维度构建行
  const allRows: Array<{
    rowKey: string
    display: Record<string, unknown>
    refs: unknown[]
    metrics: Record<string, string>
    carousel_creatives?: CarouselCreativeIdentity[]
    campaign: (typeof campaigns)[0]
  }> = []

  for (const campaign of campaigns) {
    if (!campaign) continue
    const parsed = parseDramaFromName(campaign.name)
    const drama = DEMO_DRAMAS.find((d) => d.id === campaign.drama_id)

    if (dimension === "campaign") {
      allRows.push({
        rowKey: `${bcId}-${campaign.campaign_id}`,
        display: {
          name: campaign.name,
          campaign_name: campaign.name,
          campaign_id: campaign.campaign_id,
          account_name:
            DEMO_ADVERTISERS.find(
              (a) => a.advertiser_id === campaign.advertiser_id,
            )?.name ?? "账户",
          account_id: campaign.advertiser_id,
          advertiser_id: campaign.advertiser_id,
          remote_id: campaign.campaign_id,
          operation_status: campaign.status,
          secondary_status:
            campaign.status === "ENABLE"
              ? "CAMPAIGN_STATUS_ENABLE"
              : "CAMPAIGN_STATUS_DISABLE",
          campaign_budget: campaign.budget.toString(),
          drama_name: parsed.dramaName || drama?.title || "未知剧",
        },
        refs: [
          {
            tenant_id: req.path.tenant_id,
            advertiser_id: campaign.advertiser_id,
            kind: "campaign",
            remote_id: campaign.campaign_id,
          },
        ],
        metrics: generateMetrics(
          campaign,
          startDate,
          "campaign",
          campaign.campaign_id,
        ),
        campaign,
      })
    } else if (dimension === "adgroup") {
      for (const adgroup of campaign.adgroups) {
        allRows.push({
          rowKey: `${bcId}-${adgroup.adgroup_id}`,
          display: {
            name: adgroup.name,
            account_name:
              DEMO_ADVERTISERS.find(
                (a) => a.advertiser_id === campaign.advertiser_id,
              )?.name ?? "账户",
            account_id: campaign.advertiser_id,
            campaign_name: campaign.name,
            campaign_id: campaign.campaign_id,
            adgroup_name: adgroup.name,
            adgroup_id: adgroup.adgroup_id,
            advertiser_id: campaign.advertiser_id,
            remote_id: adgroup.adgroup_id,
            operation_status: adgroup.status,
            secondary_status:
              adgroup.status === "ENABLE"
                ? "ADGROUP_STATUS_DELIVERY_OK"
                : "ADGROUP_STATUS_PAUSED",
          },
          refs: [
            {
              tenant_id: req.path.tenant_id,
              advertiser_id: campaign.advertiser_id,
              kind: "adgroup",
              remote_id: adgroup.adgroup_id,
            },
          ],
          metrics: generateMetrics(
            campaign,
            startDate,
            "adgroup",
            adgroup.adgroup_id,
          ),
          campaign,
        })
      }
    } else if (dimension === "ad") {
      for (const adgroup of campaign.adgroups) {
        for (const ad of adgroup.ads) {
          allRows.push({
            rowKey: `${bcId}-${ad.ad_id}`,
            display: {
              name: ad.name,
              account_name:
                DEMO_ADVERTISERS.find(
                  (a) => a.advertiser_id === campaign.advertiser_id,
                )?.name ?? "账户",
              account_id: campaign.advertiser_id,
              campaign_name: campaign.name,
              campaign_id: campaign.campaign_id,
              adgroup_name: adgroup.name,
              adgroup_id: adgroup.adgroup_id,
              creative_format: ad.carousel_creatives ? "CAROUSEL_ADS" : "VIDEO",
              metric_scope: ad.carousel_creatives ? "AD" : "MATERIAL",
              creative_linkage: ad.carousel_creatives ? "COMPLETE" : null,
              ad_name: ad.name,
              ad_id: ad.ad_id,
              advertiser_id: campaign.advertiser_id,
              remote_id: ad.ad_id,
              operation_status: ad.status,
              secondary_status:
                ad.status === "ENABLE"
                  ? "AD_STATUS_DELIVERY_OK"
                  : "AD_STATUS_PAUSED",
            },
            refs: [
              {
                tenant_id: req.path.tenant_id,
                advertiser_id: campaign.advertiser_id,
                kind: "ad",
                remote_id: ad.ad_id,
              },
            ],
            carousel_creatives: ad.carousel_creatives,
            metrics: generateMetrics(campaign, startDate, "ad", ad.ad_id),
            campaign,
          })
        }
      }
    } else if (dimension === "account") {
      // 账户维度：按账户汇总
      const accountMap = new Map<string, (typeof allRows)[0][]>()
      for (const campaign of campaigns) {
        if (!campaign) continue
        if (!accountMap.has(campaign.advertiser_id)) {
          accountMap.set(campaign.advertiser_id, [])
        }
        accountMap.get(campaign.advertiser_id)!.push({
          rowKey: `${bcId}-${campaign.advertiser_id}`,
          display: {},
          refs: [],
          metrics: generateMetrics(
            campaign,
            startDate,
            "campaign",
            campaign.campaign_id,
          ),
          campaign,
        })
      }

      for (const [advertiserId, campaignsData] of accountMap) {
        const advertiser = DEMO_ADVERTISERS.find(
          (a) => a.advertiser_id === advertiserId,
        )
        if (!advertiser) continue

        // 汇总指标
        const sumMetrics = {
          spend: 0,
          revenue_d0: 0,
          revenue_total: 0,
          impressions: 0,
          clicks: 0,
          ad_impression_events: 0,
        }

        for (const item of campaignsData) {
          sumMetrics.spend += Number.parseFloat(item.metrics.spend)
          sumMetrics.revenue_d0 += Number.parseFloat(item.metrics.revenue_d0)
          sumMetrics.revenue_total += Number.parseFloat(
            item.metrics.revenue_total,
          )
          sumMetrics.impressions += Number.parseInt(
            item.metrics.impressions,
            10,
          )
          sumMetrics.clicks += Number.parseInt(item.metrics.clicks, 10)
          sumMetrics.ad_impression_events += Number.parseInt(
            item.metrics.ad_impression_events,
            10,
          )
        }

        allRows.push({
          rowKey: `${bcId}-account-${advertiserId}`,
          display: {
            name: advertiser.name,
            account_name: advertiser.name,
            account_id: advertiserId,
            advertiser_id: advertiserId,
            remote_id: advertiserId,
            account_status: "STATUS_ENABLE",
            balance: advertiser.balance.toFixed(2),
            balance_currency: advertiser.currency,
            balance_scope: "ADVERTISER",
            balance_updated_at: isoDaysAgo(0, 2, 0),
          },
          refs: [],
          metrics: {
            spend: sumMetrics.spend.toFixed(2),
            revenue_d0: sumMetrics.revenue_d0.toFixed(2),
            revenue_total: sumMetrics.revenue_total.toFixed(2),
            roas_d0: (sumMetrics.revenue_d0 / sumMetrics.spend).toFixed(3),
            roas_d3: (
              (sumMetrics.revenue_d0 * 1.05) /
              sumMetrics.spend
            ).toFixed(3),
            roas_d7: (
              (sumMetrics.revenue_d0 * 1.12) /
              sumMetrics.spend
            ).toFixed(3),
            roas_d14: (
              (sumMetrics.revenue_d0 * 1.18) /
              sumMetrics.spend
            ).toFixed(3),
            roas_d30: (
              (sumMetrics.revenue_d0 * 1.25) /
              sumMetrics.spend
            ).toFixed(3),
            roas_total: (sumMetrics.revenue_total / sumMetrics.spend).toFixed(
              3,
            ),
            impressions: sumMetrics.impressions.toString(),
            clicks: sumMetrics.clicks.toString(),
            ctr: (sumMetrics.clicks / sumMetrics.impressions).toFixed(4),
            cpm: ((sumMetrics.spend / sumMetrics.impressions) * 1000).toFixed(
              2,
            ),
            cpc: (sumMetrics.spend / sumMetrics.clicks).toFixed(2),
            ad_impression_events: sumMetrics.ad_impression_events.toString(),
            cost_per_ad_impression_event: (
              sumMetrics.spend / sumMetrics.ad_impression_events
            ).toFixed(2),
          },
          campaign: campaignsData[0].campaign,
        })
      }
    } else if (dimension === "drama") {
      // 剧维度：按版权方+剧名归组
      const dramaMap = new Map<string, (typeof allRows)[0][]>()
      for (const campaign of campaigns) {
        if (!campaign) continue
        const parsed = parseDramaFromName(campaign.name)
        if (!parsed.isValid) continue
        const dramaKey = `${parsed.provider}-${parsed.dramaName}`
        if (!dramaMap.has(dramaKey)) {
          dramaMap.set(dramaKey, [])
        }
        dramaMap.get(dramaKey)!.push({
          rowKey: `${bcId}-drama-${dramaKey}`,
          display: { provider: parsed.provider, drama_name: parsed.dramaName },
          refs: [],
          metrics: generateMetrics(
            campaign,
            startDate,
            "campaign",
            campaign.campaign_id,
          ),
          campaign,
        })
      }

      for (const [dramaKey, campaignsData] of dramaMap) {
        const [provider, dramaName] = dramaKey.split("-")
        const sumMetrics = {
          spend: 0,
          revenue_d0: 0,
          revenue_total: 0,
          impressions: 0,
          clicks: 0,
          ad_impression_events: 0,
        }

        for (const item of campaignsData) {
          sumMetrics.spend += Number.parseFloat(item.metrics.spend)
          sumMetrics.revenue_d0 += Number.parseFloat(item.metrics.revenue_d0)
          sumMetrics.revenue_total += Number.parseFloat(
            item.metrics.revenue_total,
          )
          sumMetrics.impressions += Number.parseInt(
            item.metrics.impressions,
            10,
          )
          sumMetrics.clicks += Number.parseInt(item.metrics.clicks, 10)
          sumMetrics.ad_impression_events += Number.parseInt(
            item.metrics.ad_impression_events,
            10,
          )
        }

        allRows.push({
          rowKey: `${bcId}-drama-${dramaKey}`,
          display: {
            name: dramaName,
            drama_name: dramaName,
            provider,
          },
          refs: [],
          metrics: {
            spend: sumMetrics.spend.toFixed(2),
            revenue_d0: sumMetrics.revenue_d0.toFixed(2),
            revenue_total: sumMetrics.revenue_total.toFixed(2),
            roas_d0: (sumMetrics.revenue_d0 / sumMetrics.spend).toFixed(3),
            roas_d3: (
              (sumMetrics.revenue_d0 * 1.05) /
              sumMetrics.spend
            ).toFixed(3),
            roas_d7: (
              (sumMetrics.revenue_d0 * 1.12) /
              sumMetrics.spend
            ).toFixed(3),
            roas_d14: (
              (sumMetrics.revenue_d0 * 1.18) /
              sumMetrics.spend
            ).toFixed(3),
            roas_d30: (
              (sumMetrics.revenue_d0 * 1.25) /
              sumMetrics.spend
            ).toFixed(3),
            roas_total: (sumMetrics.revenue_total / sumMetrics.spend).toFixed(
              3,
            ),
            impressions: sumMetrics.impressions.toString(),
            clicks: sumMetrics.clicks.toString(),
            ctr: (sumMetrics.clicks / sumMetrics.impressions).toFixed(4),
            cpm: ((sumMetrics.spend / sumMetrics.impressions) * 1000).toFixed(
              2,
            ),
            cpc: (sumMetrics.spend / sumMetrics.clicks).toFixed(2),
            ad_impression_events: sumMetrics.ad_impression_events.toString(),
            cost_per_ad_impression_event: (
              sumMetrics.spend / sumMetrics.ad_impression_events
            ).toFixed(2),
          },
          campaign: campaignsData[0].campaign,
        })
      }
    } else if (dimension === "material") {
      // 素材维度：每个广告的素材
      for (const campaign of campaigns) {
        if (!campaign) continue
        for (const adgroup of campaign.adgroups) {
          for (const ad of adgroup.ads) {
            const material = materialsOfDrama(campaign.drama_id).find(
              (m) => m.id === ad.material_id,
            )
            if (!material) continue

            const adMetrics = generateMetrics(
              campaign,
              startDate,
              "ad",
              ad.ad_id,
            )
            // 素材指标是广告的一部分
            const materialSpend =
              Number.parseFloat(adMetrics.spend) *
              (0.7 + seeded(material.id) * 0.3)

            allRows.push({
              rowKey: `${bcId}-material-${material.id}-${ad.ad_id}`,
              display: {
                name: material.file_name,
                material_name: material.file_name,
                main_material_id: material.video_id,
                account_id: campaign.advertiser_id,
                account_name:
                  DEMO_ADVERTISERS.find(
                    (a) => a.advertiser_id === campaign.advertiser_id,
                  )?.name ?? "账户",
                advertiser_id: campaign.advertiser_id,
              },
              refs: [],
              metrics: {
                spend: materialSpend.toFixed(2),
                revenue_d0: (
                  materialSpend * Number.parseFloat(adMetrics.roas_d0)
                ).toFixed(2),
                revenue_total: (
                  materialSpend * Number.parseFloat(adMetrics.roas_total)
                ).toFixed(2),
                roas_d0: adMetrics.roas_d0,
                roas_d7: adMetrics.roas_d7,
                roas_d14: adMetrics.roas_d14,
                roas_total: adMetrics.roas_total,
                impressions: Math.round(
                  Number.parseInt(adMetrics.impressions, 10) * 0.8,
                ).toString(),
                clicks: Math.round(
                  Number.parseInt(adMetrics.clicks, 10) * 0.8,
                ).toString(),
                ctr: adMetrics.ctr,
                cpm: adMetrics.cpm,
                cpc: adMetrics.cpc,
                ad_impression_events: Math.round(
                  Number.parseInt(adMetrics.ad_impression_events, 10) * 0.8,
                ).toString(),
                cost_per_ad_impression_event:
                  adMetrics.cost_per_ad_impression_event,
              },
              campaign,
            })
          }
        }
      }
    }
  }

  // 搜索过滤
  const filtered = allRows.filter((row) => {
    if (!query) return true
    const searchText = JSON.stringify(row.display).toLowerCase()
    return matchesSearch(searchText, query)
  })

  // 去重（账户、剧维度可能有重复键）
  const uniqueRows = Array.from(
    new Map(filtered.map((row) => [row.rowKey, row])).values(),
  )

  // 分页
  const paged = paginate(
    uniqueRows,
    { limit, cursor: cursor ?? undefined },
    (row) => row.rowKey,
  )

  // 汇总
  const summary = {
    row_count: uniqueRows.length,
    incomplete_advertiser_count: 0,
    incomplete_dates: [],
    buckets: [
      {
        currency: "USD",
        timezone: "UTC",
        attribution: "IAA",
        values: paged.items.reduce(
          (acc, item) => {
            acc.spend = (
              Number.parseFloat(acc.spend) +
              Number.parseFloat(item.metrics.spend)
            ).toFixed(2)
            acc.revenue_d0 = (
              Number.parseFloat(acc.revenue_d0) +
              Number.parseFloat(item.metrics.revenue_d0)
            ).toFixed(2)
            acc.revenue_total = (
              Number.parseFloat(acc.revenue_total) +
              Number.parseFloat(item.metrics.revenue_total)
            ).toFixed(2)
            acc.impressions = (
              Number.parseInt(acc.impressions, 10) +
              Number.parseInt(item.metrics.impressions, 10)
            ).toString()
            acc.clicks = (
              Number.parseInt(acc.clicks, 10) +
              Number.parseInt(item.metrics.clicks, 10)
            ).toString()
            acc.ad_impression_events = (
              Number.parseInt(acc.ad_impression_events, 10) +
              Number.parseInt(item.metrics.ad_impression_events, 10)
            ).toString()
            return acc
          },
          {
            spend: "0",
            revenue_d0: "0",
            revenue_total: "0",
            impressions: "0",
            clicks: "0",
            ad_impression_events: "0",
          } as Record<string, string>,
        ),
        availability: {
          spend: "AVAILABLE",
          revenue_d0: "AVAILABLE",
          revenue_total: "AVAILABLE",
          impressions: "AVAILABLE",
          clicks: "AVAILABLE",
          ad_impression_events: "AVAILABLE",
        },
      },
    ],
  }

  // 计算汇总的派生指标 - 需要类型断言因为 values 初始化时只有部分字段
  const summaryValues = summary.buckets[0].values as Record<string, string>
  const totalSpend = Number.parseFloat(summaryValues.spend)
  const totalRevenueD0 = Number.parseFloat(summaryValues.revenue_d0)
  const totalRevenueTotal = Number.parseFloat(summaryValues.revenue_total)
  const totalImpressions = Number.parseInt(summaryValues.impressions, 10)
  const totalClicks = Number.parseInt(summaryValues.clicks, 10)
  const totalAdImpressions = Number.parseInt(
    summaryValues.ad_impression_events,
    10,
  )

  summaryValues.roas_d0 =
    totalSpend > 0 ? (totalRevenueD0 / totalSpend).toFixed(3) : "0"
  summaryValues.roas_d3 =
    totalSpend > 0 ? ((totalRevenueD0 * 1.05) / totalSpend).toFixed(3) : "0"
  summaryValues.roas_d7 =
    totalSpend > 0 ? ((totalRevenueD0 * 1.12) / totalSpend).toFixed(3) : "0"
  summaryValues.roas_d14 =
    totalSpend > 0 ? ((totalRevenueD0 * 1.18) / totalSpend).toFixed(3) : "0"
  summaryValues.roas_d30 =
    totalSpend > 0 ? ((totalRevenueD0 * 1.25) / totalSpend).toFixed(3) : "0"
  summaryValues.roas_total =
    totalSpend > 0 ? (totalRevenueTotal / totalSpend).toFixed(3) : "0"
  summaryValues.ctr =
    totalImpressions > 0 ? (totalClicks / totalImpressions).toFixed(4) : "0"
  summaryValues.cpm =
    totalImpressions > 0
      ? ((totalSpend / totalImpressions) * 1000).toFixed(2)
      : "0"
  summaryValues.cpc =
    totalClicks > 0 ? (totalSpend / totalClicks).toFixed(2) : "0"
  summaryValues.cost_per_ad_impression_event =
    totalAdImpressions > 0 ? (totalSpend / totalAdImpressions).toFixed(2) : "0"

  // 扩展 availability 对象以包含所有派生指标，先赋值给变量避免类型推断问题
  summary.buckets[0].availability = Object.assign(
    summary.buckets[0].availability || {},
    {
      roas_d0: "AVAILABLE",
      roas_d3: dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
      roas_d7: "AVAILABLE",
      roas_d14: "AVAILABLE",
      roas_d30: dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
      roas_total: "AVAILABLE",
      ctr: "AVAILABLE",
      cpm: "AVAILABLE",
      cpc: "AVAILABLE",
      cost_per_ad_impression_event: "AVAILABLE",
    },
  )

  // Demo返回类型与生成类型可能有细微差异，但运行时行为正确
  return {
    snapshot: {
      snapshot_id: demoUuid(9001),
      expires_at: "2099-01-01T00:00:00Z",
      filters: {
        dimension,
        start_date: startDate,
        end_date: endDate,
      },
      publication_versions: {},
    },
    items: paged.items.map((row) => ({
      carousel_creatives: row.carousel_creatives ?? [],
      row_key: row.rowKey,
      display: Object.fromEntries(
        Object.entries(row.display as Record<string, unknown>).map(([k, v]) => [
          k,
          v === null || v === undefined ? null : String(v),
        ]),
      ) as { [key: string]: string | null },
      refs: row.refs as any,
      material_uses: [],
      metric_buckets: [
        {
          currency: "USD",
          timezone: "UTC",
          attribution: "IAA",
          values: row.metrics,
          availability: Object.keys(row.metrics).reduce(
            (acc, key) => {
              acc[key] =
                (key === "roas_d3" || key === "roas_d30") &&
                dimension === "material"
                  ? "UNSUPPORTED"
                  : "AVAILABLE"
              return acc
            },
            {} as Record<string, string>,
          ),
        },
      ],
      capabilities: { can_manage: true },
      coverage: {
        status: "COMPLETE",
        updated_at: isoDaysAgo(0, 0, 5),
      },
    })),
    total: uniqueRows.length,
    summary,
    coverage: {
      status: "COMPLETE",
      updated_at: isoDaysAgo(0, 0, 5),
    },
    next_cursor: paged.next_cursor ?? null,
  } as any
})

// 趋势
handle("ads_reporting-ad_trend", (req) => {
  const level = req.query.level ?? "account"
  const granularity = req.query.granularity ?? "day"
  const startDate = req.query.start_date ?? dateDaysAgo(7)
  const endDate = req.query.end_date ?? dateDaysAgo(0)

  // 生成趋势点
  const start = new Date(startDate)
  const end = new Date(endDate)
  const points: Array<{ at: string; buckets: unknown[]; coverage: string }> = []

  for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const dateStr = d.toISOString().split("T")[0]
    const daySpend = 80 + seeded(`trend-${dateStr}`) * 120

    points.push({
      at:
        granularity === "hour"
          ? `${dateStr}T${String(d.getHours()).padStart(2, "0")}:00Z`
          : dateStr,
      buckets: [
        {
          currency: "USD",
          timezone: "UTC",
          attribution: "IAA",
          values: {
            spend: daySpend.toFixed(2),
            cpm: (1.5 + seeded(`cpm-${dateStr}`) * 1.0).toFixed(2),
          },
        },
      ],
      coverage: "COMPLETE",
    })
  }

  return {
    level,
    granularity,
    timezone: "UTC",
    points,
    syncing: false,
  } as any
})

// 广告详情
handle("ads_reporting-ad_detail", (req) => {
  const { kind, remote_id: id } = req.path
  const tenantId = req.path.tenant_id

  // 从 launched 里找对应的对象
  let found: {
    campaign: (typeof demoWorld.launched)[0]
    adgroup?: (typeof demoWorld.launched)[0]["adgroups"][0]
    ad?: (typeof demoWorld.launched)[0]["adgroups"][0]["ads"][0]
  } | null = null

  for (const campaign of demoWorld.launched) {
    if (kind === "campaign" && campaign.campaign_id === id) {
      found = { campaign }
      break
    }
    for (const adgroup of campaign.adgroups) {
      if (kind === "adgroup" && adgroup.adgroup_id === id) {
        found = { campaign, adgroup }
        break
      }
      for (const ad of adgroup.ads) {
        if (kind === "ad" && ad.ad_id === id) {
          found = { campaign, adgroup, ad }
          break
        }
      }
      if (found) break
    }
    if (found) break
  }

  if (!found) {
    return {
      ref: {
        tenant_id: tenantId,
        advertiser_id: "unknown",
        kind,
        remote_id: id,
      },
      name: "未找到",
      ad_type: "VIDEO",
      configuration: {},
      statuses: { operation: "UNKNOWN", review: "APPROVED" },
      parent: null,
      materials: [],
      operation_history: [],
      observed_at: isoDaysAgo(0, 1, 0),
    }
  }

  const { campaign, adgroup, ad } = found

  return {
    ref: {
      tenant_id: tenantId,
      advertiser_id: campaign.advertiser_id,
      kind,
      remote_id: id,
    },
    name:
      kind === "campaign"
        ? campaign.name
        : kind === "adgroup"
          ? adgroup!.name
          : ad!.name,
    ad_type: "VIDEO",
    configuration:
      kind === "campaign"
        ? {
            budget: campaign.budget.toString(),
            budget_mode: "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
            objective_type: "APP_PROMOTION",
            app_promotion_type: "MINIS",
            campaign_type: "REGULAR_CAMPAIGN",
            create_time: campaign.created_at,
            modify_time: campaign.created_at,
          }
        : kind === "adgroup"
          ? {
              roas_bid: campaign.target_roas?.toString() ?? null,
              bid_type: "BID_TYPE_NO_BID",
              deep_bid_type: "VO_MIN_ROAS",
              budget: adgroup!.budget.toString(),
              budget_mode: "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
              create_time: campaign.created_at,
              modify_time: campaign.created_at,
            }
          : {
              ad_text_list: [{ ad_text: ad!.name }],
              create_time: campaign.created_at,
              modify_time: campaign.created_at,
            },
    statuses: {
      operation:
        kind === "campaign"
          ? campaign.status
          : kind === "adgroup"
            ? adgroup!.status
            : ad!.status,
      review: "APPROVED",
    },
    parent:
      kind !== "campaign"
        ? {
            ref: {
              tenant_id: tenantId,
              advertiser_id: campaign.advertiser_id,
              kind: "campaign",
              remote_id: campaign.campaign_id,
            },
            operation_status: campaign.status,
            reasons:
              campaign.status === "DISABLE" ? ["operation_status:DISABLE"] : [],
          }
        : null,
    materials:
      kind === "ad"
        ? [
            {
              use_ref: {
                ad_ref: {
                  tenant_id: tenantId,
                  advertiser_id: campaign.advertiser_id,
                  kind: "ad",
                  remote_id: ad!.ad_id,
                },
                platform_material_id: `video-${ad!.material_id}`,
                ad_material_id: ad!.material_id,
                material_type: "VIDEO",
              },
              name:
                materialsOfDrama(campaign.drama_id).find(
                  (m) => m.id === ad!.material_id,
                )?.file_name ?? "video.mp4",
              operation_status: ad!.status,
              complete: true,
              external: false,
            },
          ]
        : [],
    operation_history: [
      {
        task_id: demoUuid(701),
        created_at: isoDaysAgo(2, 15, 30),
        field: "status",
        mode: "set",
        original_value: "ENABLE",
        final_value: "DISABLE",
        execution_result: "ACCEPTED",
        reason: null,
      },
    ],
    observed_at: isoDaysAgo(0, 1, 0),
  } as any
})

// 广告的素材列表
handle("ads_reporting-ad_materials", (req) => {
  const adId = req.path.remote_id

  // 找到这个广告
  let found: {
    campaign: (typeof demoWorld.launched)[0]
    ad: (typeof demoWorld.launched)[0]["adgroups"][0]["ads"][0]
  } | null = null
  for (const campaign of demoWorld.launched) {
    for (const adgroup of campaign.adgroups) {
      for (const ad of adgroup.ads) {
        if (ad.ad_id === adId) {
          found = { campaign, ad }
          break
        }
      }
      if (found) break
    }
    if (found) break
  }

  if (!found) {
    return {
      items: [],
      coverage: { status: "COMPLETE" },
    }
  }

  const { campaign, ad } = found
  if (ad.carousel_creatives)
    return {
      items: [],
      coverage: { status: "COMPLETE" },
      carousel_creatives: ad.carousel_creatives,
      carousel_linkage_complete: true,
      metric_scope: "AD",
      frozen_creative_links: ad.frozen_creative_links ?? [],
    } satisfies AdMaterialsPublic
  const materials = materialsOfDrama(campaign.drama_id).filter(
    (m) => m.id === ad.material_id,
  )

  return {
    items: materials.map((material) => ({
      use_ref: {
        ad_ref: {
          tenant_id: req.path.tenant_id,
          advertiser_id: campaign.advertiser_id,
          kind: "ad",
          remote_id: adId,
        },
        platform_material_id: material.video_id,
        ad_material_id: material.id,
        material_type: "VIDEO",
      },
      name: material.file_name,
      operation_status: ad.status,
      post_ids: [`post-${material.id}`],
      shared: false,
      metric_buckets: [
        {
          currency: "USD",
          timezone: "UTC",
          values: { spend: (5 + seeded(material.id) * 15).toFixed(2) },
          availability: { spend: "AVAILABLE" },
        },
      ],
    })),
    coverage: { status: "COMPLETE" },
  } as any
})

// 报表视图列表
handle("ads_reporting-list_report_views", (_req) => {
  return DEMO_REPORT_VIEWS as any
})

// 创建报表视图
handle("ads_reporting-create_report_view", (req) => {
  const view = {
    id: demoUuid(nextId()),
    name: req.body.name,
    filters: req.body.filters,
    columns: req.body.columns ?? [],
    created_at: new Date().toISOString(),
  }
  DEMO_REPORT_VIEWS.push(view)
  return view as any
})

// 更新报表视图
handle("ads_reporting-update_report_view", (req) => {
  const view = DEMO_REPORT_VIEWS.find((v) => v.id === req.path.view_id)
  if (!view) {
    return {
      id: req.path.view_id,
      name: "未找到",
      filters: {},
      columns: [],
      created_at: new Date().toISOString(),
    }
  }
  if (req.body.name) view.name = req.body.name
  if (req.body.filters) view.filters = req.body.filters
  if (req.body.columns) view.columns = req.body.columns
  return view as any
})

// 删除报表视图
handle("ads_reporting-delete_report_view", (req) => {
  const index = DEMO_REPORT_VIEWS.findIndex((v) => v.id === req.path.view_id)
  if (index >= 0) DEMO_REPORT_VIEWS.splice(index, 1)
  return undefined as any
})

// 报表导出列表
handle("ads_reporting-list_report_exports", (_req) => {
  return {
    items: DEMO_REPORT_EXPORTS,
    total: DEMO_REPORT_EXPORTS.length,
  } as any
})

// 创建报表导出
handle("ads_reporting-create_report_export", (req) => {
  const exportJob = {
    id: demoUuid(nextId()),
    status: "QUEUED" as "QUEUED" | "GENERATING" | "COMPLETE" | "FAILED",
    created_at: new Date().toISOString(),
    completed_at: null as string | null,
    dimension:
      (req.body as { filters?: { dimension?: string } }).filters?.dimension ??
      "campaign",
    row_count: null as number | null,
  }
  DEMO_REPORT_EXPORTS.push(exportJob)

  // 2秒后变成完成 - 修改对象属性
  setTimeout(() => {
    // 类型断言因为 setTimeout 中修改的是可变属性
    const job = exportJob as {
      status: "QUEUED" | "GENERATING" | "COMPLETE" | "FAILED"
      completed_at: string | null
      row_count: number | null
    }
    job.status = "COMPLETE"
    job.completed_at = new Date().toISOString()
    job.row_count = 10 + Math.floor(seeded(exportJob.id) * 40)
  }, 2000)

  return {
    id: exportJob.id,
    status: exportJob.status,
    coverage: {},
  } as any
})

// 查询报表导出
handle("ads_reporting-get_report_export", (req) => {
  const exportJob = DEMO_REPORT_EXPORTS.find((e) => e.id === req.path.export_id)
  if (!exportJob) {
    return {
      id: req.path.export_id,
      status: "FAILED" as const,
      coverage: {},
    }
  }
  return {
    id: exportJob.id,
    status: exportJob.status,
    coverage: {},
  } as any
})

// 下载报表导出
handle("ads_reporting-download_report_export", (_req) => {
  // 返回 CSV 文本，适配器会根据 responseType 转成 Blob
  return "名称,消耗,D0 ROAS\njiashu-The Billionaire's Secret Bride-demo,125.50,1.15\nwangyan-Reborn as the Alpha's Mate-demo,98.20,1.32\n"
})

// ========== 广告管理 (ad_management) ==========
