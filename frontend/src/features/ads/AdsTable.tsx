import { ArrowDown, ArrowUp, ArrowUpDown, Info } from "lucide-react"
import type { ReportRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  type AdsDimension,
  availabilityLabel,
  childDimensionForColumn,
  formatMetric,
  officialPlatformUrl,
  rowBudget,
  rowHierarchyId,
  rowMetric,
  rowName,
  rowOptimizationGoal,
  targetRoasLabel,
} from "./search"

type DisplayColumn = { key: string; label: string }

// 表头排序字段与后端 SUPPORTED_SORT_FIELDS 一一对应；每列按自己的字段排序。
const METRIC_COLUMNS: Array<{ key: string; label: string }> = [
  { key: "spend", label: "消耗" },
  { key: "roas_total", label: "总收入 ROAS" },
  ...["d0", "d3", "d7", "d14", "d30"].map((day) => ({
    key: `roas_${day}`,
    label: `${day.toUpperCase()} ROAS`,
  })),
  { key: "revenue_total", label: "总收入" },
  { key: "ad_impression_events", label: "广告展示事件总数" },
  { key: "cost_per_ad_impression_event", label: "单次广告展示事件成本" },
  { key: "ctr", label: "点击率" },
]

function SortableHead({
  label,
  sortBy,
  activeSortBy,
  direction,
  onSort,
}: {
  label: string
  sortBy: string
  activeSortBy: string | undefined
  direction: "asc" | "desc"
  onSort: (sortBy: string) => void
}) {
  const active = activeSortBy === sortBy
  const Icon = active
    ? direction === "asc"
      ? ArrowUp
      : ArrowDown
    : ArrowUpDown
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-8 gap-1 px-1 font-medium"
      onClick={() => onSort(sortBy)}
      aria-label={`${label}${active ? (direction === "asc" ? "，当前升序" : "，当前降序") : "，点击排序"}`}
    >
      {label}
      <Icon className="size-3.5" aria-hidden="true" />
    </Button>
  )
}

function displayColumns(dimension: AdsDimension): DisplayColumn[] {
  switch (dimension) {
    case "account":
      return [{ key: "account_name", label: "账户" }]
    case "campaign":
      return [
        { key: "account_name", label: "账户" },
        { key: "campaign_name", label: "系列" },
      ]
    case "adgroup":
      return [
        { key: "account_name", label: "账户" },
        { key: "campaign_name", label: "系列" },
        { key: "adgroup_name", label: "广告组" },
      ]
    case "ad":
      return [
        { key: "account_name", label: "账户" },
        { key: "campaign_name", label: "系列" },
        { key: "adgroup_name", label: "广告组" },
        { key: "ad_name", label: "广告" },
      ]
    case "material":
      return [{ key: "material_name", label: "素材" }]
    case "drama":
      return [{ key: "drama_name", label: "剧" }]
  }
}

