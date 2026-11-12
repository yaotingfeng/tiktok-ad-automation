import { createFileRoute } from "@tanstack/react-router"
import { StrategyEditor } from "@/features/strategies/StrategyEditor"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/strategies/$strategyId",
)({
  validateSearch: (search: Record<string, unknown>) => ({
    version_id: typeof search.version_id === "string" ? search.version_id : undefined,
    ai_optimizer_id: typeof search.ai_optimizer_id === "string" ? search.ai_optimizer_id : undefined,
    record_id: typeof search.record_id === "string" ? search.record_id : undefined,
    return_to: typeof search.return_to === "string" ? search.return_to : undefined,
    readonly: search.readonly === true || search.readonly === "true" ? true : undefined,
    scope: search.scope === "bound" ? "bound" : undefined,
  }),
  component: Page,
})
function Page() {
  const { strategyId } = Route.useParams()
  const search = Route.useSearch()
  return <StrategyEditor strategyId={strategyId} versionId={search.version_id} aiOptimizerId={search.ai_optimizer_id} returnContext={search.return_to} readonly={search.readonly || !!search.ai_optimizer_id} />
}
