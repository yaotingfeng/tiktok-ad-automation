import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"
export const Route = createFileRoute("/_layout/")({
  component: WorkspaceEntry,
  head: () => ({ meta: [{ title: "工作台 · 短剧投放" }] }),
})
