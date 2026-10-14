import { createFileRoute } from "@tanstack/react-router"
import { ManagementTaskDetailPage } from "@/features/ad-management/ManagementTaskDetailPage"

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/ad-management-tasks/$taskId",
)({
  validateSearch: (search: Record<string, unknown>) => ({
    bc_id: typeof search.bc_id === "string" ? search.bc_id : undefined,
  }),
  component: ManagementTaskDetailPage,
  head: () => ({ meta: [{ title: "操作记录详情 · TK-ADA" }] }),
})
