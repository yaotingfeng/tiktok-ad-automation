import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo, useState } from "react"
import {
  type Action_Output as Action,
  AutomationRulesService,
  type ConfirmationHit,
  type ConfirmationItem,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
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
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { displayTime } from "@/features/accounts/presentation"
import { errorMessage, ServerTable } from "@/features/tenants/shared"
import { metricTitle } from "./DryRunPanel"
import {
  catalogQuery,
  confirmationQuery,
  confirmationsQuery,
  ruleKeys,
} from "./queries"
import { ACTION_LABELS, LEVEL_LABELS } from "./ruleForm"

export const CONFIRMATION_STATUS: Record<string, string> = {
  PENDING: "待确认",
  CONFIRMED: "已确认",
  DISMISSED: "已忽略",
  EXPIRED: "已失效",
}
const ALL = "__all__"

/** 待确认批次中要执行的操作，例如「提高预算 20%（上限 500）→ 广告组」。 */
export function actionText(action: Action) {
  const adjustment = action.adjustment
  let verb: string = ACTION_LABELS[action.type]
  if (adjustment) {
    const modeLabel = {
      INCREASE: "提高",
      DECREASE: "降低",
      SET: "设为",
    }[adjustment.mode]
    const unit = adjustment.value_type === "PERCENT" ? "%" : ""
    const target = action.type === "BUDGET" ? "预算" : "ROAS 出价"
    const bound = adjustment.mode === "INCREASE" ? "上限" : "下限"
    verb = `${modeLabel}${target} ${adjustment.value}${unit}`
    if (adjustment.limit) verb += `（${bound} ${adjustment.limit}）`
  }
  return `${verb} → ${LEVEL_LABELS[action.level]}`
}

function metricsText(
  metrics: Record<string, string>,
  catalog: Parameters<typeof metricTitle>[1],
) {
  const entries = Object.entries(metrics)
  if (!entries.length) return "—"
  return entries
    .map(([key, value]) => `${metricTitle(key, catalog)} ${value || "—"}`)
    .join("；")
}

function ConfirmationSheet({
  tenantId,
  confirmationId,
  write,
  onClose,
}: {
  tenantId: string
  confirmationId: string
  write: boolean
  onClose: () => void
}) {
  const client = useQueryClient()
  const detail = useQuery(confirmationQuery(tenantId, confirmationId))
  const catalog = useQuery(catalogQuery(tenantId))
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const data = detail.data
  useEffect(() => {
    // 用最新数据重新判断后已不满足条件的对象默认不勾选。
    if (data)
      setSelected(
        new Set(
          data.items
            .filter((item) => item.still_matching)
            .map((item) => item.subject_key),
        ),
      )
  }, [data])
  const done = async () => {
    await client.invalidateQueries({ queryKey: ruleKeys(tenantId) })
    onClose()
  }
  const confirm = useMutation({
    mutationFn: () =>
      AutomationRulesService.confirmBatch({
        path: { tenant_id: tenantId, confirmation_id: confirmationId },
        body: {
          subject_keys: (data?.items ?? [])
            .map((item) => item.subject_key)
            .filter((key) => selected.has(key)),
        },
      }),
    onSuccess: done,
    onError: (err) => setError(errorMessage(err)),
  })
  const dismiss = useMutation({
    mutationFn: () =>
      AutomationRulesService.dismissBatch({
        path: { tenant_id: tenantId, confirmation_id: confirmationId },
      }),
    onSuccess: done,
    onError: (err) => setError(errorMessage(err)),
  })
  const open = data?.confirmation.status === "PENDING"
  const toggle = (item: ConfirmationHit, checked: boolean) =>
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(item.subject_key)
      else next.delete(item.subject_key)
      return next
    })
  const pending = confirm.isPending || dismiss.isPending
  return (
    <Sheet open onOpenChange={(value) => !value && onClose()}>
      <SheetContent className="w-full sm:max-w-4xl">
        <SheetHeader className="border-b pr-12">
          <SheetTitle>
            {data ? data.confirmation.rule_name : "待确认批次"}
          </SheetTitle>
          <SheetDescription>
            {data
              ? `${displayTime(data.confirmation.created_at)} · ${actionText(data.action)} · 命中 ${data.confirmation.hit_count} 个，涉及 ${data.confirmation.target_count} 个对象`
              : "加载中…"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-2">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          {data && !open && (
            <Alert>
              <AlertDescription>
                该批次{CONFIRMATION_STATUS[data.confirmation.status]}
                ，不能再操作。
              </AlertDescription>
            </Alert>
          )}
          {data && data.skipped_accounts.length > 0 && (
            <p className="text-sm text-muted-foreground break-all">
              现在数据未更新的账户：
              {data.skipped_accounts
                .map((item) => item.advertiser_id)
                .join("、")}
            </p>
          )}
          <p className="text-sm text-muted-foreground">
            「现在」为按最新数据重新判断的结果；执行时仍会做频控、冷却期与每日涨幅检查。
          </p>
          {data && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10" />
                  <TableHead>对象</TableHead>
                  <TableHead>判断时</TableHead>
                  <TableHead>现在</TableHead>
                  <TableHead className="text-right">涉及对象</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((item) => (
                  <TableRow key={item.subject_key}>
                    <TableCell>
                      <Checkbox
                        aria-label={`选择 ${item.subject_label}`}
                        disabled={!open || !write}
                        checked={selected.has(item.subject_key)}
                        onCheckedChange={(checked) => toggle(item, !!checked)}
                      />
                    </TableCell>
                    <TableCell className="max-w-56 whitespace-normal">
                      <span
                        className="block truncate"
                        title={item.subject_label}
                      >
                        {item.subject_label}
                      </span>
                      {!item.still_matching && (
                        <Badge variant="outline" className="mt-1">
                          条件已不满足
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {metricsText(item.metrics_then, catalog.data)}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {metricsText(item.metrics_now, catalog.data)}
                    </TableCell>
                    <TableCell className="text-right">
                      {item.target_count}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
        {open && write && (
          <SheetFooter className="flex-row justify-end border-t">
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => dismiss.mutate()}
            >
              忽略本批
            </Button>
            <Button
              disabled={pending || selected.size === 0}
              onClick={() => confirm.mutate()}
            >
              确认执行所选
            </Button>
          </SheetFooter>
        )}
      </SheetContent>
    </Sheet>
  )
}

export function Confirmations({
  tenantId,
  write,
  openConfirmation,
  onOpenConfirmation,
}: {
  tenantId: string
  write: boolean
  openConfirmation?: string
  onOpenConfirmation: (id: string | undefined) => void
}) {
  const [status, setStatus] = useState<string>("PENDING")
  const list = useQuery(
    confirmationsQuery(tenantId, status === ALL ? undefined : status),
  )
  const columns = useMemo<ColumnDef<ConfirmationItem>[]>(
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
        header: "生成时间",
        id: "created_at",
        size: 170,
        cell: ({ row }) => displayTime(row.original.created_at),
      },
      {
        header: "命中",
        id: "hits",
        size: 80,
        cell: ({ row }) => row.original.hit_count,
      },
      {
        header: "涉及对象",
        id: "targets",
        size: 90,
        cell: ({ row }) => row.original.target_count,
      },
      {
        header: "状态",
        id: "status",
        size: 100,
        cell: ({ row }) => (
          <Badge
            variant={row.original.status === "PENDING" ? "default" : "outline"}
          >
            {CONFIRMATION_STATUS[row.original.status] ?? row.original.status}
          </Badge>
        ),
      },
      {
        header: "操作",
        id: "open",
        size: 90,
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenConfirmation(row.original.id)}
          >
            {row.original.status === "PENDING" && write ? "处理" : "查看"}
          </Button>
        ),
      },
    ],
    [onOpenConfirmation, write],
  )
  return (
    <Card>
      <CardHeader>
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger aria-label="按状态筛选" className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="PENDING">待确认</SelectItem>
            <SelectItem value={ALL}>全部状态</SelectItem>
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent>
        <ServerTable
          fixedLayout={{ fillColumn: "rule" }}
          rows={list.data?.items ?? []}
          columns={columns}
          loading={list.isPending}
          fetching={list.isFetching}
          error={list.error}
          retry={() => void list.refetch()}
          filtered={status !== "PENDING"}
          emptyTitle="没有待确认的批次"
        />
      </CardContent>
      {openConfirmation && (
        <ConfirmationSheet
          tenantId={tenantId}
          confirmationId={openConfirmation}
          write={write}
          onClose={() => onOpenConfirmation(undefined)}
        />
      )}
    </Card>
  )
}
