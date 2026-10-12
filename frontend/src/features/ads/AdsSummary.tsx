import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { availabilityLabel, availabilityReason } from "./search"

type SummaryBucket = {
  currency: string
  values?: Record<string, string | null>
  availability?: Record<string, string>
}
const COLUMNS = [
  { key: "spend", label: "消耗" },
  { key: "revenue_total", label: "总收入" },
  { key: "roas_total", label: "总收入 ROAS" },
  { key: "ctr", label: "点击率" },
]

export function AdsSummary({ summary }: { summary?: Record<string, unknown> }) {
  const buckets = (summary?.buckets ?? []) as SummaryBucket[]
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {COLUMNS.map((column) => (
        <Card key={column.key}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              {column.label}
            </CardTitle>
          </CardHeader>
          <CardContent
            className="text-xl font-semibold"
            title={
              buckets.length
                ? buckets
                    .map((bucket) => {
                      const value = bucket.values?.[column.key] ?? null
                      return availabilityReason(
                        bucket.availability?.[column.key] ??
                          (value === null ? "MISSING" : "AVAILABLE"),
                        value,
                      )
                    })
                    .filter(Boolean)
                    .join("；") || undefined
                : "暂无数据，同步完成后显示"
            }
          >
            {buckets.length
              ? buckets
                  .map((bucket) => {
                    const value = bucket.values?.[column.key] ?? null
                    // 后端已按币种合计并重算比率；前端保留 NULL 和每一份币种结果。
                    const text = availabilityLabel(
                      bucket.availability?.[column.key] ??
                        (value === null ? "MISSING" : "AVAILABLE"),
                      value,
                      { percent: column.key === "ctr" },
                    )
                    return `${buckets.length > 1 ? `${bucket.currency} ` : ""}${text}`
                  })
                  .join(" / ")
              : "—"}
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
