import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useMemo, useState } from "react"
import type { RunListItem, RunSummary } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { displayTime } from "@/features/accounts/presentation"
import { ServerTable } from "@/features/tenants/shared"
import { HitTable } from "./DryRunPanel"
import { catalogQuery, rulesQuery, runQuery, runsQuery } from "./queries"
import { MODE_LABELS } from "./ruleForm"

const NOTIFY_LABELS: Record<string, string> = {
  NOT_REQUIRED: "无需发送",
  PENDING: "待发送",
  SENT: "已发送",
  FAILED: "发送失败",
}
const RUN_ERRORS: Record<string, string> = {
  rule_owner_forbidden: "规则最后修改人已无权限",
  evaluation_failed: "判断失败",
  evaluation_timeout: "判断超时",
}
const ALL = "__all__"
const EXECUTION_LABELS: Record<string, string> = {
  AWAITING_CONFIRMATION: "待确认",
  QUEUED: "已排队",
  CONFIRMED: "已确认，执行中",
  PARTIAL: "部分完成",
  SKIPPED: "未执行（已跳过）",
  FAILED: "执行失败",
  DISMISSED: "已忽略",
  EXPIRED: "已失效",
}

const EXECUTION_ERRORS: Record<string, string> = {
  rule_owner_forbidden: "执行人已无执行权限，未执行。",
  automation_disabled: "自动规则部署开关已关闭，未执行。",
  execution_error: "执行异常，未完成。",
}

type Summary = {
  submitted?: number
  skipped_frequency?: number
  skipped_daily_limit?: number
  conflicts?: number
  no_change?: number
  failed?: number
  error?: string
  reason?: string
  target_count?: number
  max_targets?: number
  failed_bcs?: { bc_id: string; reason: string }[]
  tasks?: { bc_id: string; task_id: string }[]
  conflict_details?: { target_key: string; rule_name: string; action: string }[]
}

/** 执行列：仅通知显示模式名；已提交显示提交数，其余显示状态。 */
function executionLabel(run: RunSummary) {
  if (run.mode === "NOTIFY" || run.execution_status === "NOT_REQUIRED")
    return MODE_LABELS.NOTIFY
  if (run.execution_status === "SUBMITTED")
    return `已提交 ${(run.execution_summary as Summary).submitted ?? 0}`
  return EXECUTION_LABELS[run.execution_status] ?? run.execution_status
}

function ExecutionSummary({
  tenantId,
  run,
}: {
  tenantId: string
  run: RunSummary
}) {
  const summary = run.execution_summary as Summary
  if (run.execution_status === "AWAITING_CONFIRMATION")
    return (
      <p className="text-sm">
        {summary.reason === "max_targets_exceeded"
          ? `涉及 ${summary.target_count} 个对象，超过单次自动执行上限 ${summary.max_targets}，已转为人工确认。`
          : "命中结果等待确认，请在「待确认」中处理。"}
      </p>
    )
  if (
    !["SUBMITTED", "PARTIAL", "FAILED", "SKIPPED"].includes(
      run.execution_status,
    )
  )
    return null
  return (
    <div className="flex flex-col gap-1 rounded-md border p-3 text-sm">
      <p>
        提交 {summary.submitted ?? 0} 个对象；频控跳过{" "}
        {summary.skipped_frequency ?? 0}、冷却期冲突 {summary.conflicts ?? 0}
        、超出每日涨幅 {summary.skipped_daily_limit ?? 0}、无需变更{" "}
        {summary.no_change ?? 0}
        {summary.failed ? `、失败 ${summary.failed}` : ""}
      </p>
      {summary.error && (
        <p className="text-destructive">
          {EXECUTION_ERRORS[summary.error] ?? "执行失败"}
        </p>
      )}
      {(summary.failed_bcs ?? []).map((item) => (
        <p key={item.bc_id} className="text-destructive break-all">
          BC {item.bc_id} 执行失败：{item.reason}
        </p>
      ))}
      {(summary.conflict_details ?? []).map((item) => (
        <p key={item.target_key} className="break-all text-muted-foreground">
          冲突：{item.target_key} 与规则「{item.rule_name}」相反
        </p>
      ))}
      {(summary.tasks ?? []).map((task) => (
        <Link
          key={task.task_id}
          to="/tenants/$tenantId/ad-management-tasks/$taskId"
          params={{ tenantId, taskId: task.task_id }}
          search={{ bc_id: task.bc_id }}
          className="text-primary underline-offset-4 hover:underline"
        >
          操作记录
        </Link>
      ))}
    </div>
  )
}

