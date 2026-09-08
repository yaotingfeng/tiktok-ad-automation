import { createFileRoute } from "@tanstack/react-router"
import { TenantPlaceholder } from "@/features/tenants/TenantPlaceholder"
export const Route = createFileRoute("/_layout/tenants/$tenantId/builds/new")({
  component: () => <TenantPlaceholder title="广告搭建" />,
  head: () => ({ meta: [{ title: "广告搭建 · 短剧投放" }] }),
})
