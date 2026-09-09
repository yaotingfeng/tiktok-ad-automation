import { createFileRoute } from "@tanstack/react-router"
import { SubmissionListPage } from "@/features/builds/SubmissionListPage"
export const Route = createFileRoute("/_layout/tenants/$tenantId/build-tasks/")(
  {
    component: SubmissionListPage,
    validateSearch: (s: Record<string, unknown>) =>
      Object.fromEntries(
        ["q", "status_group", "range", "from", "to", "provider_connection_id"]
          .filter((k) => typeof s[k] === "string" && String(s[k]).length <= 255)
          .map((k) => [k, s[k]]),
      ),
  },
)
