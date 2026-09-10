import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/strategies")({
  component: () => (
    <WorkspaceEntry
      title="投放策略"
      description="管理预算、出价、素材分组与创意规则。"
    />
  ),
  head: () => ({ meta: [{ title: "投放策略 · TK-ADA" }] }),
})
