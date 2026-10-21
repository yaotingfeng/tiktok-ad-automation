// 广告报表与管理的私有数据与状态。
import type { DemoLaunchedCampaign } from "./world"
import { seeded } from "./world"

/** 报表导出任务。 */
export interface DemoReportExport {
  id: string
  status: "QUEUED" | "GENERATING" | "COMPLETE" | "FAILED"
  created_at: string
  completed_at: string | null
  dimension: string
  row_count: number | null
}

export const DEMO_REPORT_EXPORTS: DemoReportExport[] = []

/** 报表视图（保存的筛选配置）。 */
export interface DemoReportView {
  id: string
  name: string
  filters: Record<string, unknown>
  columns: string[]
  created_at: string
}

export const DEMO_REPORT_VIEWS: DemoReportView[] = []

/** 生成指标值：确定性随机，量级逼真。 */
export function generateMetrics(
  campaign: DemoLaunchedCampaign,
  date: string,
  level: "campaign" | "adgroup" | "ad",
  levelId: string,
) {
  const seed = `${levelId}-${date}`
  const ageDays = campaign.age_days
  // 未来可能需要 daysSinceLaunch 来计算日期偏移
  // const daysSinceLaunch = Math.floor(
  //   (Date.now() - new Date(campaign.created_at).getTime()) / 86400000,
  // )
  // const dayOffset = Math.max(0, daysSinceLaunch - ageDays)

  // 起量—稳定—衰退的生命周期曲线
  let lifeCycleFactor = 1.0
  if (ageDays <= 3) {
    // 起量期：第一天 30%，第二天 60%，第三天 80%
    lifeCycleFactor = Math.min(1.0, 0.3 + ageDays * 0.25)
  } else if (ageDays > 21) {
    // 衰退期：21 天后每天衰减 2%
    lifeCycleFactor = Math.max(0.4, 1.0 - (ageDays - 21) * 0.02)
  }

  // 基础日消耗：Campaign 50-300，广告组 20-100，广告 10-60
  const baseSpend =
    level === "campaign"
      ? 80 + seeded(seed) * 220
      : level === "adgroup"
        ? 30 + seeded(`${seed}g`) * 70
        : 15 + seeded(`${seed}a`) * 45

  const spend =
    baseSpend * lifeCycleFactor * (0.85 + seeded(`${seed}var`) * 0.3)

  // ROAS：0.6–1.8，目标 ROAS 附近波动
  const targetRoas = campaign.target_roas ?? 1.0
  const roasD0 = Math.max(
    0.5,
    Math.min(2.0, targetRoas * (0.85 + seeded(`${seed}roas`) * 0.3)),
  )
  const roasTotal = roasD0 * (1.2 + seeded(`${seed}rt`) * 0.3)

  const revenueD0 = spend * roasD0
  const revenueTotal = spend * roasTotal

  // 展示、点击
  const impressions = Math.round(spend * (800 + seeded(`${seed}imp`) * 600))
  const clicks = Math.round(
    impressions * (0.015 + seeded(`${seed}ctr`) * 0.025),
  )
  const ctr = clicks / impressions

  // 广告展示事件（IAA）
  const adImpressionEvents = Math.round(
    clicks * (0.4 + seeded(`${seed}aie`) * 0.3),
  )

  return {
    spend: spend.toFixed(2),
    revenue_d0: revenueD0.toFixed(2),
    revenue_total: revenueTotal.toFixed(2),
    roas_d0: roasD0.toFixed(3),
    roas_d3: (roasD0 * 1.05).toFixed(3),
    roas_d7: (roasD0 * 1.12).toFixed(3),
    roas_d14: (roasD0 * 1.18).toFixed(3),
    roas_d30: (roasD0 * 1.25).toFixed(3),
    roas_total: roasTotal.toFixed(3),
    impressions: impressions.toString(),
    clicks: clicks.toString(),
    ctr: ctr.toFixed(4),
    cpm: ((spend / impressions) * 1000).toFixed(2),
    cpc: (spend / clicks).toFixed(2),
    ad_impression_events: adImpressionEvents.toString(),
    cost_per_ad_impression_event: (spend / adImpressionEvents).toFixed(2),
  }
}

/** 从 Campaign 名称解析版权方和剧名（按设计规则：版权方-剧名-备注）。 */
export function parseDramaFromName(campaignName: string): {
  provider: string
  dramaName: string
  isValid: boolean
} {
  const parts = campaignName.split("-").map((p) => p.trim())
  if (parts.length >= 2 && parts[0] && parts[1]) {
    return { provider: parts[0], dramaName: parts[1], isValid: true }
  }
  return { provider: "", dramaName: "", isValid: false }
}
