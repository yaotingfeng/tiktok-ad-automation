import type { TrendPublic } from "@/client"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatMetric } from "./search"
export function AdsTrend({ trend }: { trend?: TrendPublic | null }) {
  const points = trend?.points ?? []
  const values = points
    .map((point) => Number(point.values?.d0_roas ?? Number.NaN))
    .filter(Number.isFinite)
  const max = Math.max(...values, 1)
  const width = 640
  const height = 160
  const polyline = values
    .map(
      (value, index) =>
        `${(index / Math.max(values.length - 1, 1)) * width},${height - (value / max) * (height - 16)}`,
    )
    .join(" ")
  return (
    <Card>
      <CardHeader>
        <CardTitle>趋势</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <svg
          role="img"
          aria-label="D0 ROAS 趋势图"
          viewBox={`0 0 ${width} ${height}`}
          className="h-40 w-full"
          preserveAspectRatio="none"
        >
          <polyline
            fill="none"
            stroke="currentColor"
            strokeWidth="3"
            points={polyline}
          />
        </svg>
        <table className="sr-only">
          <caption>D0 ROAS 趋势数据</caption>
          <thead>
            <tr>
              <th>时间</th>
              <th>D0 ROAS</th>
            </tr>
          </thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.bucket_start}>
                <td>{point.bucket_start}</td>
                <td>{formatMetric(point.values?.d0_roas)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!points.length && (
          <p className="text-sm text-muted-foreground">暂无趋势数据</p>
        )}
      </CardContent>
    </Card>
  )
}
