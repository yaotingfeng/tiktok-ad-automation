import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useMemo, useState } from "react"
import {
  type ProviderApplicationPublic,
  type ProviderLinkPublic,
  ProvidersService,
  type providersGetLinksData,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  displayTime,
  FilterSelect,
  Identifier,
} from "@/features/accounts/presentation"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { ConfigFacts, CopyField, connectionStates, kinds } from "./presentation"
import { applicationsQuery, providerKey } from "./queries"

const states = {
  ready: "可用",
  pending: "待核实",
  superseded: "已有更新版本",
  invalid: "已失效",
  result_unknown: "结果待核实",
  failed: "失败",
}
export function LinkHistory({
  connectionId,
  onConnection,
}: {
  connectionId?: string
  onConnection: (id?: string) => void
}) {
  const { tenantId, scope } = useTenantScope(),
    paging = useCursorPage(),
    [input, setInput] = useState(""),
    [search, setSearch] = useState(""),
    [application, setApplication] = useState<ProviderApplicationPublic>(),
    [status, setStatus] = useState("all"),
    [pickApp, setPickApp] = useState(false),
    [detail, setDetail] = useState<string>(),
    [connectionName, setConnectionName] = useState("")
  const query = useQuery({
    queryKey: [
      ...providerKey(tenantId!),
      "history",
      connectionId,
      application?.external_id,
      search,
      status,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await ProvidersService.getLinks({
          path: { tenant_id: tenantId! },
          query: {
            connection_id: connectionId,
            application_id: application?.external_id,
            query: search,
            status:
              status === "all"
                ? undefined
                : (status as NonNullable<
                    providersGetLinksData["query"]
                  >["status"]),
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  const data = useRetainedData(query.data, query.error),
    filtered = !!connectionId || !!application || !!search || status !== "all"
  const columns = useMemo<ColumnDef<ProviderLinkPublic>[]>(
    () => [
      {
        header: "剧目",
        cell: ({ row: { original: r } }) => (
          <div>
            <strong>{r.title}</strong>
            <Identifier value={r.external_drama_id} />
          </div>
        ),
      },
      {
        header: "版权方 / 应用",
        cell: ({ row: { original: r } }) => (
          <div className="flex flex-col gap-1">
            <span>
              {r.connection_name} · {kinds[r.provider_kind]}
            </span>
            <span>{r.application_name}</span>
            <span className="text-xs">{r.language || "语言待核实"}</span>
          </div>
        ),
      },
      {
        header: "链接状态",
        cell: ({ row }) => (
          <Badge
            variant={row.original.status === "ready" ? "secondary" : "outline"}
          >
            {states[row.original.status as keyof typeof states] ||
              row.original.status}
          </Badge>
        ),
      },
      {
        header: "推广链接",
        cell: ({ row }) => (
          <CopyField label="推广链接" value={row.original.url} />
        ),
      },
      {
        header: "归因名称",
        cell: ({ row }) => (
          <CopyField label="归因名称" value={row.original.protected_base} />
        ),
      },
      {
        header: "操作",
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setDetail(row.original.link_id)}
          >
            查看详情
          </Button>
        ),
      },
    ],
    [],
  )
  return (
    <div className="flex flex-col gap-4">
      <Card className="min-w-0">
        <CardHeader>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault()
              setSearch(input.trim())
              paging.reset()
            }}
          >
            <Field className="w-full sm:w-80">
              <FieldLabel htmlFor="provider-link-search">
                剧名或剧目 ID
              </FieldLabel>
              <Input
                id="provider-link-search"
                maxLength={255}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="搜索正式剧名或完整剧目 ID"
              />
            </Field>
            <DirectoryPicker
              label="版权方连接"
              valueLabel={
                connectionId ? connectionName || connectionId : undefined
              }
              queryKey={[...providerKey(tenantId!), "connection-filter"]}
              load={async (query, cursor, limit, signal) =>
                (
                  await ProvidersService.listConnections({
                    path: { tenant_id: tenantId! },
                    query: { query, cursor, limit },
                    signal,
                  })
                ).data!
              }
              renderItem={(row) => (
                <span>
                  {row.display_name} · {kinds[row.kind]}
                </span>
              )}
              onSelect={(row) => {
                setConnectionName(row.display_name)
                setApplication(undefined)
                paging.reset()
                onConnection(row.id)
              }}
            />
            <Button
              type="button"
              variant="outline"
              disabled={!connectionId}
              onClick={() => setPickApp(true)}
            >
              {application?.name || "筛选应用"}
            </Button>
            <FilterSelect
              label="链接状态"
              choices={states}
              value={status}
              onChange={(v) => {
                setStatus(v)
                paging.reset()
              }}
            />
            <Button type="submit" variant="outline">
              搜索
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setInput("")
                setSearch("")
                setApplication(undefined)
                setConnectionName("")
                setStatus("all")
                paging.reset()
                onConnection(undefined)
              }}
            >
              清除筛选
            </Button>
          </form>
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          <ServerTable
            rows={data?.items || []}
            columns={columns}
            loading={query.isPending && !data}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={filtered}
            emptyTitle="尚无推广链接记录"
          />
          {query.error && data && (
            <p role="status" className="text-sm text-muted-foreground">
              保留上次读取的列表，请重试以获取当前结果。
            </p>
          )}
        </CardContent>
        <CardFooter className="block">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching}
          />
        </CardFooter>
      </Card>
      {!query.isPending && !query.error && !data?.items.length && !filtered && (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>在广告搭建中准备推广链接</EmptyTitle>
            <EmptyDescription>
              这里展示当前租户已记录的推广链接。
            </EmptyDescription>
          </EmptyHeader>
          {scope?.role !== "viewer" && (
            <Button asChild variant="outline">
              <Link
                to="/tenants/$tenantId/builds/new"
                params={{ tenantId: tenantId! }}
                search={{ bc_id: scope?.bcId || undefined }}
              >
                前往广告搭建
              </Link>
            </Button>
          )}
        </Empty>
      )}
      {detail && (
        <HistoryDetail
          tenantId={tenantId!}
          linkId={detail}
          onClose={() => setDetail(undefined)}
        />
      )}{" "}
      {pickApp && connectionId && (
        <ApplicationFilter
          tenantId={tenantId!}
          connectionId={connectionId}
          onClose={() => setPickApp(false)}
          onSelect={(row) => {
            setApplication(row)
            paging.reset()
            setPickApp(false)
          }}
        />
      )}
    </div>
  )
}
function ApplicationFilter({
  tenantId,
  connectionId,
  onClose,
  onSelect,
}: {
  tenantId: string
  connectionId: string
  onClose: () => void
  onSelect: (row: ProviderApplicationPublic) => void
}) {
  const paging = useCursorPage(),
    query = useQuery(
      applicationsQuery(tenantId, connectionId, paging.cursor, paging.limit),
    )
  const columns = useMemo<ColumnDef<ProviderApplicationPublic>[]>(
    () => [
      {
        header: "应用",
        cell: ({ row }) => (
          <div>
            {row.original.name}
            <Identifier value={row.original.external_id} />
          </div>
        ),
      },
      {
        header: "状态",
        cell: ({ row }) =>
          row.original.available ? "可用" : "历史应用（当前不可用）",
      },
      {
        header: "筛选",
        cell: ({ row }) => (
          <Button onClick={() => onSelect(row.original)}>筛选此应用</Button>
        ),
      },
    ],
    [onSelect],
  )
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>筛选版权方应用</DialogTitle>
          <DialogDescription>
            历史不可用应用也可用于查询既有链接，不会成为新的取链目标。
          </DialogDescription>
        </DialogHeader>
        <ServerTable
          rows={query.data?.items || []}
          columns={columns}
          loading={query.isPending}
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          emptyTitle="该连接尚无已发现应用"
        />
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          busy={query.isFetching}
        />
      </DialogContent>
    </Dialog>
  )
}
function HistoryDetail({
  tenantId,
  linkId,
  onClose,
}: {
  tenantId: string
  linkId: string
  onClose: () => void
}) {
  const query = useQuery({
      queryKey: [...providerKey(tenantId), "link", linkId],
      queryFn: async ({ signal }) =>
        (
          await ProvidersService.linkDetails({
            path: { tenant_id: tenantId, link_id: linkId },
            signal,
          })
        ).data,
    }),
    row = query.data
  return (
    <ManagementSheet
      title="推广链接详情"
      description="当前租户保存的完整链接与归因信息"
      dirty={false}
      onClose={onClose}
    >
      {query.error && (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      )}{" "}
      {query.isPending && <p role="status">正在读取链接详情…</p>}
      {row && (
        <div className="flex flex-col gap-4 text-sm">
          <h3 className="font-semibold">{row.title}</h3>
          <Identifier value={row.external_drama_id} />
          <p>
            {row.connection_name} ·{" "}
            {connectionStates[row.connection_status] || row.connection_status}
          </p>
          <p>
            {row.application_name} · {row.application_id}
          </p>
          <p>{row.language || "语言待核实"}</p>
          <p>
            {states[row.status as keyof typeof states] || row.status} · 版本{" "}
            {row.version} · 最近验证：{displayTime(row.verified_at)}
          </p>
          <h3 className="font-semibold">推广链接</h3>
          <CopyField expanded label="推广链接" value={row.url} />
          <h3 className="font-semibold">归因名称</h3>
          <CopyField expanded label="归因名称" value={row.protected_base} />
          <h3 className="font-semibold">已记录配置</h3>
          <ConfigFacts
            config={row.config}
            incomplete={row.config_display_incomplete}
          />
        </div>
      )}
    </ManagementSheet>
  )
}
