import type {
  adsReportingQueryAdsData,
  ReportingFilter_Input,
  ReportRow,
} from "@/client"

export type AdsDimension =
  | "account"
  | "campaign"
  | "adgroup"
  | "ad"
  | "material"
  | "drama"
export type AdsSearch = Omit<
  ReportingFilter_Input,
  "dimension" | "start_date" | "end_date"
> & {
  dimension: AdsDimension
  start_date: string
  end_date: string
  page: number
  limit: number
  cursor?: string | null
}
export const DIMENSIONS: Array<{ value: AdsDimension; label: string }> = [
  { value: "account", label: "账户" },
  { value: "campaign", label: "系列" },
  { value: "adgroup", label: "广告组" },
  { value: "ad", label: "广告" },
  { value: "material", label: "素材" },
  { value: "drama", label: "剧" },
]
export function defaultAdsSearch(): AdsSearch {
  const today = new Date().toISOString().slice(0, 10)
  return {
    dimension: "campaign",
    start_date: today,
    end_date: today,
    query: "",
    page: 1,
    limit: 50,
    sort_by: "row_key",
    sort_direction: "asc",
  }
}
/** 将页面状态映射为生成客户端查询合同，避免直接拼 HTTP 请求。 */
export function toAdsQuery(
  search: AdsSearch,
  snapshotId?: string,
): Omit<adsReportingQueryAdsData["query"], "bc_id"> {
  const { page: _page, ...filters } = search
  return {
    ...filters,
    query: filters.query || undefined,
    ids: filters.ids?.length ? filters.ids.join(",") : undefined,
    advertiser_id: filters.advertiser_ids?.[0],
    ad_types: filters.ad_types?.join(","),
    operation_statuses: filters.operation_statuses?.join(","),
    review_statuses: filters.review_statuses?.join(","),
    budget_modes: filters.budget_modes?.join(","),
    snapshot_id: snapshotId,
  }
}
export function rowName(row: ReportRow): string {
  const display = row.display ?? {}
  return (
    display.name ??
    display.campaign_name ??
    display.ad_name ??
    display.title ??
    row.row_key
  )
}
export function rowDrama(row: ReportRow): string | null {
  const display = row.display ?? {}
  if (display.drama_name || display.drama)
    return display.drama_name ?? display.drama ?? null
  const parts = rowName(row).split("-")
  return parts.length > 1 && parts[1].trim() ? parts[1].trim() : null
}
export function rowMetric(
  row: ReportRow,
  metric: string,
): { value: string | null; availability: string } {
  const bucket = row.metric_buckets?.[0]
  const value = bucket?.values?.[metric] ?? null
  return {
    value,
    availability:
      bucket?.availability?.[metric] ??
      (value === null ? "MISSING" : "AVAILABLE"),
  }
}
export function formatMetric(
  value: string | number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : "—"
}
/** 只允许已核验的 TikTok 后台固定路由，素材/剧没有可安全拼接的官方链接。 */
export function officialPlatformUrl(row: ReportRow): string | null {
  const ref = row.refs?.[0]
  if (!ref || !["campaign", "adgroup", "ad"].includes(ref.kind)) return null
  if (
    !/^[A-Za-z0-9_-]{1,128}$/.test(ref.remote_id) ||
    !/^[A-Za-z0-9_-]{1,128}$/.test(ref.advertiser_id)
  )
    return null
  const route =
    ref.kind === "campaign"
      ? "campaign"
      : ref.kind === "adgroup"
        ? "adgroup"
        : "ad"
  return `https://ads.tiktok.com/i18n/${route}/manage?aadvid=${encodeURIComponent(ref.advertiser_id)}&id=${encodeURIComponent(ref.remote_id)}`
}
