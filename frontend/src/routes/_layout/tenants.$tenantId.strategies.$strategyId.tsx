import { createFileRoute } from "@tanstack/react-router"
import { StrategyEditor } from "@/features/strategies/StrategyEditor"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/strategies/$strategyId",
)({
  validateSearch: (
    search: Record<string, unknown>,
  ): { version_id?: string; readonly?: boolean } => ({
    version_id:
      typeof search.version_id === "string" &&
      /^[a-f0-9-]{36}$/i.test(search.version_id)
        ? search.version_id
        : undefined,
    readonly:
      search.readonly === true || search.readonly === "true" ? true : undefined,
  }),
  component: Page,
})
function Page() {
  const { strategyId } = Route.useParams()
  const search = Route.useSearch()
  return (
    <StrategyEditor
      strategyId={strategyId}
      versionId={search.version_id}
      readonly={search.readonly}
    />
  )
}
