import { createFileRoute } from "@tanstack/react-router"
import { TenantPlaceholder } from "@/features/tenants/TenantPlaceholder"
export const Route = createFileRoute("/_layout/tenants/$tenantId/build-tasks")({
  component: () => <TenantPlaceholder title="搭建任务" />,
  head: () => ({ meta: [{ title: "搭建任务 · 短剧投放" }] }),
})
