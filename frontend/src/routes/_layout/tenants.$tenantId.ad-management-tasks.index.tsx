import { createFileRoute } from "@tanstack/react-router"
import { ManagementTasksPage } from "@/features/ad-management/ManagementTasksPage"

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/ad-management-tasks/",
)({
  validateSearch: (search: Record<string, unknown>) => ({
    bc_id: typeof search.bc_id === "string" ? search.bc_id : undefined,
  }),
  component: ManagementTasksPage,
})
