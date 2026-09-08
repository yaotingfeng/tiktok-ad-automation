import { useQuery } from "@tanstack/react-query"
import { useRouterState } from "@tanstack/react-router"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { isForbidden } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { StrategyError as RequestError } from "./feedback"
import { strategyQuery, versionQuery } from "./queries"
import { StrategyForm } from "./StrategyForm"
export function StrategyEditor({
  strategyId,
  versionId,
  copyVersionId,
  readonly = false,
}: {
  strategyId?: string
  versionId?: string
  copyVersionId?: string
  readonly?: boolean
}) {
  const { tenantId, scope } = useTenantScope(),
    requestedVersion = versionId || copyVersionId
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const routeMatches =
    pathname.replace(/\/$/, "") ===
    `/tenants/${tenantId}/strategies/${strategyId || "new"}`
  const version = useQuery({
    ...versionQuery(tenantId!, requestedVersion || ""),
    enabled: routeMatches && !!requestedVersion,
  })
  const recordId = strategyId || version.data?.strategy_id
  const record = useQuery({
    ...strategyQuery(tenantId!, recordId || ""),
    enabled: routeMatches && !!recordId,
  })
  if (!routeMatches) return null
  if (!strategyId && scope?.role === "viewer")
    return (
      <Alert>
        <AlertTitle>当前角色不可新建策略</AlertTitle>
        <AlertDescription>
          只读成员可以查看已有策略与历史版本。
        </AlertDescription>
      </Alert>
    )
  if (version.error && (!version.data || isForbidden(version.error)))
    return (
      <RequestError
        error={version.error}
        retry={() => void version.refetch()}
      />
    )
  if (record.error && (!record.data || isForbidden(record.error)))
    return (
      <RequestError error={record.error} retry={() => void record.refetch()} />
    )
  if ((requestedVersion && version.isPending) || (recordId && record.isPending))
    return (
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Skeleton className="h-96" />
        <Skeleton className="h-72" />
      </div>
    )
  if (strategyId && version.data && version.data.strategy_id !== strategyId)
    return (
      <Alert variant="destructive">
        <AlertTitle>版本不属于当前策略</AlertTitle>
        <AlertDescription>请返回策略列表选择对应版本。</AlertDescription>
      </Alert>
    )
  return (
    <>
      {!!record.error && (
        <RequestError
          error={record.error}
          retry={() => void record.refetch()}
        />
      )}
      {!!version.error && (
        <RequestError
          error={version.error}
          retry={() => void version.refetch()}
        />
      )}
      <StrategyForm
        key={`${tenantId}:${strategyId || "new"}:${requestedVersion || "latest"}`}
        strategy={record.data}
        version={version.data}
        copy={!!copyVersionId}
        forceReadonly={
          readonly ||
          !!(
            version.data &&
            strategyId &&
            version.data.id !== record.data?.version_id
          )
        }
      />
    </>
  )
}
