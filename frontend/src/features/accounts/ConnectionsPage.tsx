import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useSearch } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo, useRef, useState } from "react"
import { AccountsService, type BCPublic, type ConnectionPublic } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  canManage,
  errorMessage,
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { McpAuthorizationSheet } from "./McpAuthorizationSheet"
import {
  bindingLabels,
  capabilityLabel,
  connectionLabels,
  discoveryLabels,
  displayTime,
  FilterSelect,
  Identifier,
  refreshLabels,
} from "./presentation"

function isDiscoveryPending(connection: ConnectionPublic) {
  return (
    connection.status !== "DISABLED" &&
    (connection.discovery_status
      ? ["QUEUED", "RUNNING", "ADMISSION_WAIT"].includes(
          connection.discovery_status,
        )
      : connection.status === "DISCOVERING")
  )
}

function isRefreshPending(connection: ConnectionPublic) {
  return (
    connection.status === "ACTIVE" &&
    ["PENDING", "CLAIMED", "REQUEST_ARMED", "CANDIDATE_READY"].includes(
      connection.refresh_status ?? "",
    )
  )
}

function isConnectionPending(connection: ConnectionPublic) {
  return (
    (connection.status !== "DISABLED" &&
      (connection.pending_binding_count ?? 0) > 0) ||
    isDiscoveryPending(connection) ||
    isRefreshPending(connection)
  )
}

