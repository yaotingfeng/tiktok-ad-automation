import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo, useState } from "react"
import type { RunListItem, RunSummary } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
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
import {
  catalogQuery,
  type RunFilterValues,
  rulesQuery,
  runQuery,
  runsQuery,
} from "./queries"
import { MODE_LABELS } from "./ruleForm"
import { SkippedInputs } from "./SkippedInputs"

const NOTIFY_LABELS: Record<string, string> = {
  NOT_REQUIRED: "无需发送",
  PENDING: "待发送",
  SUMMARY: "等待汇总",
  SENT: "已发送",
  FAILED: "发送失败",
}
const RUN_ERRORS: Record<string, string> = {
  rule_owner_forbidden: "规则最后修改人已无权限",
  evaluation_failed: "判断失败",
  evaluation_timeout: "判断超时",
}
const ALL = "__all__"
const RESULT_LABELS: Record<NonNullable<RunFilterValues["result"]>, string> = {
  HIT: "有命中",
  NO_HIT: "无命中",
  FAILED: "运行失败",
  EXECUTED: "已执行",
  CONFLICT: "有冲突",
}

function useDebounced<T>(value: T, delay: number): T {
  const [current, setCurrent] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setCurrent(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return current
}
const EXECUTION_LABELS: Record<string, string> = {
  AWAITING_CONFIRMATION: "待确认",
  QUEUED: "已排队",
  CONFIRMED: "已确认，执行中",
  PARTIAL: "部分完成",
  ACCEPTED: "平台已受理",
  NEEDS_REVIEW: "结果未知，待核对",
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
  results?: Record<string, number>
  skipped_pending?: number
  submitted?: number
  skipped_daily_limit?: number
  skipped_frequency?: number
  conflicts?: number
  no_change?: number
  unsupported?: number
  unsupported_details?: { target_key: string; reason: string }[]
  failed?: number
  error?: string
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
    return <p className="text-sm">命中结果等待确认，请在「待确认」中处理。</p>
  if (
    ![
      "SUBMITTED",
      "PARTIAL",
      "FAILED",
      "SKIPPED",
      "ACCEPTED",
      "NEEDS_REVIEW",
    ].includes(run.execution_status)
  )
    return null
  return (
    <div className="flex flex-col gap-1 rounded-md border p-3 text-sm">
      <p>
        提交 {summary.submitted ?? 0} 个对象；频控跳过{" "}
        {summary.skipped_frequency ?? 0}、规则冲突 {summary.conflicts ?? 0}
        、无需变更 {summary.no_change ?? 0}
        {summary.skipped_daily_limit
          ? `、历史每日涨幅跳过 ${summary.skipped_daily_limit}`
          : ""}
        {summary.skipped_pending
          ? `、等待前次操作 ${summary.skipped_pending}`
          : ""}
        {summary.unsupported ? `、不支持 ${summary.unsupported}` : ""}
        {summary.failed ? `、失败 ${summary.failed}` : ""}
      </p>
      {summary.results && (
        <p>
          平台已受理 {summary.results.accepted ?? 0}；等待执行/重试{" "}
          {summary.results.pending ?? 0}； 结果未知{" "}
          {summary.results.unknown ?? 0}；平台拒绝{" "}
          {summary.results.rejected ?? 0}； 未发送{" "}
          {summary.results.not_sent ?? 0}；发送前冲突{" "}
          {summary.results.conflict ?? 0}。
          受理结果以管理任务回执为准，平台实际状态需等待目录刷新。
        </p>
      )}
      {(summary.unsupported_details ?? []).length > 0 && (
        <p className="break-all text-muted-foreground">
          不支持操作（如普通广告的素材不能单独启停）：
          {(summary.unsupported_details ?? [])
            .map((item) => item.target_key)
            .join("、")}
        </p>
      )}
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
          冲突：{item.target_key} 规则「{item.rule_name}
          」仍命中，两条规则条件重叠
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
          {run && <SkippedInputs items={run.skipped_accounts} />}
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
  const [filters, setFilters] = useState<RunFilterValues>({})
  // 账户与剧名输入稍后再查询，避免每次按键都请求。
  const debounced = useDebounced(filters, 300)
  const rules = useQuery(rulesQuery(tenantId))
  const runs = useQuery({
    ...runsQuery(tenantId, ruleId === ALL ? undefined : ruleId, debounced),
    placeholderData: (previous) => previous,
  })
  const filtered =
    ruleId !== ALL || Object.values(filters).some((value) => value)
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
              {run.skipped_accounts.length > 0 && " · 部分数据未参与判断"}
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
      <CardHeader className="flex flex-wrap items-center gap-2">
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
        <Select
          value={filters.result ?? ALL}
          onValueChange={(value) =>
            setFilters({
              ...filters,
              result:
                value === ALL
                  ? undefined
                  : (value as RunFilterValues["result"]),
            })
          }
        >
          <SelectTrigger aria-label="按结果筛选" className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部结果</SelectItem>
            {Object.entries(RESULT_LABELS).map(([key, label]) => (
              <SelectItem key={key} value={key}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          type="date"
          aria-label="开始日期"
          className="w-40"
          value={filters.since ?? ""}
          onChange={(e) => setFilters({ ...filters, since: e.target.value })}
        />
        <Input
          type="date"
          aria-label="结束日期"
          className="w-40"
          value={filters.until ?? ""}
          onChange={(e) => setFilters({ ...filters, until: e.target.value })}
        />
        <Input
          aria-label="账户 ID"
          placeholder="账户 ID"
          className="w-40"
          value={filters.advertiser_id ?? ""}
          onChange={(e) =>
            setFilters({ ...filters, advertiser_id: e.target.value.trim() })
          }
        />
        <Input
          aria-label="剧名"
          placeholder="剧名包含"
          className="w-40"
          value={filters.drama ?? ""}
          onChange={(e) => setFilters({ ...filters, drama: e.target.value })}
        />
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
          filtered={filtered}
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
