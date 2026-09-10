import { createFileRoute } from "@tanstack/react-router"
import { ProvidersPage } from "@/features/providers/ProvidersPage"
export const Route = createFileRoute("/_layout/tenants/$tenantId/providers")({
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    tab: "connections" | "links"
    connection_id?: string
    task_id?: string
  } => ({
    tab: search.tab === "links" ? "links" : "connections",
    connection_id:
      typeof search.connection_id === "string" &&
      /^[a-f0-9-]{36}$/i.test(search.connection_id)
        ? search.connection_id
        : undefined,
    task_id:
      typeof search.task_id === "string" &&
      /^[a-f0-9-]{36}$/i.test(search.task_id)
        ? search.task_id
        : undefined,
  }),
  component: ProvidersPage,
  head: () => ({ meta: [{ title: "版权方连接 · TK-ADA" }] }),
})
