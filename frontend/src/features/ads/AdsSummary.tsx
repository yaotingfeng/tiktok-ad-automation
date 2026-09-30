import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatMetric } from "./search"
export function AdsSummary({ summary }: { summary?: Record<string, unknown> }) {
  const d0 = summary?.d0_roas ?? summary?.actual_d0_roas
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            消耗
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {formatMetric(summary?.spend as string | null)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            D0 收入
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {formatMetric(
            summary?.native_growth_ad_revenue_value_d0 as string | null,
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            实际 D0 ROAS
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {formatMetric(d0 as string | null)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            目标 ROAS
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xl font-semibold">
          {summary?.target_roas == null
            ? "—"
            : formatMetric(summary.target_roas as string)}
        </CardContent>
      </Card>
    </div>
  )
}
