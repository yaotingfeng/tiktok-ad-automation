import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"

export const Route = createFileRoute("/_layout/materials")({
  component: () => (
    <WorkspaceEmpty
      title="素材库"
      description="集中管理素材及其实际上传账户。"
    />
  ),
  head: () => ({ meta: [{ title: "素材库 · 短剧投放" }] }),
})
