import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"
import type { TrendPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"
import { Skeleton } from "@/components/ui/skeleton"
import type { ColumnDef } from "./metricCatalog"
import { formatMetric } from "./metricCatalog"

/**
 * 小时趋势点是 UTC 的 ISO 分钟串。对象只有单一账户时区时按该时区显示；
 * 多时区（MIXED）或时区未知/非法时退回 UTC。
 */
export function trendHourZone(timezone?: string | null): string {
  if (!timezone || timezone === "MIXED") return "UTC"
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone })
    return timezone
  } catch {
    return "UTC"
  }
}

export function formatTrendHour(at: string, zone: string): string {
  const time = Date.parse(at)
  if (!Number.isFinite(time)) return at
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(time)
}

type AdsTrendChartProps = {
  trend: TrendPublic | undefined
  metric: ColumnDef
  loading: boolean
  error: boolean
  onRetry: () => void
}

export function AdsTrendChart({
  trend,
  metric,
  loading,
  error,
  onRetry,
}: AdsTrendChartProps) {
  if (loading) {
    return <Skeleton className="h-[300px] w-full" />
  }

  if (error) {
    return (
      <div className="flex h-[300px] items-center justify-center gap-3">
        <p className="text-sm text-muted-foreground">趋势读取失败</p>
        <Button variant="outline" size="sm" onClick={onRetry}>
          重试
        </Button>
      </div>
    )
  }

  if (!trend?.points || trend.points.length === 0) {
    return (
      <div className="flex h-[300px] items-center justify-center">
        <p className="text-sm text-muted-foreground">暂无趋势数据</p>
      </div>
    )
  }

  // 提取所有币种
  const currencies = [
    ...new Set(
      trend.points.flatMap((p) => p.buckets?.map((b) => b.currency) ?? []),
    ),
  ]

  // 转换数据：每个点包含各币种的值
  const data = trend.points.map((point) => {
    const entry: Record<string, string | number | null> = {
      at: point.at,
      coverage: point.coverage ?? "MISSING",
    }
    for (const bucket of point.buckets ?? []) {
      const rawValue = bucket.values?.[metric.key]
      entry[bucket.currency] =
        rawValue == null ? null : Number.parseFloat(rawValue)
    }
    return entry
  })

  // X 轴格式化：日粒度显示 MM-DD，小时粒度显示 HH:00
  const formatXAxis = (value: unknown) => {
    // 防御 null/undefined/非字符串
    if (typeof value !== "string" || !value) return ""
    if (trend.granularity === "hour") {
      // 单一时区按该时区显示；退回 UTC 时（多时区/未知）明确标注 UTC。
      const zone = trendHourZone(trend.timezone)
      const suffix = zone !== trend.timezone ? " UTC" : ""
      return `${formatTrendHour(value, zone)}${suffix}`
    }
    // 日粒度：YYYY-MM-DD -> MM-DD
    const match = /-(\d{2}-\d{2})$/.exec(value)
    return match ? match[1] : value
  }

  // 图表配置：每个币种一个颜色
  const chartConfig: Record<string, { label: string; color: string }> = {}
  // 主题色变量是 oklch 值，必须直接用 var(--chart-N)；包一层 hsl() 会成为非法颜色，
  // 折线 stroke 失效而看不见。
  const colors = [
    "var(--chart-1)",
    "var(--chart-2)",
    "var(--chart-3)",
    "var(--chart-4)",
    "var(--chart-5)",
  ]
  currencies.forEach((currency, index) => {
    chartConfig[currency] = {
      label: currency,
      color: colors[index % colors.length],
    }
  })

  return (
    <div data-testid="ads-trend-chart" className="space-y-3">
      <ChartContainer config={chartConfig} className="h-[300px] w-full">
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis
            dataKey="at"
            tickFormatter={formatXAxis}
            tick={{ fontSize: 12 }}
          />
          <YAxis tick={{ fontSize: 12 }} />
          <ChartTooltip
            content={
              <ChartTooltipContent
                labelFormatter={(value) => {
                  const point = trend.points?.find((p) => p.at === value)
                  const coverage = point?.coverage
                  const label = formatXAxis(value)
                  if (coverage === "INCOMPLETE") {
                    return `${label}（该日数据不完整）`
                  }
                  if (coverage === "MISSING") {
                    return `${label}（该时段无数据）`
                  }
                  return label
                }}
                formatter={(value) => formatMetric(String(value), metric.unit)}
              />
            }
          />
          <ChartLegend content={<ChartLegendContent />} />
          {currencies.map((currency) => (
            <Line
              key={currency}
              type="monotone"
              dataKey={currency}
              stroke={chartConfig[currency].color}
              strokeWidth={2}
              dot={{ r: 3 }}
              connectNulls={false}
              // 关闭入场动画，保证截图与用例里折线立即完整绘制。
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ChartContainer>
      {trend.timezone === "MIXED" && (
        <p className="text-xs text-muted-foreground">各账户按本地日期汇总</p>
      )}
    </div>
  )
}
