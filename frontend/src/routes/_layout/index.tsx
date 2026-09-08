import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"

export const Route = createFileRoute("/_layout/")({
  component: WorkspaceEmpty,
  head: () => ({ meta: [{ title: "广告搭建 · 短剧投放" }] }),
})
