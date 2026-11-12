import { createFileRoute } from "@tanstack/react-router"
import { StrategyEditor } from "@/features/strategies/StrategyEditor"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/strategies/new",
)({
  validateSearch: (search: Record<string, unknown>) => ({
    copy_version_id: typeof search.copy_version_id === "string" ? search.copy_version_id : undefined,
    ai_optimizer_id: typeof search.ai_optimizer_id === "string" ? search.ai_optimizer_id : undefined,
    return_to: typeof search.return_to === "string" ? search.return_to : undefined,
  }),
  component: Page,
})
function Page() {
  const search = Route.useSearch()
  return <StrategyEditor copyVersionId={search.copy_version_id} returnContext={search.return_to} readonly={!!search.ai_optimizer_id} />
}
