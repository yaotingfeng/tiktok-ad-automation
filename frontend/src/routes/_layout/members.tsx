import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/members")({
  component: () => (
    <WorkspaceEntry
      title="成员管理"
      description="按角色管理当前租户的成员权限。"
    />
  ),
  head: () => ({ meta: [{ title: "成员管理 · TK-ADA" }] }),
})
