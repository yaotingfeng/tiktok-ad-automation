import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useMemo, useState } from "react"
import type { RunListItem } from "@/client"
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
          {detail.data && (
            <HitTable hits={detail.data.hits} catalog={catalog.data} />
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
