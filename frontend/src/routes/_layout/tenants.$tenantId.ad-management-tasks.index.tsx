import { createFileRoute } from "@tanstack/react-router"
import { ManagementTasksPage } from "@/features/ad-management/ManagementTasksPage"

const ACTIONS = ["enable", "disable", "budget", "roas"] as const
const SOURCES = ["manual", "rule"] as const

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/ad-management-tasks/",
)({
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    bc_id?: string
    action?: (typeof ACTIONS)[number]
    source?: (typeof SOURCES)[number]
  } => ({
    bc_id: typeof search.bc_id === "string" ? search.bc_id : undefined,
    // 操作记录筛选写入地址，刷新或分享链接时保留。
    action: ACTIONS.find((value) => value === search.action),
    source: SOURCES.find((value) => value === search.source),
  }),
  component: ManagementTasksPage,
})
