import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/materials")({
  component: () => (
    <WorkspaceEntry
      title="素材库"
      description="集中管理素材及其实际上传账户。"
    />
  ),
  head: () => ({ meta: [{ title: "素材库 · TK-ADA" }] }),
})
