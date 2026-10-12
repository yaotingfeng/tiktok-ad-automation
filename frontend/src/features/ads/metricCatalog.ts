import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type MetricDefinitionPublic } from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"
import type { AdsDimension } from "./search"

export type MetricUnit = MetricDefinitionPublic["unit"]
export type ColumnDef = {
  key: string
  label: string
  fullName: string
  unit: MetricUnit
  recoveryDay: number | null
}

// 本地存储列 dN 与官方天数的对应：revenue_d3 是官方"第 2 天"，依此类推。
const RECOVERY_DAYS: Record<string, number> = {
  d0: 0,
  d3: 2,
  d7: 6,
  d14: 13,
  d30: 29,
}

export function recoveryDayOf(key: string): number | null {
  const match = /_(d\d+)$/.exec(key)
  return match ? (RECOVERY_DAYS[match[1]] ?? null) : null
}

function localToday(timezone: string, now: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now)
}

/** 第 N 天指标在点击后 (N+1)×24 小时内仍会增长；任一账户未满窗口即视为未结束。 */
export function isRecoveryOpen(
  day: number,
  endDate: string,
  timezones: string[],
  now: Date = new Date(),
): boolean {
  const end = Date.parse(`${endDate}T00:00:00Z`)
  return timezones.some((zone) => {
    const today = Date.parse(`${localToday(zone, now)}T00:00:00Z`)
    const daysPassed = (today - end) / 86_400_000
    return daysPassed < day + 1
  })
}

export function formatMetric(
  value: string | null | undefined,
  unit: MetricUnit,
): string {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  if (!Number.isFinite(number)) return "—"
  if (unit === "ratio") return `${(number * 100).toFixed(2)}%`
  if (unit === "count") return Math.round(number).toLocaleString("en-US")
  return number.toFixed(2)
}

export function columnsFor(
  catalog: MetricDefinitionPublic[],
  dimension: AdsDimension,
): ColumnDef[] {
  const material = dimension === "material"
  return catalog
    .filter((item) => !material || item.material_field !== null)
    .map((item) => ({
      key: item.key,
      label: (material ? item.material_label : null) ?? item.basic_label,
      fullName: (material ? item.material_name_zh : null) ?? item.basic_name_zh,
      unit: item.unit,
      recoveryDay: recoveryDayOf(item.key),
    }))
}

export function useMetricCatalog() {
  const { tenantId } = useTenantScope()
  return useQuery({
    queryKey: ["tenant", tenantId, "ads-metric-catalog"],
    enabled: Boolean(tenantId),
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: async () =>
      (
        await AdsReportingService.metricCatalog({
          path: { tenant_id: tenantId! },
        })
      ).data,
  })
}
