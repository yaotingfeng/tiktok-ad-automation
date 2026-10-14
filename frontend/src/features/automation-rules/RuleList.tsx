import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { Plus } from "lucide-react"
import { useCallback, useMemo, useState } from "react"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { displayTime } from "@/features/accounts/presentation"
import { ServerTable } from "@/features/tenants/shared"
import { catalogQuery, overviewQuery, ruleKeys, rulesQuery } from "./queries"
import { type EditorTarget, RuleEditorSheet } from "./RuleEditorSheet"
import {
  ACTION_LABELS,
  type Definition,
  FREQUENCY_LABELS,
  LEVEL_LABELS,
  MODE_LABELS,
  summarizeAction,
  summarizeConditions,
  summarizeScope,
} from "./ruleForm"
import { TemplatePicker } from "./TemplatePicker"

type Filters = { enabled: string; mode: string; action: string; level: string }
const NO_FILTERS: Filters = {
  enabled: "ALL",
  mode: "ALL",
  action: "ALL",
  level: "ALL",
}

function hasFilters(filters: Filters) {
  return Object.values(filters).some((value) => value !== "ALL")
}

function matchesFilters(rule: RulePublic, filters: Filters) {
  const def = rule.definition
  return (
    (filters.enabled === "ALL" ||
      (filters.enabled === "ON") === rule.enabled) &&
    (filters.mode === "ALL" || def.mode === filters.mode) &&
    (filters.action === "ALL" || def.action.type === filters.action) &&
    (filters.level === "ALL" || def.action.level === filters.level)
  )
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: [string, string][]
  onChange: (value: string) => void
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label={label} className="w-36">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map(([key, name]) => (
          <SelectItem key={key} value={key}>
            {name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function OverviewCards({ tenantId }: { tenantId: string }) {
  const overview = useQuery(overviewQuery(tenantId))
  const data = overview.data
  const actions = data
    ? Object.values(data.actions_7d).reduce((sum, value) => sum + value, 0)
    : null
  const items: [string, string | number | null, string?][] = [
    [
      "执行动作",
      actions,
      data
        ? `暂停 ${data.actions_7d.PAUSE ?? 0} · 启用 ${data.actions_7d.ENABLE ?? 0} · 调预算 ${data.actions_7d.BUDGET ?? 0} · 调 ROAS ${data.actions_7d.ROAS ?? 0}`
        : undefined,
    ],
    ["待确认", data?.pending_confirmations ?? null],
    ["失败", data?.failed_7d ?? null],
    ["冲突", data?.conflicts_7d ?? null],
  ]
  return (
    <section
      aria-label="近 7 天概览"
      className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
    >
      {items.map(([title, value, detail]) => (
        <Card key={title} className="gap-1 py-4">
          <CardContent className="flex flex-col gap-1">
            <span className="text-sm text-muted-foreground">{title}</span>
            <span className="text-2xl font-semibold tabular-nums">
              {value ?? "—"}
            </span>
            {detail && (
              <span className="text-xs text-muted-foreground">{detail}</span>
            )}
          </CardContent>
        </Card>
      ))}
    </section>
  )
}

const RUN_ERRORS: Record<string, string> = {
  rule_owner_forbidden: "规则最后修改人已无权限",
  evaluation_failed: "判断失败",
  evaluation_timeout: "判断超时",
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
  admin,
}: {
  tenantId: string
  write: boolean
  admin: boolean
}) {
  const client = useQueryClient()
  const rules = useQuery(rulesQuery(tenantId))
  const catalog = useQuery(catalogQuery(tenantId))
  const [search, setSearch] = useState("")
  const [editor, setEditor] = useState<EditorTarget | null>(null)
  const [removing, setRemoving] = useState<RulePublic | null>(null)
  const [picking, setPicking] = useState(false)
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [batchDelete, setBatchDelete] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const batch = useMutation({
    mutationFn: async (operation: "ENABLE" | "DISABLE" | "DELETE") =>
      (
        await AutomationRulesService.batchRules({
          path: { tenant_id: tenantId },
          body: { rule_ids: [...selected], operation },
        })
      ).data!,
    onSuccess: async (result) => {
      setBatchDelete(false)
      setSelected(new Set())
      setNotice(
        result.skipped.length
          ? `已处理 ${result.updated} 条，${result.skipped.length} 条未处理（权限不足或规则不存在）。`
          : `已处理 ${result.updated} 条。`,
      )
      await refresh()
    },
  })
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
  const rows = (rules.data?.items ?? []).filter(
    (rule) =>
      rule.name.includes(search.trim()) && matchesFilters(rule, filters),
  )
  const toggleSelected = useCallback(
    (id: string, checked: boolean) =>
      setSelected((current) => {
        const next = new Set(current)
        if (checked) next.add(id)
        else next.delete(id)
        return next
      }),
    [],
  )
  const columns = useMemo<ColumnDef<RulePublic>[]>(() => {
    const info = catalog.data as CatalogPublic | undefined
    return [
      ...(write
        ? [
            {
              header: "",
              id: "select",
              size: 40,
              cell: ({ row: { original: rule } }) => (
                <Checkbox
                  aria-label={`选择 ${rule.name}`}
                  checked={selected.has(rule.id)}
                  onCheckedChange={(checked) =>
                    toggleSelected(rule.id, !!checked)
                  }
                />
              ),
            } satisfies ColumnDef<RulePublic>,
          ]
        : []),
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
  }, [catalog.data, write, toggle, selected, toggleSelected])
  return (
    <div className="flex flex-col gap-4">
      <OverviewCards tenantId={tenantId} />
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
            <div className="flex gap-2">
              <Button
                variant="outline"
                onClick={() => setPicking(true)}
                disabled={!catalog.data}
              >
                从模板新建
              </Button>
              <Button
                onClick={() => setEditor({ kind: "create" })}
                disabled={!catalog.data}
              >
                <Plus data-icon="inline-start" />
                新建规则
              </Button>
            </div>
          )}
          <div className="flex w-full flex-wrap items-center gap-2">
            <FilterSelect
              label="按状态筛选"
              value={filters.enabled}
              options={[
                ["ALL", "全部状态"],
                ["ON", "已开启"],
                ["OFF", "已关闭"],
              ]}
              onChange={(enabled) => setFilters({ ...filters, enabled })}
            />
            <FilterSelect
              label="按执行模式筛选"
              value={filters.mode}
              options={[["ALL", "全部模式"], ...Object.entries(MODE_LABELS)]}
              onChange={(mode) => setFilters({ ...filters, mode })}
            />
            <FilterSelect
              label="按操作筛选"
              value={filters.action}
              options={[["ALL", "全部操作"], ...Object.entries(ACTION_LABELS)]}
              onChange={(action) => setFilters({ ...filters, action })}
            />
            <FilterSelect
              label="按执行层级筛选"
              value={filters.level}
              options={[["ALL", "全部层级"], ...Object.entries(LEVEL_LABELS)]}
              onChange={(level) => setFilters({ ...filters, level })}
            />
            {write && selected.size > 0 && (
              <div className="ml-auto flex items-center gap-2">
                <span className="text-sm text-muted-foreground">
                  已选 {selected.size} 条
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={batch.isPending}
                  onClick={() => batch.mutate("ENABLE")}
                >
                  批量开启
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={batch.isPending}
                  onClick={() => batch.mutate("DISABLE")}
                >
                  批量停用
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={batch.isPending}
                  onClick={() => setBatchDelete(true)}
                >
                  批量删除
                </Button>
              </div>
            )}
          </div>
          {notice && (
            <p className="w-full text-sm text-muted-foreground">{notice}</p>
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
            filtered={!!search.trim() || hasFilters(filters)}
            emptyTitle="还没有自动规则"
          />
        </CardContent>
        {editor && catalog.data && (
          <RuleEditorSheet
            tenantId={tenantId}
            catalog={catalog.data}
            target={editor}
            admin={admin}
            onClose={() => setEditor(null)}
          />
        )}
        {picking && (
          <TemplatePicker
            tenantId={tenantId}
            onClose={() => setPicking(false)}
            onPick={(template) => {
              setPicking(false)
              setEditor({
                kind: "create",
                name: template.name,
                definition: template.definition as Definition,
              })
            }}
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
        <Dialog open={batchDelete} onOpenChange={setBatchDelete}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>批量删除规则</DialogTitle>
              <DialogDescription>
                将删除所选的 {selected.size} 条规则及其运行记录，无法恢复。
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setBatchDelete(false)}>
                取消
              </Button>
              <Button
                variant="destructive"
                disabled={batch.isPending}
                onClick={() => batch.mutate("DELETE")}
              >
                删除
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </Card>
    </div>
  )
}
