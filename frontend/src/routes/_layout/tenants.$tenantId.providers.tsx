import { createFileRoute } from "@tanstack/react-router"
import { TenantPlaceholder } from "@/features/tenants/TenantPlaceholder"
export const Route = createFileRoute("/_layout/tenants/$tenantId/providers")({
  component: () => <TenantPlaceholder title="版权方连接" />,
  head: () => ({ meta: [{ title: "版权方连接 · 短剧投放" }] }),
})
