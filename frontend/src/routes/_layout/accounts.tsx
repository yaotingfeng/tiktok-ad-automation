import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"

export const Route = createFileRoute("/_layout/accounts")({
  component: () => (
    <WorkspaceEmpty
      title="账户与授权"
      description="管理当前租户的 TikTok BC 与广告账户授权。"
    />
  ),
  head: () => ({ meta: [{ title: "账户与授权 · 短剧投放" }] }),
})
