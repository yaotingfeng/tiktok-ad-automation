import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import { Loader2, type LucideIcon } from "lucide-react"

import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"

export type Item = {
  icon: LucideIcon
  title: string
  path?: string
  onClick?: () => void
  disabled?: boolean
  pendingLabel?: string
}

export type NavGroup = {
  label: string
  items: Item[]
}

interface MainProps {
  label?: string
  items: Item[]
}

export function Main({ items, label }: MainProps) {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const currentPath = router.location.pathname

  const handleMenuClick = () => {
    if (isMobile) {
      setOpenMobile(false)
    }
  }

  return (
    <SidebarGroup>
      {label && <SidebarGroupLabel>{label}</SidebarGroupLabel>}
      <SidebarGroupContent role="navigation" aria-label={label ?? "平台入口"}>
        <SidebarMenu>
          {items.map((item) => {
            // 列表页的详情、新建等子路由也高亮所属菜单项。
            const isActive =
              !!item.path &&
              (currentPath === item.path ||
                currentPath.startsWith(`${item.path}/`) ||
                (item.path.endsWith("/builds/new") &&
                  /\/build-(drafts|previews)\//.test(currentPath)))

            return (
              <SidebarMenuItem key={item.title}>
                <SidebarMenuButton
                  tooltip={item.title}
                  isActive={isActive}
                  aria-current={isActive ? "page" : undefined}
                  asChild={!!item.path}
                  disabled={item.disabled}
                  aria-label={item.pendingLabel ? item.title : undefined}
                  aria-busy={item.pendingLabel ? true : undefined}
                  onClick={
                    item.path
                      ? undefined
                      : () => {
                          handleMenuClick()
                          item.onClick?.()
                        }
                  }
                >
                  {item.path ? (
                    <RouterLink to={item.path} onClick={handleMenuClick}>
                      <item.icon />
                      <span>{item.title}</span>
                    </RouterLink>
                  ) : (
                    <>
                      {item.pendingLabel ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <item.icon />
                      )}
                      <span>{item.pendingLabel ?? item.title}</span>
                    </>
                  )}
                </SidebarMenuButton>
              </SidebarMenuItem>
            )
          })}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}
