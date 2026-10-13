import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/automation-rules")({
  component: () => (
    <WorkspaceEntry
      title="自动规则"
      description="按条件自动检查投放数据并发送飞书预警。"
    />
  ),
  head: () => ({ meta: [{ title: "自动规则 · TK-ADA" }] }),
})
