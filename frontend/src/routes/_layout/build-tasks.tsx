import { createFileRoute } from "@tanstack/react-router"
import { WorkspaceEntry } from "@/features/tenants/TenantScope"

export const Route = createFileRoute("/_layout/build-tasks")({
  component: () => (
    <WorkspaceEntry
      title="搭建任务"
      description="查看广告创建进度、实际结果与待处理异常。"
    />
  ),
  head: () => ({ meta: [{ title: "搭建任务 · TT ADA" }] }),
})
