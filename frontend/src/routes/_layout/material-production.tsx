import { createFileRoute } from "@tanstack/react-router"
import { MaterialProductionLauncher } from "@/features/material-production/MaterialProductionLauncher"

export const Route = createFileRoute("/_layout/material-production")({
  validateSearch: (search: Record<string, unknown>): { tenant?: string } => ({
    tenant: typeof search.tenant === "string" ? search.tenant : undefined,
  }),
  component: Launcher,
  head: () => ({ meta: [{ title: "打开素材制作 · TK-ADA" }] }),
})

function Launcher() {
  const { tenant } = Route.useSearch()
  return <MaterialProductionLauncher tenantName={tenant ?? ""} />
}
