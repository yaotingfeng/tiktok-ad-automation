import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useSearch } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useMemo, useState } from "react"
import { AccountsService, type BCPublic, type ConnectionPublic } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
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
import {
  connectionLabels,
  displayTime,
  FilterSelect,
  Identifier,
} from "./presentation"

export function ConnectionsPage() {
  const { tenantId, scope, user } = useTenantScope()
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
      state.state.data?.items.some((item) => item.status === "DISCOVERING")
        ? 5000
        : false,
  })
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
  const ready = configuration.data?.configured === true
  const columns = useMemo<ColumnDef<ConnectionPublic>[]>(
    () => [
      {
        header: "授权连接",
        cell: ({ row }) => (
          <div className="flex flex-col gap-1">
            <strong>TikTok 授权连接</strong>
            <Identifier value={row.original.id} />
          </div>
        ),
      },
      {
        header: "授权状态",
        cell: ({ row }) => (
          <Badge variant="outline">
            {connectionLabels[row.original.status]}
          </Badge>
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
            查看关联 BC
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
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!ready}
                  onClick={() =>
                    setAction({ kind: "authorize", connection: row.original })
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
    [manage, ready],
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
      {configuration.data && !ready && (
        <Alert>
          <AlertTitle>等待配置开发者应用</AlertTitle>
          <AlertDescription>
            {user.is_superuser
              ? "请在平台部署配置中完成 TikTok 应用及凭据加密配置。此页面不展示密钥。"
              : "请联系平台管理员完成应用接入配置。"}
            {configuration.data.code ===
              "connection_encryption_unconfigured" && (
              <p>凭据加密配置尚未完成。</p>
            )}
          </AlertDescription>
        </Alert>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          当前租户的全部授权连接，不受顶栏 BC 筛选影响。
        </p>
        {manage && (
          <Button
            disabled={!ready}
            onClick={() => setAction({ kind: "authorize" })}
          >
            新增授权
          </Button>
        )}
      </div>
      <Card>
        <CardContent className="p-0">
          <div className="flex flex-wrap items-center gap-3 p-4">
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
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching}
          />
        </CardContent>
      </Card>
      {detail && (
        <ConnectionDetails detail={detail} onClose={() => setDetail(null)} />
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
            ? "停用后，该连接将不能用于新的系统操作及尚未执行的步骤。历史记录保留，不会停止 TikTok 上已启用的广告。已停用连接不能重新授权，可单独新增授权。"
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
  const { tenantId } = useTenantScope()
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
  })
  const data = useRetainedData(query.data, query.error)
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
  ]
  return (
    <ManagementSheet
      title="连接详情"
      description="当前租户的授权连接信息，不包含任何凭据"
      dirty={false}
      onClose={onClose}
    >
      <div className="flex flex-col gap-5">
        <dl className="grid grid-cols-[auto_1fr] gap-4 text-sm">
          <dt>连接 ID</dt>
          <dd className="overflow-x-auto">
            <Identifier value={detail.id} />
          </dd>
          <dt>授权状态</dt>
          <dd>{connectionLabels[detail.status]}</dd>
          <dt>最近授权生效</dt>
          <dd>{displayTime(detail.last_authorized_at)}</dd>
          <dt>最近发现</dt>
          <dd>{displayTime(detail.last_discovery)}</dd>
          <dt>异常状态</dt>
          <dd className="break-all">{detail.error_code || "未记录异常"}</dd>
        </dl>
        <div>
          <h2 className="mb-3 font-semibold">关联 BC</h2>
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
        </div>
      </div>
    </ManagementSheet>
  )
}
