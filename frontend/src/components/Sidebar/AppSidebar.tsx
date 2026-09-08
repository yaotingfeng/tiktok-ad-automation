import {
  Clapperboard,
  FileVideo,
  LayoutGrid,
  Link2,
  ListTodo,
  Monitor,
  SlidersHorizontal,
  Users,
} from "lucide-react"
import type { UserPublic } from "@/client"
import { WorkspaceBrand } from "@/components/Common/WorkspaceBrand"
import { Separator } from "@/components/ui/separator"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
} from "@/components/ui/sidebar"
import { type Item, Main } from "./Main"
import { User } from "./User"

const defaultWorkItems: Item[] = [
  { icon: Clapperboard, title: "广告搭建", path: "/" },
  { icon: ListTodo, title: "搭建任务", path: "/build-tasks" },
  { icon: FileVideo, title: "素材库", path: "/materials" },
  { icon: SlidersHorizontal, title: "投放策略", path: "/strategies" },
]
const defaultManagementItems: Item[] = [
  { icon: Monitor, title: "账户与授权", path: "/accounts" },
  { icon: Link2, title: "版权方连接", path: "/providers" },
  { icon: Users, title: "成员管理", path: "/members" },
]

export function AppSidebar({
  user,
  workItems = defaultWorkItems,
  managementItems = defaultManagementItems,
}: {
  user?: UserPublic | null
  workItems?: Item[]
  managementItems?: Item[]
}) {
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-6 py-6 group-data-[collapsible=icon]:px-2">
        <WorkspaceBrand />
      </SidebarHeader>
      <SidebarContent className="gap-5 px-2">
        <Main label="投放工作" items={workItems} />
        <Main label="租户管理" items={managementItems} />
      </SidebarContent>
      <SidebarFooter className="gap-3 px-2 pb-3">
        {user?.is_superuser && (
          <Main
            items={[{ icon: LayoutGrid, title: "平台管理", path: "/admin" }]}
          />
        )}
        <Separator />
        <User user={user} />
      </SidebarFooter>
    </Sidebar>
  )
}
export default AppSidebar
