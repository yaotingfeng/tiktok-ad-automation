import { createFileRoute } from "@tanstack/react-router"
import { TenantPlaceholder } from "@/features/tenants/TenantPlaceholder"
export const Route = createFileRoute("/_layout/tenants/$tenantId/accounts")({
  component: () => <TenantPlaceholder title="账户与授权" />,
  head: () => ({ meta: [{ title: "账户与授权 · 短剧投放" }] }),
})
