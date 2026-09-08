import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/providers")({
  component: () => (
    <WorkspaceEntry
      title="版权方连接"
      description="管理租户独立的版权方连接与应用配置。"
    />
  ),
  head: () => ({ meta: [{ title: "版权方连接 · 短剧投放" }] }),
})
