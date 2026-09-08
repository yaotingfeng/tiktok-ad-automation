import { createFileRoute } from "@tanstack/react-router"
import { StrategyEditor } from "@/features/strategies/StrategyEditor"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/strategies/new",
)({
  validateSearch: (
    search: Record<string, unknown>,
  ): { copy_version_id?: string } => ({
    copy_version_id:
      typeof search.copy_version_id === "string" &&
      /^[a-f0-9-]{36}$/i.test(search.copy_version_id)
        ? search.copy_version_id
        : undefined,
  }),
  component: Page,
})
function Page() {
  const search = Route.useSearch()
  return <StrategyEditor copyVersionId={search.copy_version_id} />
}
