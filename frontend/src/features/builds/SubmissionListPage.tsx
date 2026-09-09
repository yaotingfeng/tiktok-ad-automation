import { useQuery } from "@tanstack/react-query"
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo, useState } from "react"
import {
  BuildsService,
  type ProviderConnectionPublic,
  ProvidersService,
  type SubmissionListItem,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  displayTime,
  FilterSelect,
  Identifier,
} from "@/features/accounts/presentation"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"
import { isForbidden, Pager, RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import {
  countObjects,
  SubmissionTable as ServerTable,
  SubmissionBadge,
} from "./SubmissionPresentation"
import {
  dateRange,
  isoDate,
  listLocationKey,
  submissionKey,
  useSubmissionPaging,
} from "./submission-page"
export function SubmissionListPage() {
  const { tenantId, scope, bc } = useTenantScope()
  if (!tenantId || !scope?.bcId || !bc)
    return (
      <WorkspaceEmpty
        title="请先选择有效的 BC"
        description="搭建任务按当前租户与 BC 展示。"
      />
    )
  return (
    <List
      key={`${tenantId}:${scope.bcId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      write={scope.role !== "viewer"}
    />
  )
}
function List({
  tenantId,
  bcId,
  write,
}: {
  tenantId: string
  bcId: string
  write: boolean
}) {
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const navigate = useNavigate(),
    searchStr = useRouterState({ select: (s) => s.location.searchStr }),
    search = useMemo(() => new URLSearchParams(searchStr), [searchStr])
  const defaults = useMemo(() => dateRange(7), []),
    range = search.get("range") || "7",
    q = search.get("q") || "",
    status = (
      ["active", "attention", "completed"].includes(
        search.get("status_group") || "",
      )
        ? search.get("status_group")!
        : "all"
    ) as "all" | "active" | "attention" | "completed",
    from = search.get("from") ?? (range === "all" ? "" : defaults.from),
    to = search.get("to") ?? (range === "all" ? "" : defaults.to),
    provider = search.get("provider_connection_id") || undefined
  const [input, setInput] = useState(q),
    [providerLabel, setProviderLabel] = useState(provider),
    [headSeen, setHeadSeen] = useState<string>()
  useEffect(() => setInput(q), [q])
  const filters = {
      q: q || undefined,
      status_group: status,
      created_from: isoDate(from),
      created_to: isoDate(to, true),
      provider_connection_id: provider,
    },
    filterKey = JSON.stringify(filters),
    paging = useSubmissionPaging(
      `task-list-page:${tenantId}:${bcId}:${filterKey}`,
    )
  const location = useMemo(() => Object.fromEntries(search), [search])
  useEffect(() => {
    if (
      pathname !== `/tenants/${tenantId}/build-tasks` &&
      pathname !== `/tenants/${tenantId}/build-tasks/`
    )
      return
    try {
      sessionStorage.setItem(
        listLocationKey(tenantId, bcId),
        JSON.stringify({ ...location, from, to, range }),
      )
    } catch {}
  }, [tenantId, bcId, location, from, to, range, pathname])
  function apply(patch: Record<string, string | undefined>) {
    try {
      for (const key of Object.keys(sessionStorage)) {
        if (key.startsWith(`task-list-page:${tenantId}:${bcId}:`))
          sessionStorage.removeItem(key)
      }
    } catch {}
    paging.reset()
    void navigate({
      to: "/tenants/$tenantId/build-tasks",
      params: { tenantId },
      search: { ...location, bc_id: bcId, range, from, to, ...patch },
      replace: true,
    })
  }
  const query = useQuery({
    queryKey: [
      ...submissionKey(tenantId, bcId),
      "list",
      filters,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.listSubmissions({
          path: { tenant_id: tenantId },
          query: {
            bc_id: bcId,
            ...filters,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    refetchOnWindowFocus: false,
  })
  const head = useQuery({
    queryKey: [...submissionKey(tenantId, bcId), "newest"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.listSubmissions({
          path: { tenant_id: tenantId },
          query: { bc_id: bcId, limit: 1 },
          signal,
        })
      ).data,
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
  })
  useEffect(() => {
    if (headSeen === undefined && head.data)
      setHeadSeen(head.data.items[0]?.submission_id || "")
  }, [headSeen, head.data])
  const newer =
    headSeen !== undefined &&
    head.data?.items[0]?.submission_id &&
    head.data.items[0].submission_id !== headSeen
  const allowed = write && !isForbidden(query.error)
  const columns = useMemo<ColumnDef<SubmissionListItem>[]>(
    () => [
      {
        header: "任务",
        cell: ({ row: { original: r } }) => (
          <div className="flex flex-col gap-1">
            <Link
              className="font-semibold hover:underline"
              to="/tenants/$tenantId/build-tasks/$submissionId"
              params={{ tenantId, submissionId: r.submission_id }}
              search={{ bc_id: bcId }}
            >
              {r.batch_short_id}
            </Link>
            <span className="text-xs text-muted-foreground">
              {displayTime(r.created_at)}
            </span>
            <Identifier value={r.submission_id} />
          </div>
        ),
      },
      {
        header: "状态",
        cell: ({ row: { original: r } }) => (
          <SubmissionBadge status={r.status} />
        ),
      },
      {
        header: "剧目 / 账户",
        cell: ({ row: { original: r } }) => (
          <div className="flex flex-col gap-1">
            <span>
              {r.drama_count} 剧 / {r.account_count} 户
            </span>
            <span className="text-xs text-muted-foreground">
              {r.provider_name || "暂无冻结版权方"} · {r.strategy_label}
            </span>
          </div>
        ),
      },
      {
        header: "创建结果",
        cell: ({ row: { original: r } }) => (
          <div className="flex flex-col gap-1">
            <span>
              已创建 {r.succeeded.ad_count} / 已提交 {r.submitted.ad_count} 条
              Ad
            </span>
            <span className="text-xs text-muted-foreground">
              Campaign {r.succeeded.campaign_count}/{r.submitted.campaign_count}{" "}
              · Ad Group {r.succeeded.adgroup_count}/{r.submitted.adgroup_count}
            </span>
          </div>
        ),
      },
      {
        header: "异常与排除",
        cell: ({ row: { original: r } }) => (
          <div className="flex flex-col gap-1">
            {(
              [
                ["失败", countObjects(r.failed), "issues", "FAILED"],
                ["待核实", countObjects(r.unknown), "issues", "UNKNOWN"],
                ["排除", r.excluded_unit_count, "excluded", ""],
              ] as const
            ).map(([label, n, tab, result]) => (
              <Link
                key={label}
                className="text-xs hover:underline"
                to="/tenants/$tenantId/build-tasks/$submissionId"
                params={{ tenantId, submissionId: r.submission_id }}
                search={{ bc_id: bcId, tab, result }}
              >
                {label} {n} {label === "排除" ? "个组合" : "个对象"}
              </Link>
            ))}
          </div>
        ),
      },
      { header: "提交人", accessorKey: "actor_name" },
      {
        header: "最近更新",
        cell: ({ row: { original: r } }) => (
          <span className="text-xs" title={displayTime(r.updated_at)}>
            {displayTime(r.updated_at)}
          </span>
        ),
      },
      {
        header: "操作",
        cell: ({ row: { original: r } }) => (
          <Link
            className="font-medium hover:underline"
            to="/tenants/$tenantId/build-tasks/$submissionId"
            params={{ tenantId, submissionId: r.submission_id }}
            search={{ bc_id: bcId }}
          >
            查看详情
          </Link>
        ),
      },
    ],
    [tenantId, bcId],
  )
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">搭建任务</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            查看创建进度并处理异常
          </p>
        </div>
        {allowed && (
          <Button asChild>
            <Link
              to="/tenants/$tenantId/builds/new"
              params={{ tenantId }}
              search={{ bc_id: bcId }}
            >
              新建搭建
            </Link>
          </Button>
        )}
      </div>
      <Tabs
        value={status}
        onValueChange={(value) => apply({ status_group: value })}
      >
        <TabsList>
          <TabsTrigger value="all">全部</TabsTrigger>
          <TabsTrigger value="active">排队与进行中</TabsTrigger>
          <TabsTrigger value="attention">需要处理</TabsTrigger>
          <TabsTrigger value="completed">已完成</TabsTrigger>
        </TabsList>
      </Tabs>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          e.stopPropagation()
          apply({ q: input })
        }}
      >
        <Field className="min-w-56 flex-1">
          <FieldLabel htmlFor="task-search">搜索任务</FieldLabel>
          <Input
            id="task-search"
            placeholder="任务编号、剧名或账户"
            value={input}
            maxLength={255}
            onChange={(e) => setInput(e.target.value)}
          />
        </Field>
        <Button type="submit" variant="outline">
          搜索
        </Button>
        <Field className="w-auto">
          <FieldLabel>提交日期</FieldLabel>
          <FilterSelect
            label="提交日期范围"
            value={range}
            onChange={(value) => {
              const dates =
                value === "all"
                  ? { from: "", to: "" }
                  : dateRange(value === "30" ? 30 : 7)
              apply({ range: value, ...(value === "custom" ? {} : dates) })
            }}
            choices={{
              "7": "最近 7 天",
              "30": "最近 30 天",
              custom: "自定义日期",
              all: "全部日期",
            }}
          />
        </Field>
        {range === "custom" && (
          <>
            <Field className="w-auto">
              <FieldLabel htmlFor="task-from">开始日期</FieldLabel>
              <Input
                id="task-from"
                type="date"
                value={from}
                onChange={(e) => apply({ from: e.target.value })}
              />
            </Field>
            <Field className="w-auto">
              <FieldLabel htmlFor="task-to">结束日期</FieldLabel>
              <Input
                id="task-to"
                type="date"
                value={to}
                onChange={(e) => apply({ to: e.target.value })}
              />
            </Field>
          </>
        )}
        <Field className="w-auto">
          <FieldLabel>版权方</FieldLabel>
          <DirectoryPicker<ProviderConnectionPublic>
            label="筛选版权方"
            valueLabel={providerLabel || provider || "全部版权方"}
            queryKey={["tenant", tenantId, "task-provider-filter"]}
            load={async (query, cursor, limit, signal) =>
              (
                await ProvidersService.listConnections({
                  path: { tenant_id: tenantId },
                  query: { query, cursor, limit },
                  signal,
                })
              ).data
            }
            renderItem={(r) => <span>{r.display_name}</span>}
            onSelect={(r) => {
              setProviderLabel(r.display_name)
              apply({ provider_connection_id: r.id })
            }}
          />
        </Field>
        {provider && (
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setProviderLabel(undefined)
              apply({ provider_connection_id: undefined })
            }}
          >
            清除版权方
          </Button>
        )}
        <Button
          type="button"
          variant="ghost"
          onClick={() => {
            setInput("")
            setProviderLabel(undefined)
            apply({
              q: undefined,
              status_group: "all",
              provider_connection_id: undefined,
              range: "all",
              from: "",
              to: "",
            })
          }}
        >
          清除筛选
        </Button>
      </form>
      {newer && (
        <Alert>
          <AlertDescription>
            <p>有新任务，刷新查看。</p>
            <Button
              variant="outline"
              onClick={() => {
                paging.reset()
                setHeadSeen(head.data?.items[0]?.submission_id)
                void query.refetch()
              }}
            >
              刷新至首页
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {query.error && (!query.data || isForbidden(query.error)) ? (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <ServerTable
            rows={query.data?.items || []}
            columns={columns}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={false}
            emptyTitle={
              q ||
              provider ||
              status !== "all" ||
              (head.data?.items.length ?? 0) > 0
                ? "没有符合筛选的任务"
                : "还没有搭建任务"
            }
          />
          <Pager
            paging={paging}
            nextCursor={query.data?.next_cursor}
            busy={query.isFetching}
          />
        </>
      )}
      <p className="text-xs text-muted-foreground">
        日期按浏览器本地时区筛选。任务创建结果与审核、实际投放和消耗分别记录；本页不会改变广告状态。
      </p>
    </div>
  )
}
