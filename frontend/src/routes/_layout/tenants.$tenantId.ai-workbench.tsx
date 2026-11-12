import { createFileRoute } from "@tanstack/react-router"
import { AIWorkbenchShell } from "@/features/ai-workbench/AIWorkbenchShell"

type AIWorkbenchSearch = {
  optimizer_id?: string
  view?: "overview" | "schedule" | "execution" | "review" | "pending"
  record_id?: string
  version_id?: string
  return_to?: string
}

export const Route = createFileRoute("/_layout/tenants/$tenantId/ai-workbench")({
  validateSearch: (search: Record<string, unknown>): AIWorkbenchSearch => ({
    optimizer_id: typeof search.optimizer_id === "string" ? search.optimizer_id : undefined,
    view: ["overview", "schedule", "execution", "review", "pending"].includes(String(search.view))
      ? (search.view as AIWorkbenchSearch["view"])
      : undefined,
    record_id: typeof search.record_id === "string" ? search.record_id : undefined,
    version_id: typeof search.version_id === "string" ? search.version_id : undefined,
    return_to: typeof search.return_to === "string" ? search.return_to : undefined,
  }),
  component: Page,
  head: () => ({ meta: [{ title: "AI 优化师工作台 · TK-ADA" }] }),
})

function Page() {
  const { tenantId } = Route.useParams()
  const search = Route.useSearch()
  return (
    <AIWorkbenchShell
      tenantId={tenantId}
      optimizerId={search.optimizer_id}
      view={search.view ?? "overview"}
      recordId={search.record_id}
      versionId={search.version_id}
      returnContext={search.return_to}
    />
  )
}