export function ConnectionsPage() {
  const { tenantId, scope } = useTenantScope()
  const navigate = useNavigate()
  const [chooseChannel, setChooseChannel] = useState(false)
  const [mcpAction, setMcpAction] = useState<{
    connectionId?: string
    attemptId?: string
    addBindings?: boolean
  } | null>(null)
  const shownAttempt = useRef<string | null>(null)
  const search = useSearch({ from: "/_layout/tenants/$tenantId/accounts" })
  const [status, setStatus] = useState("all")
  const [detail, setDetail] = useState<ConnectionPublic | null>(null)
  const [action, setAction] = useState<{
    kind: "authorize" | "disable"
    connection?: ConnectionPublic
  } | null>(null)
  const paging = useCursorPage()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "connections",
      status,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await AccountsService.getConnections({
          path: { tenant_id: tenantId! },
          query: {
            status: status === "all" ? undefined : status,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    refetchInterval: (state) =>
      !state.state.error && state.state.data?.items.some(isConnectionPending)
        ? 5000
        : false,
  })
  const observedDiscovery = useRef(new Map<string, ConnectionPublic>())
  const visiblePending = useRef(new Set<string>())
  useEffect(() => {
    if (!query.data) return // Keep observations while filters/pages are loading.
    const visibleIds = new Set(
      query.data.items.map((connection) => connection.id),
    )
    // A pending row may leave a filter/page without its outcome being observed.
    // Re-read accepted directories; disappearance is not a completion event.
    let changed = Array.from(visiblePending.current).some(
      (id) => !visibleIds.has(id),
    )
    visiblePending.current = new Set(
      query.data.items
        .filter(isConnectionPending)
        .map((connection) => connection.id),
    )
    for (const connection of query.data.items) {
      const previous = observedDiscovery.current.get(connection.id)
      if (
        (previous &&
          previous.pending_binding_count !==
            connection.pending_binding_count) ||
        (previous &&
          isRefreshPending(previous) &&
          !isRefreshPending(connection)) ||
        (connection.discovery_status === "COMPLETE" &&
          previous?.discovery_status !== "COMPLETE") ||
        (previous &&
          connection.status === "ACTIVE" &&
          (previous.status === "DISCOVERING" ||
            previous.last_discovery !== connection.last_discovery ||
            previous.last_authorized_at !== connection.last_authorized_at))
      )
        changed = true
      observedDiscovery.current.set(connection.id, connection)
    }
    if (!changed) return
    // Discovery commits alter all local directory projections, not only the
    // connection row. Invalidate this tenant only, including inactive pages.
    for (const resource of ["bcs", "accounts", "connection-bcs"]) {
      void queryClient.invalidateQueries({
        queryKey: ["tenant", tenantId, resource],
      })
    }
  }, [query.data, queryClient, tenantId])
  const configuration = useQuery({
    queryKey: ["tenant", tenantId, "tiktok-configuration"],
    queryFn: async ({ signal }) =>
      (
        await AccountsService.getConfiguration({
          path: { tenant_id: tenantId! },
          signal,
        })
      ).data,
  })
  const data = useRetainedData(query.data, query.error)
  const manage =
    canManage(scope?.role) &&
    !isForbidden(query.error) &&
    !isForbidden(configuration.error)
  const channels = configuration.data?.channels ?? []
  const ready =
    channels.find((item) => item.kind === "OFFICIAL_API")?.configured === true
  const mcpReady =
    channels.find((item) => item.kind === "OFFICIAL_MCP")?.configured === true
  useEffect(() => {
    if (
      manage &&
      search.mcp_authorization === "CANDIDATE_READY" &&
      search.attempt_id &&
      shownAttempt.current !== search.attempt_id
    ) {
      shownAttempt.current = search.attempt_id
      setMcpAction({ attemptId: search.attempt_id })
    }
  }, [manage, search.mcp_authorization, search.attempt_id])
  const closeMcp = () => {
    setMcpAction(null)
    void navigate({
      to: "/tenants/$tenantId/accounts",
      params: { tenantId: tenantId! },
      search: { bc_id: search.bc_id, tab: "connections" },
      replace: true,
    })
  }
  const columns = useMemo<ColumnDef<ConnectionPublic>[]>(
    () => [
      {
        header: "授权连接",
        cell: ({ row }) => (
          <div className="flex flex-col gap-1">
            <strong>
              {row.original.display_name ||
                (row.original.kind === "OFFICIAL_MCP"
                  ? "官方 MCP"
                  : "官方 API")}
            </strong>
            <span className="text-xs text-muted-foreground">
              {row.original.kind === "OFFICIAL_MCP" ? "官方 MCP" : "官方 API"}
              {row.original.is_default ? " · 已有 BC 设为默认执行连接" : ""}
            </span>
            <Identifier value={row.original.id} />
          </div>
        ),
      },
      {
        header: "授权状态",
        cell: ({ row }) => (
          <div className="flex flex-col gap-1">
            <Badge variant="outline">
              {connectionLabels[row.original.status]}
            </Badge>
            {(row.original.pending_binding_count ?? 0) > 0 && (
              <span className="text-xs text-muted-foreground">
                {row.original.pending_binding_count} 个 BC 正在同步
              </span>
            )}
            {row.original.discovery_status && (
              <span className="text-xs text-muted-foreground">
                发现进度：{discoveryLabels[row.original.discovery_status]}
              </span>
            )}
          </div>
        ),
      },
      {
        header: "关联 BC",
        cell: ({ row }) => (
          <Button
            variant="link"
            size="sm"
            onClick={() => setDetail(row.original)}
          >
            查看关联 BC（{row.original.binding_count ?? 0}）
          </Button>
        ),
      },
      {
        header: "最近授权生效",
        cell: ({ row }) => displayTime(row.original.last_authorized_at),
      },
      {
        header: "最近发现时间",
        cell: ({ row }) => displayTime(row.original.last_discovery),
      },
      {
        header: "操作",
        cell: ({ row }) => (
          <div className="flex gap-1">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setDetail(row.original)}
            >
              查看详情
            </Button>
            {manage && row.original.status !== "DISABLED" && (
              <>
                {row.original.kind === "OFFICIAL_MCP" &&
                  row.original.status === "ACTIVE" && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!mcpReady}
                      onClick={() =>
                        setMcpAction({
                          connectionId: row.original.id,
                          addBindings: true,
                        })
                      }
                    >
                      添加 BC
                    </Button>
                  )}
                {row.original.authorization_attempt_id && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      setMcpAction({
                        attemptId: row.original.authorization_attempt_id!,
                      })
                    }
                  >
                    选择 BC
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={
                    row.original.kind === "OFFICIAL_MCP" ? !mcpReady : !ready
                  }
                  onClick={() =>
                    row.original.kind === "OFFICIAL_MCP"
                      ? setMcpAction({ connectionId: row.original.id })
                      : setAction({
                          kind: "authorize",
                          connection: row.original,
                        })
                  }
                >
                  重新授权
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    setAction({ kind: "disable", connection: row.original })
                  }
                >
                  停用
                </Button>
              </>
            )}
          </div>
        ),
      },
    ],
    [manage, ready, mcpReady],
  )
  const callback = search.authorization
  return (
    <div className="flex flex-col gap-4">
      {callback && (
        <Alert>
          <AlertTitle>
            {callback === "CANDIDATE_READY"
              ? "授权已返回，等待账户发现完成"
              : callback === "CANCELLED"
                ? "已取消授权"
                : "授权未完成"}
          </AlertTitle>
          <AlertDescription>
            {callback === "CANDIDATE_READY"
              ? "账户发现完成后，新授权才正式生效。请以连接当前状态为准。"
              : callback === "CANCELLED"
                ? "原有连接状态会保留，可再次发起授权。"
                : "请检查连接状态后重新尝试；原有连接不会因本次失败被替换。"}
            {search.connection_id && (
              <span className="break-all">连接 ID：{search.connection_id}</span>
            )}
          </AlertDescription>
        </Alert>
      )}
      {configuration.isPending && (
        <p role="status">正在读取应用接入准备状态…</p>
      )}
      {configuration.error && (
        <RequestError
          error={configuration.error}
          retry={() => {
            void configuration.refetch()
          }}
        />
      )}
      {configuration.data && channels.some((item) => !item.configured) && (
        <Alert>
          <AlertTitle>接入准备状态</AlertTitle>
          <AlertDescription>
            {channels.map((item) => (
              <p key={item.kind}>
                {item.kind === "OFFICIAL_MCP" ? "官方 MCP" : "官方 API"}：
                {item.configured ? "可发起授权" : "等待平台完成接入配置"}
              </p>
            ))}
          </AlertDescription>
        </Alert>
      )}
      {search.mcp_authorization &&
        search.mcp_authorization !== "CANDIDATE_READY" && (
          <Alert>
            <AlertTitle>
              {search.mcp_authorization === "CANCELLED"
                ? "已取消 MCP 授权"
                : "MCP 授权未完成"}
            </AlertTitle>
            <AlertDescription>
              原有可用连接保留，请查看连接状态后重新授权。
            </AlertDescription>
          </Alert>
        )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          当前租户的全部授权连接，不受顶栏 BC 筛选影响。
        </p>
        {manage && (
          <Button
            disabled={!ready && !mcpReady}
            onClick={() => setChooseChannel(true)}
          >
            新增连接
          </Button>
        )}
      </div>
      <Card className="min-w-0">
        <CardHeader>
          <div className="flex flex-wrap items-center gap-3">
            <FilterSelect
              label="连接状态"
              choices={connectionLabels}
              value={status}
              onChange={(value) => {
                setStatus(value)
                paging.reset()
              }}
            />
            <Button
              variant="ghost"
              onClick={() => {
                setStatus("all")
                paging.reset()
              }}
            >
              清除筛选
            </Button>
          </div>
        </CardHeader>
        <CardContent className="min-w-0">
          <ServerTable
            rows={data?.items ?? []}
            columns={columns}
            loading={query.isPending && !data}
            fetching={query.isFetching}
            error={query.error}
            retry={() => {
              void query.refetch()
            }}
            filtered={status !== "all"}
            emptyTitle="当前租户尚无授权连接"
          />
        </CardContent>
        <CardFooter className="block">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching}
          />
        </CardFooter>
      </Card>
      {chooseChannel && (
        <ManagementSheet
          title="新增连接"
          description="选择本次授权通道"
          dirty={false}
          onClose={() => setChooseChannel(false)}
        >
          <div className="flex flex-col gap-3">
            <Button
              variant="outline"
              disabled={!ready}
              onClick={() => {
                setChooseChannel(false)
                setAction({ kind: "authorize" })
              }}
            >
              官方 API
            </Button>
            <Button
              variant="outline"
              disabled={!mcpReady}
              onClick={() => {
                setChooseChannel(false)
                setMcpAction({})
              }}
            >
              官方 MCP
            </Button>
            <p className="text-sm text-muted-foreground">
              每种通道独立授权，同一授权可接入多个 BC。管理员分别为各 BC
              选择默认执行连接。
            </p>
          </div>
        </ManagementSheet>
      )}
      {mcpAction && (
        <McpAuthorizationSheet
          key={`${tenantId}:${mcpAction.attemptId ?? mcpAction.connectionId ?? "new"}:${mcpAction.addBindings ?? false}`}
          {...mcpAction}
          ready={mcpReady}
          onClose={closeMcp}
          onSaved={() => {
            closeMcp()
            void queryClient.invalidateQueries({
              queryKey: ["tenant", tenantId],
            })
          }}
        />
      )}
      {detail && (
        <ConnectionDetails
          detail={
            query.data?.items.find(
              (connection) => connection.id === detail.id,
            ) ?? detail
          }
          onClose={() => setDetail(null)}
        />
      )}
      {action && (
        <ConnectionAction
          action={action}
          ready={ready}
          onClose={() => setAction(null)}
          onSaved={() => {
            setAction(null)
            void queryClient.invalidateQueries({
              queryKey: ["tenant", tenantId],
            })
          }}
        />
      )}
    </div>
  )
}
function ConnectionAction({
  action,
  ready,
  onClose,
  onSaved,
}: {
  action: { kind: "authorize" | "disable"; connection?: ConnectionPublic }
  ready: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const { tenantId, tenant } = useTenantScope()
  const disable = action.kind === "disable"
  const mutation = useMutation({
    mutationFn: async () => {
      if (disable) {
        await AccountsService.patchConnection({
          path: { tenant_id: tenantId!, connection_id: action.connection!.id },
          body: { status: "DISABLED" },
        })
        onSaved()
      } else {
        const { data } = await AccountsService.postAuthorization({
          path: { tenant_id: tenantId! },
          body: { connection_id: action.connection?.id ?? null },
        })
        const target = new URL(data.url)
        if (
          target.protocol !== "https:" ||
          !(
            target.hostname === "business-api.tiktok.com" ||
            target.hostname === "ads.tiktok.com"
          )
        )
          throw new Error("invalid_authorization_url")
        window.location.assign(target.href)
      }
    },
  })
  const title = disable
    ? "停用授权连接"
    : action.connection
      ? "重新授权连接"
      : "新增 TikTok 授权"
  return (
    <ManagementSheet
      title={title}
      description={`所属租户：${tenant?.name}`}
      dirty={false}
      pending={mutation.isPending}
      onClose={onClose}
      actions={
        <Button
          disabled={
            mutation.isPending ||
            isForbidden(mutation.error) ||
            (!disable && !ready)
          }
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending
            ? "正在处理…"
            : disable
              ? "确认停用"
              : "前往 TikTok 授权"}
        </Button>
      }
    >
      <div className="flex flex-col gap-4 text-sm">
        {action.connection && (
          <div>
            当前连接
            <Identifier value={action.connection.id} />
          </div>
        )}
        <p>
          {disable
            ? "停用后，此授权下全部已接入 BC 均不能再通过该连接执行新的系统操作及尚未执行的步骤。历史记录保留，不会停止 TikTok 上已启用的广告。已停用连接不能重新授权，可单独新增授权。"
            : "将在 TikTok 官方页面完成授权。返回后系统发现账户，完整发现成功后新凭据才正式生效。取消或失败不会替换原有可用连接。"}
        </p>
        {mutation.error && (
          <Alert variant="destructive">
            <AlertTitle>操作未完成</AlertTitle>
            <AlertDescription>{errorMessage(mutation.error)}</AlertDescription>
          </Alert>
        )}
      </div>
    </ManagementSheet>
  )
}

