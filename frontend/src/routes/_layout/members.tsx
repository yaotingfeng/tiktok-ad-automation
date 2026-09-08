import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"

export const Route = createFileRoute("/_layout/members")({
  component: () => (
    <WorkspaceEmpty
      title="成员管理"
      description="按角色管理当前租户的成员权限。"
    />
  ),
  head: () => ({ meta: [{ title: "成员管理 · 短剧投放" }] }),
})
