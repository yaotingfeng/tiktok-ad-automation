import { createFileRoute } from "@tanstack/react-router"
import { BuildWorkspace } from "@/features/builds/BuildWorkspace"
export const Route = createFileRoute("/_layout/tenants/$tenantId/builds/new")({
  validateSearch: (search: Record<string, unknown>): { view?: "drafts" } => ({
    view: search.view === "drafts" ? "drafts" : undefined,
  }),
  component: BuildWorkspace,
  head: () => ({ meta: [{ title: "广告搭建 · TK-ADA" }] }),
})
