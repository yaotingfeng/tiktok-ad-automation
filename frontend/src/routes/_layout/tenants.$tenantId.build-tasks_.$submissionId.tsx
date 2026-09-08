import { createFileRoute } from "@tanstack/react-router"
import { SubmissionSummaryPage } from "@/features/builds/SubmissionSummaryPage"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/build-tasks_/$submissionId",
)({
  component: SubmissionSummaryPage,
  head: () => ({ meta: [{ title: "搭建任务 · 短剧投放" }] }),
})
