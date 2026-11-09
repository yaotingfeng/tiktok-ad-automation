import type { RatePeriod } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export function ratePercentage(rate: RatePeriod["rate"]) {
  // 接口最多8位比例小数，对应百分数6位；消除7%等值的二进制浮点尾差。
  return rate == null ? "0" : String(Number((Number(rate) * 100).toFixed(6)))
}

export function rateText(rate: RatePeriod["rate"]) {
  return rate == null
    ? "待配置"
    : `${(Number(rate) * 100).toLocaleString("zh-CN", { maximumFractionDigits: 6 })}%`
}

export function RatePeriodTable({
  periods,
  referenceDay,
  onEdit,
  onEnd,
  onFill,
}: {
  periods: RatePeriod[]
  referenceDay?: string
  onEdit?: (period: RatePeriod) => void
  onEnd?: (period: RatePeriod) => void
  onFill?: (period: RatePeriod) => void
}) {
  const editable = Boolean(onEdit || onFill)
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>开始日期</TableHead>
          <TableHead>结束日期</TableHead>
          <TableHead>比例</TableHead>
          {referenceDay && <TableHead>状态</TableHead>}
          {editable && <TableHead>操作</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {periods.map((p) => (
          <TableRow key={p.start_date}>
            <TableCell>{p.start_date}</TableCell>
            <TableCell>{p.end_date || "持续有效"}</TableCell>
            <TableCell>
              {p.rate == null ? (
                <Badge variant="outline">待配置</Badge>
              ) : (
                rateText(p.rate)
              )}
            </TableCell>
            {referenceDay && (
              <TableCell>
                {p.start_date > referenceDay
                  ? "未开始"
                  : p.end_date && p.end_date < referenceDay
                    ? "已结束"
                    : "适用中"}
              </TableCell>
            )}
            {editable && (
              <TableCell>
                <div className="flex gap-1">
                  {p.rate == null ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onFill?.(p)}
                    >
                      设置比例
                    </Button>
                  ) : (
                    <>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onEdit?.(p)}
                      >
                        修改
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onEnd?.(p)}
                      >
                        结束
                      </Button>
                    </>
                  )}
                </div>
              </TableCell>
            )}
          </TableRow>
        ))}
        {!periods.length && (
          <TableRow>
            <TableCell
              colSpan={3 + Number(Boolean(referenceDay)) + Number(editable)}
            >
              默认 0%，尚未设置比例。
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  )
}
