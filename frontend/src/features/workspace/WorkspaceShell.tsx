import { Link } from "@tanstack/react-router"
import { Building2, ShieldCheck } from "lucide-react"
import type { CSSProperties, ReactNode } from "react"
import type { UserPublic } from "@/client"
import AppSidebar from "@/components/Sidebar/AppSidebar"
import type { Item } from "@/components/Sidebar/Main"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"

// P02 supplies the resolved tenant/BC and scoped routes. No example context is used.
export interface WorkspaceContext {
  tenant: { id: string; name: string }
  bc: { id: string; name: string } | null
  roleLabel: string
  managedByPlatform?: boolean
}

export interface WorkspaceShellProps {
  children: ReactNode
  user?: UserPublic | null
  context?: WorkspaceContext | null
  workItems?: Item[]
  managementItems?: Item[]
  contextSelector?: ReactNode
  bcSelector?: ReactNode
  platform?: boolean
}

export function WorkspaceShell({
  children,
  user,
  context,
  workItems,
  managementItems,
  contextSelector,
  bcSelector,
  platform = false,
}: WorkspaceShellProps) {
  return (
    <SidebarProvider style={{ "--sidebar-width": "216px" } as CSSProperties}>
      <a href="#workspace-main" className="skip-link">
        跳转到主要内容
      </a>
      <AppSidebar
        user={user}
        workItems={workItems}
        managementItems={managementItems}
      />
      <SidebarInset>
        <header className="workspace-topbar sticky top-0 z-10 flex min-h-[58px] shrink-0 flex-wrap items-center justify-between gap-3 border-b bg-card px-4 py-3 md:px-7">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <SidebarTrigger aria-label="切换导航" />
            <span className="hidden text-muted-foreground sm:inline">
              {platform ? "平台范围" : "工作空间"}
            </span>
            {contextSelector ?? (
              <span className="flex items-center gap-2">
                <Building2 className="size-4 text-muted-foreground" />
                <strong className="break-all">
                  {platform
                    ? "平台管理"
                    : (context?.tenant.name ?? "未接入租户")}
                </strong>
              </span>
            )}
            {!platform && (
              <>
                <Separator orientation="vertical" className="h-4!" />
                {bcSelector ?? (
                  <span className="text-muted-foreground break-all">
                    {context?.bc?.name ?? "BC 未连接"}
                  </span>
                )}
              </>
            )}
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-3 text-xs">
            <span className="break-all">{user?.full_name || user?.email}</span>
            <Badge variant="secondary">
              <ShieldCheck />
              {context?.roleLabel ??
                (user?.is_superuser ? "平台管理员" : "已登录")}
            </Badge>
            {context?.managedByPlatform && (
              <span className="text-muted-foreground">
                当前代管：{context.tenant.name}
                <Button asChild size="sm" variant="ghost">
                  <Link to="/platform/tenants">返回平台</Link>
                </Button>
              </span>
            )}
          </div>
        </header>
        <main
          id="workspace-main"
          tabIndex={-1}
          className="flex-1 p-4 py-7 outline-none md:p-8"
        >
          <div className="mx-auto flex max-w-[1536px] flex-col gap-6">
            {children}
          </div>
        </main>
      </SidebarInset>
    </SidebarProvider>
  )
}
