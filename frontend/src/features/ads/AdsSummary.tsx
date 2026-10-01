import type { ReportRow } from "@/client"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatMetric, rowTargetRoas } from "./search"

type SummaryBucket = {
  values?: Record<string, string | number | null>
}

type AdsSummaryData = {
  buckets?: SummaryBucket[]
  [key: string]: unknown
}

function metricValue(summary: AdsSummaryData | undefined, key: string) {
  const buckets = summary?.buckets ?? []
  const values = buckets
    .map((bucket) => bucket.values?.[key])
    .filter(
      (value): value is string | number =>
        value !== null && value !== undefined,
    )
  if (values.length === 0)
    return summary?.[key] as string | number | null | undefined
  if (values.every((value) => String(value) === String(values[0])))
    return values[0]
  return "多个"
}

function displayValue(value: string | number | null | undefined) {
  return value === "多个" ? value : formatMetric(value)
}

export function AdsSummary({
  summary,
  rows = [],
}: {
  summary?: Record<string, unknown>
  rows?: ReportRow[]
}) {
  const data = summary as AdsSummaryData | undefined
  const d0 = metricValue(data, "d0_roas") ?? metricValue(data, "actual_d0_roas")
  const targets = rows
    .map(rowTargetRoas)
    .filter((value): value is string => value !== null)
  const target =
    targets.length === 0
      ? rows.length > 0
        ? "目录未同步"
        : null
      : targets.every((value) => value === targets[0])
        ? targets[0]
        : "多个"
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            消耗
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {displayValue(metricValue(data, "spend"))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            D0 收入
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {displayValue(metricValue(data, "native_growth_ad_revenue_value_d0"))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            实际 D0 ROAS
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {displayValue(d0)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            目标 ROAS
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {target === "多个" || target === "目录未同步"
            ? target
            : formatMetric(target)}
        </CardContent>
      </Card>
    </div>
  )
}