function RunDetailSheet({
  tenantId,
  runId,
  onClose,
}: {
  tenantId: string
  runId: string
  onClose: () => void
}) {
  const detail = useQuery(runQuery(tenantId, runId))
  const catalog = useQuery(catalogQuery(tenantId))
  const run = detail.data?.run
  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-3xl">
        <SheetHeader className="border-b pr-12">
          <SheetTitle>{run ? run.rule_name : "运行明细"}</SheetTitle>
          <SheetDescription>
            {run
              ? `${displayTime(run.started_at)} · 判断 ${run.checked_count} 个对象，命中 ${run.hit_count} 个`
              : "加载中…"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-2">
          {run && run.skipped_accounts.length > 0 && (
            <p className="text-sm text-muted-foreground break-all">
              数据未更新已跳过的账户：
              {run.skipped_accounts
                .map((item) => item.advertiser_id)
                .join("、")}
            </p>
          )}
          {run?.notify_status === "FAILED" && (
            <p className="text-sm text-destructive">
              飞书发送失败：{run.notify_error}
            </p>
          )}
          {run && <ExecutionSummary tenantId={tenantId} run={run} />}
          {detail.data && (
            <HitTable
              hits={detail.data.hits}
              catalog={catalog.data}
              showOutcome={run?.mode !== "NOTIFY"}
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

export function RunHistory({
  tenantId,
  openRun,
  onOpenRun,
}: {
  tenantId: string
  openRun?: string
  onOpenRun: (runId: string | undefined) => void
}) {
  const [ruleId, setRuleId] = useState<string>(ALL)
  const rules = useQuery(rulesQuery(tenantId))
  const runs = useQuery(
    runsQuery(tenantId, ruleId === ALL ? undefined : ruleId),
  )
  const columns = useMemo<ColumnDef<RunListItem>[]>(
    () => [
      {
        header: "规则",
        id: "rule",
        minSize: 160,
        cell: ({ row }) => (
          <span className="block truncate font-medium">
            {row.original.rule_name}
          </span>
        ),
      },
      {
        header: "开始时间",
        id: "started_at",
        size: 170,
        cell: ({ row }) => displayTime(row.original.started_at),
      },
      {
        header: "判断对象",
        id: "checked",
        size: 90,
        cell: ({ row }) => row.original.checked_count,
      },
      {
        header: "结果",
        id: "result",
        size: 150,
        cell: ({ row: { original: run } }) =>
          run.status === "FAILED" ? (
            <Badge variant="destructive">
              {RUN_ERRORS[run.error_code ?? ""] ?? run.error_code ?? "失败"}
            </Badge>
          ) : (
            <span>
              命中 {run.hit_count}
              {run.skipped_accounts.length > 0 &&
                ` · 跳过 ${run.skipped_accounts.length} 个账户`}
            </span>
          ),
      },
      {
        header: "执行",
        id: "execution",
        size: 110,
        cell: ({ row }) => executionLabel(row.original),
      },
      {
        header: "飞书",
        id: "notify",
        size: 100,
        cell: ({ row: { original: run } }) => (
          <span title={run.notify_error ?? undefined}>
            {NOTIFY_LABELS[run.notify_status] ?? run.notify_status}
          </span>
        ),
      },
      {
        header: "明细",
        id: "detail",
        size: 100,
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenRun(row.original.id)}
          >
            查看明细
          </Button>
        ),
      },
    ],
    [onOpenRun],
  )
  return (
    <Card>
      <CardHeader>
        <Select value={ruleId} onValueChange={setRuleId}>
          <SelectTrigger aria-label="按规则筛选" className="w-64">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部规则</SelectItem>
            {(rules.data?.items ?? []).map((rule) => (
              <SelectItem key={rule.id} value={rule.id}>
                {rule.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent>
        <ServerTable
          fixedLayout={{ fillColumn: "rule" }}
          rows={runs.data?.items ?? []}
          columns={columns}
          loading={runs.isPending}
          fetching={runs.isFetching}
          error={runs.error}
          retry={() => void runs.refetch()}
          filtered={ruleId !== ALL}
          emptyTitle="还没有运行记录"
        />
      </CardContent>
      {openRun && (
        <RunDetailSheet
          tenantId={tenantId}
          runId={openRun}
          onClose={() => onOpenRun(undefined)}
        />
      )}
    </Card>
  )
}
