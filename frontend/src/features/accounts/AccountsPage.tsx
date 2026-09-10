import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useSearch } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo, useState } from "react"
import { type AccountPublic, AccountsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { ConnectionsPage } from "./ConnectionsPage"
import {
  availabilityLabels,
  displayTime,
  FilterSelect,
  Identifier,
} from "./presentation"

export function AccountsPage() {
  const { tenantId, tenant } = useTenantScope()
  const search = useSearch({ from: "/_layout/tenants/$tenantId/accounts" })
  const navigate = useNavigate()
  return (
    <>
      <div className="flex flex-col gap-2">
        <WorkspacePageTitle>账户与授权</WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          {tenant?.name} · 查看本地账户目录与授权连接
        </p>
      </div>
      <Tabs
        className="gap-4"
        value={search.tab}
        onValueChange={(tab) => {
          void navigate({
            to: "/tenants/$tenantId/accounts",
            params: { tenantId: tenantId! },
            search: {
              bc_id: search.bc_id,
              tab: tab === "connections" ? "connections" : "accounts",
            },
          })
        }}
      >
        <TabsList>
          <TabsTrigger value="accounts">账户</TabsTrigger>
          <TabsTrigger value="connections">授权连接</TabsTrigger>
        </TabsList>
        <TabsContent value="accounts">
          <AccountDirectory />
        </TabsContent>
        <TabsContent value="connections">
          <ConnectionsPage />
        </TabsContent>
      </Tabs>
    </>
  )
}
function AccountDirectory() {
  const { tenantId, scope, bc, bcPending, bcError, retryBC } = useTenantScope()
  const queryClient = useQueryClient()
  useEffect(() => {
    // Discovery can finish while its row is off-page or this tab is closed.
    // Account entry always rechecks the accepted BC directory, without discovery.
    void queryClient.invalidateQueries({
      queryKey: ["tenant", tenantId, "bcs"],
    })
  }, [tenantId, queryClient])
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [remoteInput, setRemoteInput] = useState("")
  const [remoteStatus, setRemoteStatus] = useState("")
  const [availability, setAvailability] = useState("all")
  const [detail, setDetail] = useState<AccountPublic | null>(null)
  const paging = useCursorPage()
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "accounts",
      scope?.bcId,
      search,
      remoteStatus,
      availability,
      paging.cursor,
      paging.limit,
    ],
    enabled: !!bc,
    queryFn: async ({ signal }) =>
      (
        await AccountsService.getAccounts({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bc!.bc_id,
            query: search,
            remote_status: remoteStatus || undefined,
            availability:
              availability === "all"
                ? undefined
                : (availability as AccountPublic["availability"]),
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  const data = useRetainedData(query.data, query.error)
  const columns = useMemo<ColumnDef<AccountPublic>[]>(
    () => [
      {
        header: "广告账户",
        cell: ({ row: { original: item } }) => (
          <div className="flex flex-col gap-1">
            <strong>{item.name || "名称待完善"}</strong>
            <Identifier value={item.advertiser_id} />
          </div>
        ),
      },
      {
        header: "币种与时区",
        cell: ({ row: { original: item } }) => (
          <div>
            {item.currency || "待完善"}
            <p className="text-xs text-muted-foreground">
              {item.timezone || "待完善"}
            </p>
          </div>
        ),
      },
      {
        header: "平台状态",
        cell: ({ row }) => row.original.remote_status || "待完善",
      },
      {
        header: "可用性",
        cell: ({ row: { original: item } }) => (
          <div className="flex flex-col gap-1">
            <Badge
              variant={
                item.availability === "AVAILABLE" ? "secondary" : "outline"
              }
            >
              {availabilityLabels[item.availability]}
            </Badge>
            <span className="text-xs">
              {item.can_build ? "可搭建" : "不可搭建"} ·{" "}
              {item.can_upload ? "可上传" : "不可上传"}
            </span>
          </div>
        ),
      },
      {
        header: "核验时间",
        cell: ({ row }) =>
          row.original.checked_at
            ? displayTime(row.original.checked_at)
            : "尚未核验",
      },
      {
        header: "操作",
        cell: ({ row }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setDetail(row.original)}
          >
            查看详情
          </Button>
        ),
      },
    ],
    [],
  )
  if (bcError) return <RequestError error={bcError} retry={retryBC} />
  if (!bc && !bcPending)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>当前没有可用 BC</EmptyTitle>
          <EmptyDescription>
            请切换 BC
            或在“授权连接”查看接入状态。目录不会因打开页面自动发现账户。
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        当前 BC：{bc?.name || "正在读取…"} ·
        仅展示本地已知目录；每个账户的核验时间见列表。
      </p>
      <div className="flex min-w-0 flex-col gap-4">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            setSearch(input.trim())
            setRemoteStatus(remoteInput.trim())
            paging.reset()
          }}
        >
          <Field className="w-full sm:w-80">
            <FieldLabel htmlFor="account-search">账户名称或 ID</FieldLabel>
            <Input
              id="account-search"
              maxLength={255}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="搜索账户名称或完整 ID"
            />
          </Field>
          <Field className="w-44">
            <FieldLabel htmlFor="remote-status">平台状态</FieldLabel>
            <Input
              id="remote-status"
              maxLength={64}
              value={remoteInput}
              onChange={(event) => setRemoteInput(event.target.value)}
              placeholder="输入平台原始状态"
            />
          </Field>
          <FilterSelect
            label="可用性"
            choices={availabilityLabels}
            value={availability}
            onChange={(value) => {
              setAvailability(value)
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
              setRemoteInput("")
              setRemoteStatus("")
              setAvailability("all")
              paging.reset()
            }}
          >
            清除筛选
          </Button>
        </form>
        <ServerTable
          rows={data?.items ?? []}
          columns={columns}
          loading={bcPending || (query.isPending && !data)}
          fetching={bcPending || query.isFetching}
          error={query.error}
          retry={() => {
            void query.refetch()
          }}
          filtered={!!search || !!remoteStatus || availability !== "all"}
          emptyTitle="当前 BC 尚无账户"
        />
        <Pager
          paging={paging}
          nextCursor={data?.next_cursor}
          busy={query.isFetching || bcPending}
        />
      </div>
      {detail && (
        <ManagementSheet
          title="账户详情"
          description="当前租户与 BC 下的已知账户信息"
          dirty={false}
          onClose={() => setDetail(null)}
        >
          <dl className="grid grid-cols-[auto_1fr] gap-4 text-sm">
            <dt>账户名称</dt>
            <dd>{detail.name || "待完善"}</dd>
            <dt>账户 ID</dt>
            <dd className="overflow-x-auto">
              <Identifier value={detail.advertiser_id} />
            </dd>
            <dt>所属 BC</dt>
            <dd className="break-all">
              {bc?.name} · {detail.bc_id}
            </dd>
            <dt>币种 / 时区</dt>
            <dd>
              {detail.currency || "待完善"} / {detail.timezone || "待完善"}
            </dd>
            <dt>平台状态</dt>
            <dd>{detail.remote_status || "待完善"}</dd>
            <dt>权限状态</dt>
            <dd>{detail.permission_state || "尚未核实"}</dd>
            <dt>可用性</dt>
            <dd>
              {availabilityLabels[detail.availability]}
              <p>
                {detail.can_build ? "可搭建" : "不可搭建"} ·{" "}
                {detail.can_upload ? "可上传" : "不可上传"}
              </p>
            </dd>
            <dt>核验时间</dt>
            <dd>
              {detail.checked_at ? displayTime(detail.checked_at) : "尚未核验"}
            </dd>
            {detail.ownership_conflict && (
              <>
                <dt>异常原因</dt>
                <dd>该账户存在其他租户归属。</dd>
              </>
            )}
          </dl>
        </ManagementSheet>
      )}
    </div>
  )
}
