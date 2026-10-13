import { createFileRoute } from "@tanstack/react-router"
import { AutomationRulesPage } from "@/features/automation-rules/AutomationRulesPage"

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/automation-rules",
)({
  component: AutomationRulesPage,
  head: () => ({ meta: [{ title: "自动规则 · TK-ADA" }] }),
})
