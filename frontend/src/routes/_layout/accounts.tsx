import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/accounts")({
  component: () => (
    <WorkspaceEntry
      title="账户与授权"
      description="管理当前租户的 TikTok BC 与广告账户授权。"
    />
  ),
  head: () => ({ meta: [{ title: "账户与授权 · TT ADA" }] }),
})
