import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { Plus } from "lucide-react"
import { useMemo, useState } from "react"
import {
  AutomationRulesService,
  type CatalogPublic,
  type RulePublic,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { displayTime } from "@/features/accounts/presentation"
import { ServerTable } from "@/features/tenants/shared"
import { catalogQuery, ruleKeys, rulesQuery } from "./queries"
import { type EditorTarget, RuleEditorSheet } from "./RuleEditorSheet"
import {
  type Definition,
  FREQUENCY_LABELS,
  MODE_LABELS,
  summarizeAction,
  summarizeConditions,
  summarizeScope,
} from "./ruleForm"

const RUN_ERRORS: Record<string, string> = {
  rule_owner_forbidden: "规则最后修改人已无权限",
  evaluation_failed: "判断失败",
}

function LastRun({ rule }: { rule: RulePublic }) {
  const run = rule.last_run
  if (!run) return <span className="text-muted-foreground">尚未运行</span>
  return (
    <div className="flex flex-col gap-1 tabular-nums">
      <span>{displayTime(run.started_at)}</span>
      <span className="text-xs text-muted-foreground">
        {run.status === "FAILED"
          ? (RUN_ERRORS[run.error_code ?? ""] ?? run.error_code)
          : `命中 ${run.hit_count}`}
      </span>
    </div>
  )
}

export function RuleList({
  tenantId,
  write,
}: {
  tenantId: string
  write: boolean
}) {
  const client = useQueryClient()
  const rules = useQuery(rulesQuery(tenantId))
  const catalog = useQuery(catalogQuery(tenantId))
  const [search, setSearch] = useState("")
  const [editor, setEditor] = useState<EditorTarget | null>(null)
  const [removing, setRemoving] = useState<RulePublic | null>(null)
  const refresh = () =>
    client.invalidateQueries({ queryKey: ruleKeys(tenantId) })
  const toggle = useMutation({
    mutationFn: (rule: RulePublic) =>
      AutomationRulesService.setEnabled({
        path: { tenant_id: tenantId, rule_id: rule.id },
        body: { enabled: !rule.enabled },
      }),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: (rule: RulePublic) =>
      AutomationRulesService.deleteRule({
        path: { tenant_id: tenantId, rule_id: rule.id },
      }),
    onSuccess: async () => {
      setRemoving(null)
      await refresh()
    },
  })
  const rows = (rules.data?.items ?? []).filter((rule) =>
    rule.name.includes(search.trim()),
  )
  const columns = useMemo<ColumnDef<RulePublic>[]>(() => {
    const info = catalog.data as CatalogPublic | undefined
    return [
      {
        header: "开关",
        id: "enabled",
        size: 64,
        cell: ({ row: { original: rule } }) => (
          <Checkbox
            aria-label={`启用 ${rule.name}`}
            checked={rule.enabled}
            disabled={!write || toggle.isPending}
            onCheckedChange={() => toggle.mutate(rule)}
          />
        ),
      },
      {
        header: "规则名称",
        id: "name",
        minSize: 160,
        cell: ({ row }) => (
          <span
            className="block truncate font-medium"
            title={row.original.name}
          >
            {row.original.name}
          </span>
        ),
      },
      {
        header: "条件",
        id: "conditions",
        size: 280,
        cell: ({ row }) => (
          <span className="block whitespace-normal">
            {info
              ? summarizeConditions(row.original.definition as Definition, info)
              : "—"}
          </span>
        ),
      },
      {
        header: "操作",
        id: "action",
        size: 130,
        cell: ({ row }) =>
          summarizeAction(row.original.definition as Definition),
      },
      {
        header: "执行模式",
        id: "mode",
        size: 100,
        cell: ({ row }) => (
          <Badge variant="secondary">
            {MODE_LABELS[row.original.definition.mode]}
          </Badge>
        ),
      },
      {
        header: "适用范围",
        id: "scope",
        size: 100,
        cell: ({ row }) =>
          summarizeScope(row.original.definition as Definition),
      },
      {
        header: "运行频率",
        id: "frequency",
        size: 110,
        cell: ({ row }) =>
          FREQUENCY_LABELS[row.original.definition.schedule.frequency],
      },
      {
        header: "最近运行",
        id: "last_run",
        size: 170,
        cell: ({ row }) => <LastRun rule={row.original} />,
      },
      {
        header: "行操作",
        id: "row_actions",
        size: 220,
        cell: ({ row: { original: rule } }) => (
          <div className="flex gap-1 [&_[data-slot=button]]:px-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                setEditor({
                  kind: "create",
                  name: `${rule.name} 副本`,
                  definition: rule.definition as Definition,
                })
              }
              disabled={!write}
            >
              复制
            </Button>
            {write && (
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setEditor({ kind: "edit", rule })}
                >
                  编辑
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setRemoving(rule)}
                >
                  删除
                </Button>
              </>
            )}
          </div>
        ),
      },
    ]
  }, [catalog.data, write, toggle])
  return (
    <Card>
      <CardHeader className="flex flex-wrap items-end justify-between gap-3">
        <Field className="w-full sm:w-80">
          <FieldLabel htmlFor="rule-search">规则名称</FieldLabel>
          <Input
            id="rule-search"
            placeholder="搜索规则名称"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </Field>
        {write && (
          <Button
            onClick={() => setEditor({ kind: "create" })}
            disabled={!catalog.data}
          >
            <Plus data-icon="inline-start" />
            新建规则
          </Button>
        )}
      </CardHeader>
      <CardContent>
        <ServerTable
          fixedLayout={{ fillColumn: "name" }}
          rows={rows}
          columns={columns}
          loading={rules.isPending}
          fetching={rules.isFetching}
          error={rules.error}
          retry={() => void rules.refetch()}
          filtered={!!search.trim()}
          emptyTitle="还没有自动规则"
        />
      </CardContent>
      {editor && catalog.data && (
        <RuleEditorSheet
          tenantId={tenantId}
          catalog={catalog.data}
          target={editor}
          onClose={() => setEditor(null)}
        />
      )}
      <Dialog
        open={!!removing}
        onOpenChange={(open) => !open && setRemoving(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除规则</DialogTitle>
            <DialogDescription>
              删除「{removing?.name}」后，它的运行记录也会一并删除，无法恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoving(null)}>
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => removing && remove.mutate(removing)}
            >
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
