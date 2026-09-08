import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Link,
  Navigate,
  useNavigate,
  useRouterState,
} from "@tanstack/react-router"
import { createContext, type ReactNode, useContext, useEffect } from "react"
import {
  AccountsService,
  type BCPublic,
  type Page_BCPublic_,
  type TenantSummary,
  TenantsService,
  type UserPublic,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { DirectoryPicker } from "./DirectoryPicker"
import { canManage, RequestError } from "./shared"

export type TenantScope = {
  tenantId: string
  bcId: string | null
  role: TenantSummary["role"]
}
type ScopeValue = {
  scope: TenantScope | null
  tenant: TenantSummary | null
  tenantId: string | null
  pending: boolean
  error: unknown
  forbidden: boolean
  platform: boolean
  user: UserPublic
  switchTenant: (tenant: TenantSummary) => void
  retry: () => void
  bc: BCPublic | null
  bcDirectory?: Page_BCPublic_
  bcPending: boolean
  bcError: unknown
  retryBC: () => void
  switchBC: (bc: BCPublic) => void
}
const ScopeContext = createContext<ScopeValue | null>(null)

export function TenantScopeProvider({
  user,
  children,
}: {
  user: UserPublic
  children: ReactNode
}) {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const searchParams = useRouterState({
    select: (state) =>
      state.location.search as {
        bc_id?: string
        tab?: "accounts" | "connections"
      },
  })
  const requestedBC = searchParams.bc_id
  const tenantId = /^\/tenants\/([^/]+)/.exec(pathname)?.[1] ?? null
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: ["tenant", tenantId, "scope"],
    enabled: !!tenantId,
    queryFn: async ({ signal }) =>
      (
        await TenantsService.getMyTenants({
          query: { search: tenantId!, active: true, limit: 50 },
          signal,
        })
      ).data,
  })
  const tenant =
    query.data?.items.find((item) => item.id === tenantId && item.active) ??
    null
  const bcQuery = useQuery({
    queryKey: ["tenant", tenantId, "bcs", "directory"],
    enabled: !!tenant,
    queryFn: async ({ signal }) =>
      (
        await AccountsService.getBcs({
          path: { tenant_id: tenantId! },
          query: { limit: 50 },
          signal,
        })
      ).data,
  })
  const initialBC = bcQuery.data?.items.find(
    (item) => item.bc_id === requestedBC,
  )
  const exactBC = useQuery({
    queryKey: ["tenant", tenantId, "bcs", "exact", requestedBC],
    enabled: !!tenant && !!requestedBC && !!bcQuery.data && !initialBC,
    queryFn: async ({ signal }) =>
      (
        await AccountsService.getBcs({
          path: { tenant_id: tenantId! },
          query: { query: requestedBC, limit: 50 },
          signal,
        })
      ).data,
  })
  const bc = requestedBC
    ? (initialBC ??
      exactBC.data?.items.find((item) => item.bc_id === requestedBC) ??
      null)
    : null
  // Scope identity comes only from a committed URL transition. Query results
  // validate that selection; they must never remount an unsaved editor first.
  const bcId = requestedBC ?? null
  const defaultBC = bcQuery.data?.items[0]
  const switchBC = (target: BCPublic) => {
    if (target.bc_id === bcId) return
    void navigate({
      to: pathname,
      search: {
        bc_id: target.bc_id,
        ...(searchParams.tab ? { tab: searchParams.tab } : {}),
      },
    })
  }
  useEffect(() => {
    if (!defaultBC || requestedBC) return
    void navigate({
      to: pathname,
      search: { ...searchParams, bc_id: defaultBC.bc_id },
      replace: true,
    })
  }, [defaultBC, requestedBC, pathname, navigate, searchParams])
  useEffect(() => {
    if (!tenantId || !bcId) return
    return () => {
      void queryClient.cancelQueries({
        queryKey: ["tenant", tenantId, "accounts", bcId],
      })
      queryClient.removeQueries({
        queryKey: ["tenant", tenantId, "accounts", bcId],
      })
    }
  }, [tenantId, bcId, queryClient])
  useEffect(() => {
    if (!tenantId) return
    return () => {
      // Every tenant request receives Query's signal; leaving cancels the transport too.
      void queryClient.cancelQueries({ queryKey: ["tenant", tenantId] })
      queryClient.removeQueries({ queryKey: ["tenant", tenantId] })
    }
  }, [tenantId, queryClient])
  const switchTenant = (target: TenantSummary) => {
    if (!target.active || target.id === tenantId) return
    const suffix = tenantId
      ? pathname.slice(`/tenants/${tenantId}`.length)
      : "/builds/new"
    const destination = suffix.startsWith("/strategies/")
      ? "/strategies"
      : suffix === "/members" && !canManage(target.role)
        ? "/builds/new"
        : suffix
    // Router guards run before the old scope unmounts or cancels any requests.
    void navigate({
      to: `/tenants/${target.id}${destination || "/builds/new"}`,
      search: { bc_id: undefined },
    })
  }
  const scope: TenantScope | null = tenant
    ? { tenantId: tenant.id, bcId, role: tenant.role }
    : null
  return (
    <ScopeContext.Provider
      value={{
        scope,
        tenant,
        tenantId,
        user,
        switchTenant,
        bc,
        bcDirectory: bcQuery.data,
        bcPending:
          !!tenant &&
          (bcQuery.isPending ||
            (!!requestedBC &&
              !!bcQuery.data &&
              !initialBC &&
              exactBC.isPending)),
        bcError: bcQuery.error || exactBC.error,
        retryBC: () => {
          void bcQuery.refetch()
          if (requestedBC && !initialBC) void exactBC.refetch()
        },
        switchBC,
        pending: !!tenantId && query.isPending,
        error: query.error,
        forbidden: !!tenantId && !query.isPending && !query.error && !tenant,
        platform: pathname.startsWith("/platform/") || pathname === "/admin",
        retry: () => {
          void query.refetch()
        },
      }}
    >
      {children}
    </ScopeContext.Provider>
  )
}
export function useTenantScope() {
  const value = useContext(ScopeContext)
  if (!value) throw new Error("TenantScopeProvider is required")
  return value
}
export function TenantSelector() {
  const { tenant, switchTenant } = useTenantScope()
  return (
    <DirectoryPicker<TenantSummary>
      label="当前租户"
      valueLabel={tenant?.name}
      queryKey={["my-tenants", "selector"]}
      load={async (search, cursor, limit, signal) =>
        (
          await TenantsService.getMyTenants({
            query: { search, after_id: cursor, active: true, limit },
            signal,
          })
        ).data
      }
      renderItem={(item) => <span>{item.name}</span>}
      onSelect={switchTenant}
    />
  )
}
export function TenantAccessGate({ children }: { children: ReactNode }) {
  const { pending, error, forbidden, retry } = useTenantScope()
  if (pending) return <p role="status">正在读取租户权限…</p>
  if (error) return <RequestError error={error} retry={retry} />
  if (forbidden) return <PermissionPage />
  return children
}
export function PermissionPage() {
  return (
    <Alert variant="destructive">
      <AlertTitle>无权访问此页面</AlertTitle>
      <AlertDescription>
        <p>租户已停用，或当前账号没有所需权限。请联系管理员。</p>
        <Button variant="outline" asChild>
          <Link to="/">返回工作台</Link>
        </Button>
      </AlertDescription>
    </Alert>
  )
}
export function WorkspaceEntry({
  title,
  description,
}: {
  title?: string
  description?: string
}) {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const { user } = useTenantScope()
  const query = useQuery({
    queryKey: ["my-tenants", "entry"],
    enabled: !user.is_superuser,
    queryFn: async ({ signal }) =>
      (
        await TenantsService.getMyTenants({
          query: { active: true, limit: 50 },
          signal,
        })
      ).data,
  })
  if (user.is_superuser) return <Navigate to="/platform/tenants" />
  if (query.isPending) return <p role="status">正在读取可用租户…</p>
  if (query.error)
    return (
      <RequestError
        error={query.error}
        retry={() => {
          void query.refetch()
        }}
      />
    )
  const tenant = query.data?.items[0]
  if (tenant) {
    const destinations = {
      "/accounts": "/tenants/$tenantId/accounts",
      "/members": "/tenants/$tenantId/members",
      "/materials": "/tenants/$tenantId/materials",
      "/build-tasks": "/tenants/$tenantId/build-tasks",
      "/strategies": "/tenants/$tenantId/strategies",
      "/providers": "/tenants/$tenantId/providers",
    } as const
    return (
      <Navigate
        to={
          destinations[pathname as keyof typeof destinations] ??
          "/tenants/$tenantId/builds/new"
        }
        params={{ tenantId: tenant.id }}
      />
    )
  }
  return <WorkspaceEmpty title={title} description={description} />
}
