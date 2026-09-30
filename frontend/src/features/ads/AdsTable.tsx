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
  formatMetric,
  officialPlatformUrl,
  rowDrama,
  rowMetric,
  rowName,
} from "./search"
export function AdsTable({
  rows,
  selected,
  allMatching,
  total,
  onToggle,
  onSelectAll,
  onOpen,
  nextCursor,
  hasPrevious,
  onNext,
  onPrevious,
}: {
  rows: ReportRow[]
  selected: Set<string>
  allMatching: boolean
  total: number
  onToggle: (row: ReportRow, checked: boolean) => void
  onSelectAll: () => void
  onOpen: (row: ReportRow) => void
  nextCursor?: string | null
  hasPrevious: boolean
  onNext: () => void
  onPrevious: () => void
}) {
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
              <TableHead>名称</TableHead>
              <TableHead>剧</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>消耗</TableHead>
              <TableHead>D0 ROAS</TableHead>
              <TableHead>目标 ROAS</TableHead>
              <TableHead>来源</TableHead>
              <TableHead>详情</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => {
              const d0 = rowMetric(row, "d0_roas")
              const spend = rowMetric(row, "spend")
              const target = row.display?.target_roas
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
                  <TableCell>
                    <button
                      type="button"
                      className="max-w-72 text-left font-medium hover:underline"
                      onClick={() => onOpen(row)}
                    >
                      {rowName(row)}
                    </button>
                    <div className="font-mono text-xs text-muted-foreground">
                      {row.row_key}
                    </div>
                  </TableCell>
                  <TableCell>{rowDrama(row) ?? "命名不规范"}</TableCell>
                  <TableCell>
                    <Badge variant="outline">
                      {row.display?.status ??
                        row.display?.operation_status ??
                        "未知"}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {spend.availability === "UNSUPPORTED"
                      ? "平台未提供"
                      : formatMetric(spend.value)}
                  </TableCell>
                  <TableCell>
                    {d0.availability === "UNSUPPORTED"
                      ? "平台未提供"
                      : formatMetric(d0.value)}
                  </TableCell>
                  <TableCell>{target ? formatMetric(target) : "—"}</TableCell>
                  <TableCell>
                    {row.display?.local_material_id ? "TK-ADA" : "外部广告"}
                  </TableCell>
                  <TableCell>
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
          <span>共 {total} 条</span>
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
