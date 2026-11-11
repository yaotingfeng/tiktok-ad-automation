import { createFileRoute } from "@tanstack/react-router"
import { AIWorkbenchPage } from "@/features/ai-workbench/AIWorkbenchPage"

export const Route = createFileRoute("/_layout/tenants/$tenantId/ai-workbench")({
  validateSearch: (search: Record<string, unknown>) => ({
    optimizer_id: typeof search.optimizer_id === "string" ? search.optimizer_id : "",
  }),
  component: Page,
  head: () => ({ meta: [{ title: "AI 优化师 · TK-ADA" }] }),
})

function Page() {
  const { tenantId } = Route.useParams()
  const { optimizer_id: optimizerId } = Route.useSearch()
  return <AIWorkbenchPage tenantId={tenantId} optimizerId={optimizerId} />
}

