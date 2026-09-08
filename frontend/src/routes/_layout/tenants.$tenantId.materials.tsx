import { createFileRoute } from "@tanstack/react-router"
import { TenantPlaceholder } from "@/features/tenants/TenantPlaceholder"
export const Route = createFileRoute("/_layout/tenants/$tenantId/materials")({
  component: () => <TenantPlaceholder title="素材库" />,
  head: () => ({ meta: [{ title: "素材库 · 短剧投放" }] }),
})