function displayText(row: ReportRow, key: string) {
  return (
    row.display?.[key] ??
    (key === "drama_name" ? row.display?.name : null) ??
    "—"
  )
}
export function AdsTable({
  rows,
  dimension,
  selected,
  allMatching,
  total,
  onToggle,
  onSelectAll,
  onOpen,
  onNavigate,
  nextCursor,
  hasPrevious,
  onNext,
  onPrevious,
  sortBy,
  sortDirection,
  onSort,
}: {
  rows: ReportRow[]
  dimension: AdsDimension
  selected: Set<string>
  allMatching: boolean
  total: number
  onToggle: (row: ReportRow, checked: boolean) => void
  onSelectAll: () => void
  onOpen: (row: ReportRow) => void
  onNavigate: (row: ReportRow, columnKey: string) => void
  nextCursor?: string | null
  hasPrevious: boolean
  onNext: () => void
  onPrevious: () => void
  sortBy: string
  sortDirection: "asc" | "desc"
  onSort: (sortBy: string) => void
}) {
  const columns = displayColumns(dimension)
  const currentAll =
    rows.length > 0 && rows.every((row) => selected.has(row.row_key))
  return (
    <Card>
      <CardContent className="p-0">
        {allMatching && (
          <div className="border-b bg-muted/40 px-4 py-2 text-sm">
            已选择全部 {total} 条匹配结果，可取消单行。
          </div>
        )}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">
                <Checkbox
                  checked={currentAll}
                  onCheckedChange={(checked) => {
                    if (checked) onSelectAll()
                  }}
                  aria-label="选择全部匹配结果"
                />
              </TableHead>
              {columns.map((column, index) => (
                <TableHead
                  key={column.key}
                  className={
                    index === 0
                      ? "sticky left-12 z-10 bg-background"
                      : undefined
                  }
                >
                  <SortableHead
                    label={column.label}
                    sortBy={column.key}
                    activeSortBy={sortBy}
                    direction={sortDirection}
                    onSort={onSort}
                  />
                </TableHead>
              ))}
              <TableHead>状态</TableHead>
              {dimension === "account" && (
                <TableHead>
                  <SortableHead
                    label="余额"
                    sortBy="balance"
                    activeSortBy={sortBy}
                    direction={sortDirection}
                    onSort={onSort}
                  />
                </TableHead>
              )}
              {METRIC_COLUMNS.map((column) => (
                <TableHead key={column.key}>
                  <SortableHead
                    label={column.label}
                    sortBy={column.key}
                    activeSortBy={sortBy}
                    direction={sortDirection}
                    onSort={onSort}
                  />
                </TableHead>
              ))}
              {(dimension === "campaign" || dimension === "adgroup") && (
                <TableHead>
                  <SortableHead
                    label={dimension === "campaign" ? "系列预算" : "广告组预算"}
                    sortBy={
                      dimension === "campaign"
                        ? "campaign_budget"
                        : "adgroup_budget"
                    }
                    activeSortBy={sortBy}
                    direction={sortDirection}
                    onSort={onSort}
                  />
                </TableHead>
              )}
              {(dimension === "campaign" ||
                dimension === "adgroup" ||
                dimension === "ad") && (
                <TableHead>
                  <SortableHead
                    label="目标 ROAS"
                    sortBy="target_roas"
                    activeSortBy={sortBy}
                    direction={sortDirection}
                    onSort={onSort}
                  />
                </TableHead>
              )}
              {(dimension === "campaign" ||
                dimension === "adgroup" ||
                dimension === "ad") && <TableHead>优化目标</TableHead>}
              <TableHead>数据状态</TableHead>
              <TableHead>详情</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => {
              const url = officialPlatformUrl(row)
              return (
                <TableRow key={row.row_key}>
                  <TableCell>
                    <Checkbox
                      checked={selected.has(row.row_key)}
                      onCheckedChange={(checked) =>
                        onToggle(row, checked === true)
                      }
                      aria-label={`选择 ${rowName(row)}`}
                    />
                  </TableCell>
                  {columns.map((column, index) => {
                    const label = displayText(row, column.key)
                    const identifier = rowHierarchyId(row, column.key)
                    const canNavigate =
                      childDimensionForColumn(column.key) !== null
                    return (
                      <TableCell
                        key={column.key}
                        className={`max-w-56 min-w-0 text-xs ${index === 0 ? "sticky left-12 z-10 bg-background" : ""}`}
                      >
                        <div className="flex flex-col items-start">
                          <button
                            type="button"
                            className="block max-w-56 min-w-0 whitespace-normal break-words text-left font-medium hover:underline"
                            title={label === "—" ? rowName(row) : label}
                            onClick={() =>
                              canNavigate
                                ? onNavigate(row, column.key)
                                : onOpen(row)
                            }
                          >
                            {label === "—" ? rowName(row) : label}
                          </button>
                          {identifier && (
                            <button
                              type="button"
                              className="text-[10px] text-muted-foreground hover:underline"
                              onClick={() =>
                                canNavigate
                                  ? onNavigate(row, column.key)
                                  : onOpen(row)
                              }
                            >
                              {identifier}
                            </button>
                          )}
                        </div>
                      </TableCell>
                    )
                  })}
                  <TableCell>
                    <Badge variant="outline" className="text-[10px]">
                      {row.display?.status ??
                        row.display?.operation_status ??
                        "未知"}
                    </Badge>
                  </TableCell>
                  {dimension === "account" && (
                    <TableCell
                      className="text-xs"
                      title={row.display?.balance_updated_at ?? undefined}
                    >
                      {row.display?.balance != null
                        ? `${row.display.balance_currency} ${formatMetric(row.display.balance)}${row.display.balance_scope === "PORTFOLIO" ? "（共享资金池）" : ""}`
                        : "—"}
                    </TableCell>
                  )}
                  {METRIC_COLUMNS.map((column) => {
                    const metric = rowMetric(row, column.key)
                    const reconciliation = `账户报表：${row.metric_buckets?.[0]?.currency ?? ""} ${formatMetric(row.display?.account_report_spend)}`
                    return (
                      <TableCell key={column.key} className="text-xs">
                        {availabilityLabel(metric.availability, metric.value, {
                          percent: column.key === "ctr",
                        })}
                        {column.key === "spend" &&
                          dimension === "account" &&
                          row.display?.reconciliation === "DIFF" && (
                            <span
                              className="ml-1 inline-flex"
                              role="img"
                              title={reconciliation}
                              aria-label={reconciliation}
                            >
                              <Info className="size-3.5" aria-hidden="true" />
                            </span>
                          )}
                      </TableCell>
                    )
                  })}
                  {(dimension === "campaign" || dimension === "adgroup") && (
                    <TableCell className="text-xs">
                      {rowBudget(row, dimension) ?? "—"}
                    </TableCell>
                  )}
                  {(dimension === "campaign" ||
                    dimension === "adgroup" ||
                    dimension === "ad") && (
                    <TableCell className="text-xs">
                      {targetRoasLabel(row)}
                    </TableCell>
                  )}
                  {(dimension === "campaign" ||
                    dimension === "adgroup" ||
                    dimension === "ad") && (
                    <TableCell className="text-xs">
                      {rowOptimizationGoal(row) ?? "—"}
                    </TableCell>
                  )}
                  <TableCell className="text-xs text-muted-foreground">
                    {row.coverage?.updated_at
                      ? new Date(
                          String(row.coverage.updated_at),
                        ).toLocaleString("zh-CN", {
                          timeZone:
                            row.metric_buckets?.[0]?.timezone === "MIXED"
                              ? "UTC"
                              : row.metric_buckets?.[0]?.timezone,
                          hour12: false,
                        })
                      : "尚未同步"}
                    {row.coverage?.status !== "COMPLETE" && (
                      <div
                        title={`缺失日期：${(row.coverage?.stale_dates as string[] | undefined)?.join("、") ?? ""}`}
                      >
                        数据不完整
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <button
                      type="button"
                      className="mr-2 underline"
                      onClick={() => onOpen(row)}
                    >
                      查看详情
                    </button>
                    {url ? (
                      <a
                        className="underline"
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        TikTok 后台
                      </a>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
        {!rows.length && (
          <p className="p-8 text-center text-sm text-muted-foreground">
            没有匹配的广告
          </p>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t px-4 py-3 text-sm text-muted-foreground">
          <span className="text-xs">共 {total} 条</span>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!hasPrevious}
              onClick={onPrevious}
            >
              上一页
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!rows.length || allMatching}
              onClick={onSelectAll}
            >
              选择全部匹配结果
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!nextCursor}
              onClick={onNext}
            >
              下一页
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
