import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { Plus } from "lucide-react"
import { useMemo, useState } from "react"
import { StrategiesService, type StrategyPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
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
import { displayTime, FilterSelect } from "@/features/accounts/presentation"
import { normalizeDecimal } from "@/features/strategies/validation"
import {
  isForbidden,
  Pager,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { handleApiError } from "@/lib/api-feedback"
import { StrategyError as RequestError } from "./feedback"
import { strategyKey } from "./queries"
import { StrategyVersionList } from "./StrategyVersionList"
import "./strategy-list.css"
export function StrategyList() {
  const { tenantId, tenant, scope } = useTenantScope(),
    client = useQueryClient(),
    navigate = useNavigate(),
    paging = useCursorPage()
  const [input, setInput] = useState(""),
    [search, setSearch] = useState(""),
    [active, setActive] = useState("active"),
    [history, setHistory] = useState<StrategyPublic | null>(null),
    [changeState, setChangeState] = useState<StrategyPublic | null>(null),
    [pending, setPending] = useState(false),
    [error, setError] = useState<unknown>()
  const query = useQuery({
    queryKey: [
      ...strategyKey(tenantId!),
      "list",
      search,
      active,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await StrategiesService.getStrategies({
          path: { tenant_id: tenantId! },
          query: {
            query: search,
            active: active === "all" ? undefined : active === "active",
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  const data = useRetainedData(query.data, query.error),
    write =
      !!scope &&
      scope.role !== "viewer" &&
      !isForbidden(query.error) &&
      !isForbidden(error)
  const columns = useMemo<ColumnDef<StrategyPublic>[]>(
    () => [
      {
        header: "策略名称",
        id: "name",
        minSize: 220,
        cell: ({ row }) => (
          <Link
            className="block truncate font-medium hover:underline"
            title={row.original.name}
            to="/tenants/$tenantId/strategies/$strategyId"
            params={{ tenantId: tenantId!, strategyId: row.original.id }}
            search={{ bc_id: scope?.bcId || undefined }}
          >
            {row.original.name}
          </Link>
        ),
      },
      {
        header: "当前版本",
        size: 96,
        cell: ({ row }) => (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setHistory(row.original)}
          >
            v{row.original.latest_version}
          </Button>
        ),
      },
      {
        header: "Campaign 日预算",
        size: 168,
        cell: ({ row }) => (
          <div className="flex flex-col gap-1 tabular-nums">
            <span className="wrap-anywhere whitespace-normal">
              {row.original.config.currency}{" "}
              {normalizeDecimal(row.original.config.budget)}
            </span>
            <p className="text-xs text-muted-foreground">每个 Campaign / 天</p>
          </div>
        ),
      },
      {
        header: "目标 ROAS",
        size: 112,
        cell: ({ row }) => (
          <span className="block wrap-anywhere whitespace-normal tabular-nums">
            {row.original.config.target_roas} 倍
          </span>
        ),
      },
      {
        header: "每组素材",
        size: 104,
        cell: ({ row }) => (
          <span title="按文件名顺序分组，保留不足整组的尾组">
            {row.original.config.group_size} 条/组
          </span>
        ),
      },
      {
        header: "创意数量",
        size: 108,
        cell: ({ row }) => (
          <div className="flex flex-col gap-1">
            {row.original.config.creative_count} 条/组
            <p className="text-xs text-muted-foreground">
              SP1～SP{row.original.config.creative_count}
            </p>
          </div>
        ),
      },
      {
        header: "状态",
        size: 88,
        cell: ({ row }) => (
          <Badge variant={row.original.active ? "secondary" : "outline"}>
            {row.original.active ? "可用" : "已停用"}
          </Badge>
        ),
      },
      {
        header: "更新信息",
        size: 208,
        cell: ({ row }) => (
          <div className="flex flex-col gap-1 tabular-nums">
            {displayTime(row.original.created_at)}
            <p
              className="max-w-40 truncate text-xs text-muted-foreground"
              title={row.original.created_by}
            >
              操作者 {row.original.created_by}
            </p>
          </div>
        ),
      },
      {
        header: "操作",
        size: 324,
        cell: ({ row: { original: r } }) => (
          <div className="flex gap-1 [&_[data-slot=button]]:px-2">
            <Button variant="ghost" size="sm" onClick={() => setHistory(r)}>
              查看版本
            </Button>
            {write && (
              <>
                {r.active && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      void navigate({
                        to: "/tenants/$tenantId/strategies/$strategyId",
                        params: { tenantId: tenantId!, strategyId: r.id },
                        search: { bc_id: scope?.bcId || undefined },
                      })
                    }
                  >
                    编辑
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    void navigate({
                      to: "/tenants/$tenantId/strategies/new",
                      params: { tenantId: tenantId! },
                      search: {
                        bc_id: scope?.bcId || undefined,
                        copy_version_id: r.version_id,
                      },
                    })
                  }
                >
                  复制为新策略
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setError(undefined)
                    setChangeState(r)
                  }}
                >
                  {r.active ? "停用" : "恢复可用"}
                </Button>
              </>
            )}
          </div>
        ),
      },
    ],
    [navigate, tenantId, scope?.bcId, write],
  )
  return (
    <section
      data-presentation="strategy-list"
      aria-label="投放策略工作区"
      className="flex min-w-0 flex-col gap-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle placement="content" sectionLabel="投放工作">
            投放策略
          </WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            当前租户策略 · {tenant?.name} · 不按 BC 筛选
          </p>
        </div>
        {write && (
          <Button asChild>
            <Link
              to="/tenants/$tenantId/strategies/new"
              params={{ tenantId: tenantId! }}
              search={{ bc_id: scope?.bcId || undefined }}
            >
              <Plus />
              新建策略
            </Link>
          </Button>
        )}
      </div>
      <Card className="min-w-0 gap-5 py-4 lg:gap-6 lg:py-6">
        <CardHeader className="px-4 lg:px-6">
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault()
              setSearch(input.trim())
              paging.reset()
            }}
          >
            <Field className="w-full sm:w-80">
              <FieldLabel htmlFor="strategy-search">策略名称</FieldLabel>
              <Input
                id="strategy-search"
                maxLength={255}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="搜索策略名称"
              />
            </Field>
            <FilterSelect
              label="策略状态"
              choices={{ active: "可用", inactive: "已停用" }}
              value={active}
              onChange={(v) => {
                setActive(v)
                paging.reset()
              }}
            />
            <Button variant="outline" type="submit">
              搜索
            </Button>
            <Button
              variant="ghost"
              type="button"
              onClick={() => {
                setInput("")
                setSearch("")
                setActive("all")
                paging.reset()
              }}
            >
              清除筛选
            </Button>
          </form>
        </CardHeader>
        <CardContent className="strategy-list-table min-w-0 px-4 lg:px-6">
          <ServerTable
            fixedLayout={{ fillColumn: "name" }}
            rows={data?.items || []}
            columns={columns}
            loading={query.isPending && !data}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={!!search || active === "inactive"}
            emptyTitle="还没有投放策略"
          />
        </CardContent>
        <CardFooter className="block px-4 lg:px-6">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching}
          />
        </CardFooter>
      </Card>
      {history && (
        <StrategyVersionList
          strategyId={history.id}
          name={history.name}
          onClose={() => setHistory(null)}
        />
      )}
      <Dialog
        open={!!changeState}
        onOpenChange={(open) => {
          if (!open && !pending) setChangeState(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {changeState?.active ? "停用策略？" : "恢复策略可用？"}
            </DialogTitle>
            <DialogDescription>
              只修改策略可用性。已提交任务不受影响。
            </DialogDescription>
          </DialogHeader>
          {!!error && <RequestError error={error} />}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => setChangeState(null)}
            >
              取消
            </Button>
            <Button
              disabled={pending}
              onClick={async () => {
                if (!changeState) return
                setPending(true)
                try {
                  await StrategiesService.setState({
                    path: { tenant_id: tenantId!, strategy_id: changeState.id },
                    body: { active: !changeState.active },
                  })
                  await client.invalidateQueries({
                    queryKey: strategyKey(tenantId!),
                  })
                  setChangeState(null)
                } catch (e) {
                  setError(e)
                  if (e instanceof Error) handleApiError(e)
                } finally {
                  setPending(false)
                }
              }}
            >
              {pending ? "正在保存…" : "确认"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
