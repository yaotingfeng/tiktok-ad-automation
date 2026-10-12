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
  snapshot_id?: string | null
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
  // 工作台按已审定规则默认今日；日期交给后端按各账户本地日解释。
  const today = new Date()
  const defaultDate = [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, "0"),
    String(today.getDate()).padStart(2, "0"),
  ].join("-")
  return {
    // 默认从账户看起，按消耗降序，先看花钱最多的对象。
    dimension: "account",
    start_date: defaultDate,
    end_date: defaultDate,
    query: "",
    page: 1,
    limit: 50,
    sort_by: "spend",
    sort_direction: "desc",
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
export function toReportingFilter(search: AdsSearch): ReportingFilter_Input {
  const {
    page: _page,
    limit: _limit,
    cursor: _cursor,
    snapshot_id: _snapshot,
    ...filters
  } = search
  return filters
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
export function rowTargetRoas(row: ReportRow): string | null {
  const display = row.display ?? {}
  // 目标 ROAS 来自已同步的目录配置，和报表收入/消耗指标分开显示。
  return display.target_roas ?? null
}
export function rowOptimizationGoal(row: ReportRow): string | null {
  const value = row.display?.optimization_goal
  if (!value) return null
  const normalized = value.toUpperCase().replace(/_/g, " ")
  if (normalized.includes("VALUE")) return "最大价值"
  if (normalized.includes("ROAS")) return "目标 ROAS"
  return value
}
// 指标单元格只显示数值；没有值时统一显示“—”，原因放在悬停说明里，
// 避免同步未完成时整表、整排汇总卡片都是“数据缺失”之类的提示文字。
const UNAVAILABLE_REASONS: Record<string, string> = {
  UNSUPPORTED: "平台未提供该指标",
  UNAVAILABLE: "暂不可用",
  FAILED: "读取失败，请刷新重试",
  INCOMPLETE: "该范围数据尚未同步完整",
}
/** 表格单元格"—"的悬停说明。 */
export function unavailableReason(availability: string): string {
  return UNAVAILABLE_REASONS[availability] ?? "暂无数据，同步完成后显示"
}
export function availabilityLabel(
  availability: string,
  value: string | null,
  options?: { percent?: boolean },
): string {
  if (availability === "MULTI") return value ?? "—"
  if (availability !== "AVAILABLE" || value === null) return "—"
  if (options?.percent) {
    const number = Number(value)
    return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : "—"
  }
  return formatMetric(value)
}
/** “—”对应的原因说明；有值时不返回说明。 */
export function availabilityReason(
  availability: string,
  value: string | null,
): string | undefined {
  if (
    availability === "MULTI" ||
    (availability === "AVAILABLE" && value !== null)
  )
    return undefined
  return unavailableReason(availability)
}
function formatMetric(
  value: string | number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : "—"
}

const NAME_KEY: Record<AdsDimension, string> = {
  account: "account_name",
  campaign: "campaign_name",
  adgroup: "adgroup_name",
  ad: "ad_name",
  material: "material_name",
  drama: "drama_name",
}
// 名称单元格的 ID 行；后端在维度行上统一给出 remote_id，素材用主素材 ID。
const ID_KEY: Partial<Record<AdsDimension, string>> = {
  account: "account_id",
  campaign: "campaign_id",
  adgroup: "adgroup_id",
  ad: "ad_id",
  material: "main_material_id",
}
// 逐级下钻：账户 → 系列 → 广告组 → 广告 → 素材；素材、剧没有下级。
const CHILD: Partial<Record<AdsDimension, AdsDimension>> = {
  account: "campaign",
  campaign: "adgroup",
  adgroup: "ad",
  ad: "material",
}
export const nameKeyFor = (dimension: AdsDimension) => NAME_KEY[dimension]
export const childDimension = (dimension: AdsDimension) =>
  CHILD[dimension] ?? null

/** 当前维度对象自身的 ID；剧维度没有平台 ID。 */
export function rowObjectId(
  row: ReportRow,
  dimension: AdsDimension,
): string | null {
  if (dimension === "drama") return null
  const display = row.display ?? {}
  const idKey = ID_KEY[dimension]
  return (
    display.remote_id ??
    (idKey ? display[idKey] : null) ??
    row.refs?.[0]?.remote_id ??
    null
  )
}

export type ParentLink = {
  kind: "account" | "campaign" | "adgroup"
  id: string
  name: string
}
/** 名称下方的上级路径；只列出比当前维度更高的层级，素材与剧不显示路径。 */
export function parentPath(
  row: ReportRow,
  dimension: AdsDimension,
): ParentLink[] {
  const display = row.display ?? {}
  const order = ["account", "campaign", "adgroup"] as const
  const depth = {
    account: 0,
    campaign: 1,
    adgroup: 2,
    ad: 3,
    material: 0,
    drama: 0,
  }[dimension]
  return order
    .slice(0, depth)
    .map((kind) => ({
      kind,
      id: display[`${kind}_id`] ?? "",
      name: display[`${kind}_name`] ?? display[`${kind}_id`] ?? "",
    }))
    .filter((link) => link.id)
}
/** 单个币种桶里的指标值与可用状态；多币种时由调用方逐桶渲染。 */
export function metricOf(row: ReportRow, key: string, bucket = 0) {
  const vector = row.metric_buckets?.[bucket]
  const value = vector?.values?.[key] ?? null
  return {
    value,
    availability:
      vector?.availability?.[key] ?? (value === null ? "MISSING" : "AVAILABLE"),
    currency: vector?.currency ?? "",
  }
}
/** 行所属账户 ID：账户行在 display，其余行优先 display，再退到对象引用。 */
export function rowAdvertiserId(row: ReportRow): string {
  const display = row.display ?? {}
  return (
    display.account_id ??
    display.advertiser_id ??
    row.refs?.[0]?.advertiser_id ??
    ""
  )
}
/**
 * 由上级路径或剧的关联系列构造详情用的行：只有身份字段，没有指标，
 * 也不在当前快照里，因此不能作为冻结选择的行键。
 */
export function detailRowFor(
  link: ParentLink,
  advertiserId: string,
): ReportRow {
  return {
    row_key: `detail:${link.kind}:${advertiserId}:${link.id}`,
    display: {
      name: link.name,
      [`${link.kind}_name`]: link.name,
      [`${link.kind}_id`]: link.id,
      remote_id: link.id,
      account_id: advertiserId,
      advertiser_id: advertiserId,
    },
    refs: [],
    metric_buckets: [],
  }
}
