import { FlaskConical, LayoutGrid } from "lucide-react"
import type { UserPublic } from "@/client"
import { WorkspaceBrand } from "@/components/Common/WorkspaceBrand"
import { Separator } from "@/components/ui/separator"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { Main, type NavGroup } from "./Main"
import { workspaceNavigation } from "./navigation"
import { User } from "./User"

export function AppSidebar({
  user,
  groups,
}: {
  user?: UserPublic | null
  groups?: NavGroup[]
}) {
  // 未传入分组时（未接入租户或用户信息加载中）展示全局入口。
  const navigation =
    groups ?? workspaceNavigation({ manage: !!user?.is_superuser })
  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader>
        <WorkspaceBrand />
      </SidebarHeader>
      <SidebarContent>
        {navigation
          .filter((group) => group.items.length > 0)
          .map((group) => (
            <Main key={group.label} label={group.label} items={group.items} />
          ))}
      </SidebarContent>
      <SidebarFooter>
        {/* workspaceNavigation 将 AI 独立分组放到这里之前，保证它紧邻体验版入口。 */}
        <SidebarGroup data-testid="experience-entry-group">
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton tooltip="打开体验版" data-testid="experience-entry" asChild>
                  <a href="/demo/" target="_blank" rel="noopener noreferrer">
                    <FlaskConical />
                    <span>打开体验版</span>
                  </a>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        {user?.is_superuser && (
          <Main
            items={[
              {
                icon: LayoutGrid,
                title: "平台管理",
                path: "/platform/tenants",
              },
            ]}
          />
        )}
        <Separator />
        <User user={user} />
      </SidebarFooter>
    </Sidebar>
  )
}
export default AppSidebar
