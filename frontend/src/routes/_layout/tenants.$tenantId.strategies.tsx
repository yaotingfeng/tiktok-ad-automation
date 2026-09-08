import { createFileRoute } from "@tanstack/react-router"
import { TenantPlaceholder } from "@/features/tenants/TenantPlaceholder"
export const Route = createFileRoute("/_layout/tenants/$tenantId/strategies")({
  component: () => <TenantPlaceholder title="投放策略" />,
  head: () => ({ meta: [{ title: "投放策略 · 短剧投放" }] }),
})