function ConnectionDetails({
  detail,
  onClose,
}: {
  detail: ConnectionPublic
  onClose: () => void
}) {
  const { tenantId, scope } = useTenantScope()
  const queryClient = useQueryClient()
  const paging = useCursorPage()
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "connection-bcs",
      detail.id,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await AccountsService.getBcs({
          path: { tenant_id: tenantId! },
          query: {
            connection_id: detail.id,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    refetchInterval: (state) =>
      !state.state.error &&
      ((detail.pending_binding_count ?? 0) > 0 ||
        state.state.data?.items.some(
          (item) => item.binding_status === "SYNCING",
        ))
        ? 5000
        : false,
  })
  const previousBCs = useRef(new Map<string, string>())
  useEffect(() => {
    if (!query.data) return
    let changed = false
    for (const item of query.data.items) {
      const signature = `${item.binding_status}:${item.last_discovery}:${item.discovery_status}`
      const previous = previousBCs.current.get(item.bc_id)
      if (previous && previous !== signature) changed = true
      previousBCs.current.set(item.bc_id, signature)
    }
    if (changed) {
      for (const resource of ["bcs", "accounts", "connections"])
        void queryClient.invalidateQueries({
          queryKey: ["tenant", tenantId, resource],
        })
    }
  }, [query.data, queryClient, tenantId])
  const data = useRetainedData(query.data, query.error)
  const [unbind, setUnbind] = useState<BCPublic | null>(null)
  const bcMutation = useMutation({
    mutationFn: async ({
      bcId,
      kind,
    }: {
      bcId: string
      kind: "sync" | "unbind"
    }) => {
      const path = {
        tenant_id: tenantId!,
        connection_id: detail.id,
        bc_id: bcId,
      }
      if (kind === "sync") await AccountsService.syncMcpBc({ path })
      else await AccountsService.unbindMcpBc({ path })
      setUnbind(null)
      await queryClient.invalidateQueries({ queryKey: ["tenant", tenantId] })
    },
  })
  const defaultMutation = useMutation({
    mutationFn: async (bcId: string) => {
      await AccountsService.putDefaultConnection({
        path: { tenant_id: tenantId!, bc_id: bcId },
        body: { connection_id: detail.id },
      })
      await queryClient.invalidateQueries({ queryKey: ["tenant", tenantId] })
    },
  })
  const manage =
    canManage(scope?.role) &&
    !isForbidden(query.error) &&
    !isForbidden(defaultMutation.error) &&
    !isForbidden(bcMutation.error)
  const columns: ColumnDef<BCPublic>[] = [
    {
      header: "关联 BC",
      cell: ({ row }) => (
        <div>
          {row.original.name || "未命名 BC"}
          <Identifier value={row.original.bc_id} />
        </div>
      ),
    },
    {
      header: "归属",
      cell: ({ row }) =>
        row.original.ownership_conflict ? "存在归属冲突" : "当前租户",
    },
    {
      header: "默认执行连接",
      cell: ({ row }) =>
        row.original.is_default ? (
          <Badge variant="outline">当前默认</Badge>
        ) : manage ? (
          <Button
            size="sm"
            variant="outline"
            disabled={
              detail.status !== "ACTIVE" ||
              (!!row.original.binding_status &&
                row.original.binding_status !== "ACTIVE") ||
              row.original.ownership_conflict ||
              defaultMutation.isPending
            }
            onClick={() => defaultMutation.mutate(row.original.bc_id)}
          >
            设为默认执行连接
          </Button>
        ) : (
          "未设为默认"
        ),
    },
  ]
  if (detail.kind === "OFFICIAL_MCP")
    columns.push(
      {
        header: "同步状态",
        cell: ({ row }) => (
          <div className="flex flex-col gap-1">
            <Badge variant="outline">
              {row.original.binding_status
                ? bindingLabels[row.original.binding_status]
                : "尚无同步记录"}
            </Badge>
            {row.original.discovery_status && (
              <span className="text-xs text-muted-foreground">
                {discoveryLabels[row.original.discovery_status]}
              </span>
            )}
            <span className="text-xs text-muted-foreground">
              {displayTime(row.original.last_discovery)}
            </span>
            {row.original.error_code && (
              <span className="text-xs">{row.original.error_code}</span>
            )}
          </div>
        ),
      },
      {
        header: "BC 操作",
        cell: ({ row }) =>
          manage && row.original.binding_status !== "DISABLED" ? (
            <div className="flex flex-wrap gap-1">
              <Button
                size="sm"
                variant="outline"
                disabled={
                  detail.status !== "ACTIVE" ||
                  row.original.binding_status === "SYNCING" ||
                  bcMutation.isPending
                }
                onClick={() =>
                  bcMutation.mutate({ bcId: row.original.bc_id, kind: "sync" })
                }
              >
                {row.original.binding_status === "ERROR"
                  ? "重试同步"
                  : "同步账户"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={detail.status === "DISABLED" || bcMutation.isPending}
                onClick={() => setUnbind(row.original)}
              >
                解绑 BC
              </Button>
            </div>
          ) : (
            "—"
          ),
      },
    )
  return (
    <ManagementSheet
      title="连接详情"
      description="当前租户的授权连接信息，不包含任何凭据"
      dirty={false}
      pending={bcMutation.isPending || defaultMutation.isPending}
      onClose={onClose}
    >
      <div className="flex flex-col gap-5">
        <dl className="grid grid-cols-[auto_1fr] gap-4 text-sm">
          <dt>调用通道</dt>
          <dd>{detail.kind === "OFFICIAL_MCP" ? "官方 MCP" : "官方 API"}</dd>
          <dt>连接 ID</dt>
          <dd className="overflow-x-auto">
            <Identifier value={detail.id} />
          </dd>
          <dt>授权状态</dt>
          <dd>{connectionLabels[detail.status]}</dd>
          <dt>发现进度</dt>
          <dd>
            {detail.discovery_status
              ? discoveryLabels[detail.discovery_status]
              : "尚无发现记录"}
          </dd>
          <dt>最近授权生效</dt>
          <dd>{displayTime(detail.last_authorized_at)}</dd>
          <dt>最近发现</dt>
          <dd>{displayTime(detail.last_discovery)}</dd>
          <dt>读取权限</dt>
          <dd>{capabilityLabel(detail.read_authorized)}</dd>
          <dt>素材上传权限</dt>
          <dd>{capabilityLabel(detail.upload_authorized)}</dd>
          <dt>广告搭建权限</dt>
          <dd>{capabilityLabel(detail.build_authorized)}</dd>
          <dt>权限核验时间</dt>
          <dd>{displayTime(detail.evidence_checked_at)}</dd>
          {detail.kind === "OFFICIAL_MCP" && (
            <>
              <dt>凭据刷新</dt>
              <dd>
                {detail.refresh_status
                  ? refreshLabels[detail.refresh_status] || "状态待核实"
                  : "尚无刷新记录"}
              </dd>
            </>
          )}
          <dt>异常状态</dt>
          <dd className="break-all">{detail.error_code || "未记录异常"}</dd>
        </dl>
        {unbind && (
          <Alert>
            <AlertTitle>解绑 {unbind.name || "未命名 BC"}</AlertTitle>
            <AlertDescription>
              <p>
                此 BC 将停止使用当前连接；其他 BC 保留接入。历史记录保留，已在
                TikTok 启用的广告不会停止。
              </p>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  disabled={bcMutation.isPending}
                  onClick={() => setUnbind(null)}
                >
                  取消解绑
                </Button>
                <Button
                  disabled={
                    bcMutation.isPending || isForbidden(bcMutation.error)
                  }
                  onClick={() =>
                    bcMutation.mutate({ bcId: unbind.bc_id, kind: "unbind" })
                  }
                >
                  确认解绑 BC
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}
        {bcMutation.error && (
          <Alert variant="destructive">
            <AlertTitle>BC 操作未完成</AlertTitle>
            <AlertDescription>
              {errorMessage(bcMutation.error)}
            </AlertDescription>
          </Alert>
        )}
        {defaultMutation.error && (
          <Alert variant="destructive">
            <AlertTitle>默认连接未更改</AlertTitle>
            <AlertDescription>
              {errorMessage(defaultMutation.error)}
            </AlertDescription>
          </Alert>
        )}
        <p className="text-sm text-muted-foreground">
          默认连接用于此 BC 后续新建的任务；已准备的任务继续使用其原连接。
        </p>
        <section className="flex min-w-0 flex-col gap-4">
          <h2 className="font-semibold">关联 BC</h2>
          <ServerTable
            rows={data?.items ?? []}
            columns={columns}
            loading={query.isPending && !data}
            fetching={query.isFetching}
            error={query.error}
            retry={() => {
              void query.refetch()
            }}
            filtered={false}
            emptyTitle="此连接尚无已完成发现的 BC"
          />
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching}
          />
        </section>
      </div>
    </ManagementSheet>
  )
}
