import { createFileRoute, Outlet } from "@tanstack/react-router"

export type StrategyScopeSearch = {
  ai_optimizer_id?: string
  record_id?: string
  version_id?: string
  return_to?: string
  scope?: "bound" | "tenant"
  readonly?: boolean
}

export const Route = createFileRoute("/_layout/tenants/$tenantId/strategies")({
  validateSearch: (search: Record<string, unknown>): StrategyScopeSearch => ({
    ai_optimizer_id: typeof search.ai_optimizer_id === "string" ? search.ai_optimizer_id : undefined,
    record_id: typeof search.record_id === "string" ? search.record_id : undefined,
    version_id: typeof search.version_id === "string" ? search.version_id : undefined,
    return_to: typeof search.return_to === "string" ? search.return_to : undefined,
    scope: search.scope === "bound" ? "bound" : search.scope === "tenant" ? "tenant" : undefined,
    readonly: search.readonly === true || search.readonly === "true" ? true : undefined,
  }),
  component: Outlet,
  head: () => ({ meta: [{ title: "投放策略 · TK-ADA" }] }),
})
