import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatMetric } from "./search"

type SummaryBucket = {
  currency?: string
  timezone?: string
  attribution?: string
  values?: Record<string, string | number | null>
}

type AdsSummaryData = {
  buckets?: SummaryBucket[]
  [key: string]: unknown
}

function metricValue(summary: AdsSummaryData | undefined, key: string) {
  const buckets = summary?.buckets ?? []
  const coordinates = new Set(
    buckets.map(
      (bucket) => `${bucket.currency}|${bucket.timezone}|${bucket.attribution}`,
    ),
  )
  if (coordinates.size > 1) return "多口径"
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
  return value === "多个" || value === "多口径" ? value : formatMetric(value)
}

function ratioValue(
  summary: AdsSummaryData | undefined,
  numerator: string,
  denominator: string,
) {
  const top = metricValue(summary, numerator)
  const bottom = metricValue(summary, denominator)
  if (
    top === "多个" ||
    top === "多口径" ||
    bottom === "多个" ||
    bottom === "多口径"
  )
    return "多口径"
  const numeratorValue = Number(top)
  const denominatorValue = Number(bottom)
  if (
    !Number.isFinite(numeratorValue) ||
    !Number.isFinite(denominatorValue) ||
    denominatorValue === 0
  )
    return null
  return numeratorValue / denominatorValue
}

function percentValue(value: string | number | null | undefined) {
  if (value === "多个" || value === "多口径") return value
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : "—"
}

export function AdsSummary({ summary }: { summary?: Record<string, unknown> }) {
  const data = summary as AdsSummaryData | undefined
  const revenue = metricValue(data, "native_growth_total_ad_impression_value")
  const revenueRoas = ratioValue(
    data,
    "native_growth_total_ad_impression_value",
    "spend",
  )
  const ctr = ratioValue(data, "clicks", "impressions")
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
            广告收入
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {displayValue(revenue)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            广告收益 ROAS
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {displayValue(revenueRoas)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            点击率
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {percentValue(ctr)}
        </CardContent>
      </Card>
    </div>
  )
}
