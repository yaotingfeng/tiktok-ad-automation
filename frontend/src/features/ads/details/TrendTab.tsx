import { useState } from "react"
import type { ReportRow, TrendPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { AdsTrendChart, formatTrendHour, trendHourZone } from "../AdsTrendChart"
import { type ColumnDef, formatMetric } from "../metricCatalog"
import { type AdsDimension, rowAdvertiserId, rowObjectId } from "../search"
import { useAdsTrend } from "../useAdsTrend"

type Range = "7" | "14" | "30" | "page"
const RANGES: Array<{ value: Range; label: string }> = [
  { value: "7", label: "近 7 天" },
  { value: "14", label: "近 14 天" },
  { value: "30", label: "近 30 天" },
  { value: "page", label: "跟随页面" },
]
// 趋势图可切换的指标（规格 6 趋势页签）。
const TREND_METRICS = [
  "spend",
  "revenue_total",
  "roas_total",
  "roas_d0",
  "impressions",
  "ctr",
  "cpm",
]

function shiftDate(date: string, days: number): string {
  const time = Date.parse(`${date}T00:00:00Z`) + days * 86_400_000
  return new Date(time).toISOString().slice(0, 10)
}

/**
 * 详情趋势的查询参数。对象趋势必须同时带 advertiserIds 与 ids：
 * 否则后端会按整个 BC 的全部账户判断数据覆盖。剧没有平台层级，按其关联系列查询。
 */
export function trendScope(
  row: ReportRow,
  dimension: AdsDimension,
): {
  level: TrendPublic["level"]
  advertiserIds: string[]
  ids: string[]
} {
  if (dimension === "drama") {
    const refs = (row.refs ?? []).filter((ref) => ref.kind === "campaign")
    return {
      level: "campaign",
      advertiserIds: [...new Set(refs.map((ref) => ref.advertiser_id))],
      ids: refs.map((ref) => ref.remote_id),
    }
  }
  if (dimension === "material") {
    // 素材行是一个视频跨账户的合计：覆盖范围取它全部使用位置所在账户。
    const fromUses = (row.material_uses ?? []).map(
      (use) => use.ad_ref.advertiser_id,
    )
    const own = rowAdvertiserId(row)
    const id = rowObjectId(row, dimension)
    return {
      level: "material",
      advertiserIds: [
        ...new Set(fromUses.length ? fromUses : own ? [own] : []),
      ],
      ids: id ? [id] : [],
    }
  }
  const advertiserId = rowAdvertiserId(row)
  const advertiserIds = advertiserId ? [advertiserId] : []
  if (dimension === "account")
    return { level: "account", advertiserIds, ids: [] }
  const id = rowObjectId(row, dimension)
  return { level: dimension, advertiserIds, ids: id ? [id] : [] }
}

function cellText(
  point: NonNullable<TrendPublic["points"]>[number],
  column: ColumnDef,
) {
  const buckets = point.buckets ?? []
  if (!buckets.length) return "—"
  return buckets
    .map((bucket) => {
      const text = formatMetric(
        bucket.values?.[column.key] ?? null,
        column.unit,
      )
      return buckets.length > 1 ? `${bucket.currency} ${text}` : text
    })
    .join(" / ")
}

export function TrendTab({
  row,
  dimension,
  startDate,
  endDate,
  columns,
}: {
  row: ReportRow
  dimension: AdsDimension
  startDate: string
  endDate: string
  columns: ColumnDef[]
}) {
  const [range, setRange] = useState<Range>("14")
  const [hourly, setHourly] = useState(false)
  const [metricKey, setMetricKey] = useState("spend")
  // 页面是单日区间且对象为账户/系列时才提供按小时；按小时固定看页面那一天。
  const hourAllowed =
    startDate === endDate &&
    (dimension === "account" || dimension === "campaign")
  const byHour = hourAllowed && hourly
  const rangeStart =
    byHour || range === "page"
      ? startDate
      : shiftDate(endDate, 1 - Number(range))
  const scope = trendScope(row, dimension)
  const trend = useAdsTrend({
    ...scope,
    startDate: rangeStart,
    endDate,
    granularity: byHour ? "hour" : "day",
    enabled: scope.level === "account" || scope.ids.length > 0,
  })
  const metrics = TREND_METRICS.map((key) =>
    columns.find((column) => column.key === key),
  ).filter((column): column is ColumnDef => Boolean(column))
  // 后端对单一账户时区返回 IANA 名，多账户不同时区返回 MIXED。
  const hourZone = trendHourZone(trend.data?.timezone)
  const active =
    metrics.find((column) => column.key === metricKey) ?? metrics[0]
  return (
    <div className="flex flex-col gap-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {RANGES.map((option) => (
          <Button
            key={option.value}
            type="button"
            size="sm"
            variant={range === option.value && !byHour ? "default" : "outline"}
            disabled={byHour}
            onClick={() => setRange(option.value)}
          >
            {option.label}
          </Button>
        ))}
        {hourAllowed && (
          <Button
            type="button"
            size="sm"
            variant={byHour ? "default" : "outline"}
            aria-pressed={byHour}
            onClick={() => setHourly(!hourly)}
          >
            按小时
          </Button>
        )}
      </div>
      <fieldset className="flex flex-wrap gap-2" aria-label="趋势指标">
        {metrics.map((column) => (
          <Button
            key={column.key}
            type="button"
            size="sm"
            variant={active?.key === column.key ? "secondary" : "ghost"}
            aria-pressed={active?.key === column.key}
            onClick={() => setMetricKey(column.key)}
          >
            {column.label}
          </Button>
        ))}
      </fieldset>
      {byHour && trend.data?.syncing && (
        <p role="status" className="text-xs text-muted-foreground">
          正在拉取小时数据，完成后自动更新
        </p>
      )}
      {active && (
        <AdsTrendChart
          trend={trend.data}
          metric={active}
          loading={trend.isLoading}
          error={trend.isError}
          onRetry={() => void trend.refetch()}
        />
      )}
      {(trend.data?.points?.length ?? 0) > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="whitespace-nowrap">
                  {byHour ? `时间（${hourZone}）` : "日期"}
                </TableHead>
                {columns.map((column) => (
                  <TableHead
                    key={column.key}
                    className="whitespace-nowrap"
                    title={column.fullName}
                  >
                    {column.label}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {trend.data?.points?.map((point) => (
                <TableRow key={point.at}>
                  <TableCell className="whitespace-nowrap">
                    {/* 小时点按对象唯一账户时区显示，时区名只在表头标一次。 */}
                    {byHour ? formatTrendHour(point.at, hourZone) : point.at}
                    {point.coverage === "INCOMPLETE" && (
                      <span className="ml-1 text-xs text-amber-600">
                        不完整
                      </span>
                    )}
                  </TableCell>
                  {columns.map((column) => (
                    <TableCell key={column.key} className="whitespace-nowrap">
                      {cellText(point, column)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
