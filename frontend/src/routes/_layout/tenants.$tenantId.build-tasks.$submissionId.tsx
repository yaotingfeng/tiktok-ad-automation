import { createFileRoute } from "@tanstack/react-router"
import { SubmissionDetailPage } from "@/features/builds/SubmissionDetailPage"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/build-tasks/$submissionId",
)({
  component: SubmissionDetailPage,
  validateSearch: (
    s: Record<string, unknown>,
  ): { tab?: string; result?: string } => ({
    tab: ["details", "issues", "excluded", "events"].includes(String(s.tab))
      ? String(s.tab)
      : undefined,
    result:
      typeof s.result === "string" && /^[A-Z_]{1,30}$/.test(s.result)
        ? s.result
        : undefined,
  }),
  head: () => ({ meta: [{ title: "任务详情 · 短剧投放" }] }),
})
