import { createFileRoute } from "@tanstack/react-router"
import { AdsWorkspace } from "@/features/ads/AdsWorkspace"

export const Route = createFileRoute("/_layout/tenants/$tenantId/ads")({
  validateSearch: (search: Record<string, unknown>) => ({
    bc_id: typeof search.bc_id === "string" ? search.bc_id : undefined,
  }),
  component: AdsWorkspace,
  head: () => ({ meta: [{ title: "广告报表 · TK-ADA" }] }),
})
