import { createFileRoute } from "@tanstack/react-router"
import type { StrategyScopeSearch } from "./tenants.$tenantId.strategies"
import { StrategyEditor } from "@/features/strategies/StrategyEditor"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/strategies/new",
)({
  validateSearch: (search: Record<string, unknown>): StrategyScopeSearch & { copy_version_id?: string } => ({
    copy_version_id: typeof search.copy_version_id === "string" ? search.copy_version_id : undefined,
    ai_optimizer_id: typeof search.ai_optimizer_id === "string" ? search.ai_optimizer_id : undefined,
    return_to: typeof search.return_to === "string" ? search.return_to : undefined,
    record_id: typeof search.record_id === "string" ? search.record_id : undefined,
    version_id: typeof search.version_id === "string" ? search.version_id : undefined,
  }),
  component: Page,
})
function Page() {
  const search = Route.useSearch()
  return <StrategyEditor copyVersionId={search.copy_version_id} recordId={search.record_id} versionId={search.version_id} aiOptimizerId={search.ai_optimizer_id} returnContext={search.return_to} readonly={!!search.ai_optimizer_id} />
}
