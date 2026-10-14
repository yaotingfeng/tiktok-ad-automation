import {
  createFileRoute,
  Outlet,
  redirect,
  useRouterState,
} from "@tanstack/react-router"
import { workspaceNavigation } from "@/components/Sidebar/navigation"
import { BCSelector } from "@/features/accounts/BCSelector"
import { canManage, roleLabels } from "@/features/tenants/shared"
import {
  TenantAccessGate,
  TenantScopeProvider,
  TenantSelector,
  useTenantScope,
} from "@/features/tenants/TenantScope"
import { ApiFeedback } from "@/features/workspace/ApiFeedback"
import { WorkspaceShell } from "@/features/workspace/WorkspaceShell"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { rememberLoginReturn } from "@/lib/login-return"

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: ({ location }) => {
    if (!isLoggedIn()) {
      rememberLoginReturn(location.pathname + location.searchStr)
      throw redirect({ to: "/login" })
    }
  },
})
function Layout() {
  const { user, error, isPending } = useAuth()
  if (isPending || error || !user)
    return (
      <WorkspaceShell user={user}>
        <ApiFeedback />
        <p role="status">
          {error
            ? "暂时无法读取用户信息，请刷新页面重试。"
            : "正在载入 TK-ADA…"}
        </p>
      </WorkspaceShell>
    )
  return (
    <TenantScopeProvider user={user}>
      <ScopedLayout />
    </TenantScopeProvider>
  )
}
function ScopedLayout() {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const { scope, tenant, tenantId, user, platform, bc } = useTenantScope()
  const prefix = `/tenants/${tenantId}`
  // 平台视图不展示租户菜单；未接入租户时交给侧栏展示全局入口。
  const groups = tenantId
    ? workspaceNavigation({ prefix, manage: canManage(scope?.role) })
    : platform
      ? []
      : undefined
  return (
    <WorkspaceShell
      user={user}
      platform={platform}
      context={
        tenant && scope
          ? {
              tenant: { id: tenant.id, name: tenant.name },
              bc: bc ? { id: bc.bc_id, name: bc.name } : null,
              roleLabel: roleLabels[scope.role],
              managedByPlatform: scope.role === "platform_admin",
            }
          : null
      }
      contextSelector={tenantId ? <TenantSelector /> : undefined}
      bcSelector={tenant ? <BCSelector key={tenantId} /> : undefined}
      groups={groups}
    >
      <ApiFeedback />
      <TenantAccessGate>
        {/* 素材目录只随租户切换；上传管理器单独保存批次 BC。 */}
        <Outlet
          key={`${tenantId ?? "global"}:${pathname.endsWith("/materials") ? "materials" : (scope?.bcId ?? "")}`}
        />
      </TenantAccessGate>
    </WorkspaceShell>
  )
}
