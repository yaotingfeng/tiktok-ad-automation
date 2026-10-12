import { Link } from "@tanstack/react-router"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

type HistoryItem = {
  task_id?: string
  created_at?: string
  field?: string
  mode?: string
  original_value?: string | null
  final_value?: string | null
  execution_result?: string | null
  reason?: string | null
}

const FIELDS: Record<string, string> = {
  status: "启停",
  roas: "目标 ROAS",
  budget: "预算",
}
const MODES: Record<string, string> = {
  set: "设为",
  add: "增加",
  subtract: "减少",
  increase_percent: "增加百分比",
  decrease_percent: "减少百分比",
}
// 与后端 execution_result 取值一一对应；被拒绝、有冲突醒目提示，结果待确认弱化显示。
const RESULTS: Record<
  string,
  { label: string; variant: "outline" | "destructive" | "secondary" }
> = {
  ACCEPTED: { label: "已执行", variant: "outline" },
  NO_CHANGE: { label: "无需修改", variant: "outline" },
  REJECTED: { label: "被拒绝", variant: "destructive" },
  CONFLICT: { label: "有冲突", variant: "destructive" },
  UNSUPPORTED: { label: "不支持", variant: "outline" },
  NOT_SENT: { label: "未发送", variant: "outline" },
  CANCELLED: { label: "已取消", variant: "outline" },
  PENDING: { label: "待执行", variant: "outline" },
  UNKNOWN: { label: "结果待确认", variant: "secondary" },
}

function ResultBadge({ value }: { value?: string | null }) {
  if (!value) return "—"
  const result = RESULTS[value]
  // 未登记的新状态原样显示，避免误译。
  if (!result) return value
  return <Badge variant={result.variant}>{result.label}</Badge>
}

// 启停只有"设为"一种方式，不重复显示；数值动作带上调整方式。
function actionText(item: HistoryItem) {
  const field = FIELDS[item.field ?? ""] ?? item.field ?? "—"
  if (item.field === "status" || !item.mode) return field
  return `${field} · ${MODES[item.mode] ?? item.mode}`
}

export function HistoryTab({
  tenantId,
  bcId,
  items,
}: {
  tenantId: string
  bcId: string
  items: Array<Record<string, unknown>>
}) {
  const rows = items as HistoryItem[]
  if (!rows.length)
    return <p className="text-sm text-muted-foreground">暂无本工具的操作记录</p>
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>时间</TableHead>
            <TableHead>动作</TableHead>
            <TableHead>原值 → 新值</TableHead>
            <TableHead>结果</TableHead>
            <TableHead>任务</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((item, index) => (
            <TableRow key={`${item.task_id ?? ""}:${index}`}>
              <TableCell className="whitespace-nowrap">
                {item.created_at
                  ? new Date(item.created_at).toLocaleString("zh-CN", {
                      hour12: false,
                    })
                  : "—"}
              </TableCell>
              <TableCell>{actionText(item)}</TableCell>
              <TableCell>{`${item.original_value ?? "—"} → ${item.final_value ?? "—"}`}</TableCell>
              <TableCell title={item.reason ?? undefined}>
                <ResultBadge value={item.execution_result} />
              </TableCell>
              <TableCell>
                {item.task_id && (
                  <Link
                    className="hover:underline"
                    to="/tenants/$tenantId/ad-management-tasks/$taskId"
                    params={{ tenantId, taskId: item.task_id }}
                    search={{ bc_id: bcId }}
                  >
                    {item.task_id.slice(0, 8)}…
                  </Link>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
