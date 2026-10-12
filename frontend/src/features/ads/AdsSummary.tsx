import { AlertCircle } from "lucide-react"
import type { MetricDefinitionPublic } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatMetric } from "./metricCatalog"

type SummaryBucket = {
  currency: string
  values?: Record<string, string | null>
  availability?: Record<string, string>
}

type AdsSummaryProps = {
  summary?: {
    buckets?: SummaryBucket[]
    incomplete_advertiser_count?: number
    incomplete_dates?: string[]
  }
  catalog: MetricDefinitionPublic[]
  activeMetric: string
  onSelectMetric: (key: string) => void
}

// 汇总卡片的 7 个指标（规格 §3.3）
const SUMMARY_KEYS = [
  "spend",
  "revenue_total",
  "roas_total",
  "roas_d0",
  "impressions",
  "ctr",
  "cpm",
]

export function AdsSummary({
  summary,
  catalog,
  activeMetric,
  onSelectMetric,
}: AdsSummaryProps) {
  const buckets = summary?.buckets ?? []
  const incompleteCount = summary?.incomplete_advertiser_count ?? 0
  const incompleteDates = summary?.incomplete_dates ?? []

  // 等待指标字典加载
  if (catalog.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {SUMMARY_KEYS.map((key) => {
          const def = catalog.find((d) => d.key === key)
          if (!def) return null
          const active = activeMetric === key

          return (
            <Button
              key={key}
              variant="outline"
              className={`h-auto flex-col items-start p-0 ${
                active ? "ring-2 ring-primary" : ""
              }`}
              onClick={() => onSelectMetric(key)}
              aria-pressed={active}
            >
              <Card className="w-full border-none shadow-none">
                <CardHeader className="pb-2">
                  <CardTitle
                    className="text-sm font-medium text-muted-foreground"
                    title={def.basic_name_zh}
                  >
                    {def.basic_label}
                  </CardTitle>
                </CardHeader>
                <CardContent className="text-xl font-semibold">
                  {buckets.length > 0 ? (
                    buckets
                      .map((bucket) => {
                        const rawValue = bucket.values?.[key]
                        const availability = bucket.availability?.[key]
                        const formatted = formatMetric(rawValue, def.unit)

                        // PARTIAL 仍显示数值，加一个小标记
                        const isPartial = availability === "PARTIAL"
                        const isUnsupported = availability === "UNSUPPORTED"

                        if (isUnsupported) {
                          return "—"
                        }

                        const prefix =
                          buckets.length > 1 ? `${bucket.currency} ` : ""
                        const suffix = isPartial ? " 部分" : ""
                        return `${prefix}${formatted}${suffix}`
                      })
                      .join(" / ")
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </CardContent>
              </Card>
            </Button>
          )
        })}
      </div>

      {incompleteCount > 0 && (
        <div
          className="flex items-center gap-2 text-sm text-muted-foreground"
          title={
            incompleteDates.length > 0
              ? `缺失日期：${incompleteDates.join("、")}`
              : undefined
          }
        >
          <AlertCircle className="h-4 w-4" />
          <span>{incompleteCount} 个账户数据不完整</span>
        </div>
      )}
    </div>
  )
}
