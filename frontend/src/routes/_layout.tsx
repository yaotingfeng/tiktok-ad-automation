import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"
import {
  Clapperboard,
  FileVideo,
  Link2,
  ListTodo,
  Monitor,
  SlidersHorizontal,
  Users,
} from "lucide-react"
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

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: () => {
    if (!isLoggedIn()) throw redirect({ to: "/login" })
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
            : "正在载入工作空间…"}
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
  const { scope, tenant, tenantId, user, platform } = useTenantScope()
  const prefix = `/tenants/${tenantId}`
  const workItems = tenantId
    ? [
        { icon: Clapperboard, title: "广告搭建", path: `${prefix}/builds/new` },
        { icon: ListTodo, title: "搭建任务", path: `${prefix}/build-tasks` },
        { icon: FileVideo, title: "素材库", path: `${prefix}/materials` },
        {
          icon: SlidersHorizontal,
          title: "投放策略",
          path: `${prefix}/strategies`,
        },
      ]
    : undefined
  const managementItems = tenantId
    ? [
        { icon: Monitor, title: "账户与授权", path: `${prefix}/accounts` },
        { icon: Link2, title: "版权方连接", path: `${prefix}/providers` },
        ...(canManage(scope?.role)
          ? [{ icon: Users, title: "成员管理", path: `${prefix}/members` }]
          : []),
      ]
    : undefined
  return (
    <WorkspaceShell
      user={user}
      platform={platform}
      context={
        tenant && scope
          ? {
              tenant: { id: tenant.id, name: tenant.name },
              bc: null,
              roleLabel: roleLabels[scope.role],
              managedByPlatform: scope.role === "platform_admin",
            }
          : null
      }
      contextSelector={tenantId ? <TenantSelector /> : undefined}
      workItems={workItems}
      managementItems={managementItems}
    >
      <ApiFeedback />
      <TenantAccessGate>
        <Outlet key={`${tenantId ?? "global"}:${scope?.bcId ?? ""}`} />
      </TenantAccessGate>
    </WorkspaceShell>
  )
}
