import { createFileRoute } from "@tanstack/react-router"
import { AutomationRulesPage } from "@/features/automation-rules/AutomationRulesPage"

export type AutomationRulesSearch = { tab?: "rules" | "runs"; run?: string }

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/automation-rules",
)({
  // 飞书卡片链接带 tab=runs&run=<运行 ID>，直接打开对应运行明细。
  validateSearch: (search: Record<string, unknown>): AutomationRulesSearch => ({
    tab: search.tab === "runs" ? "runs" : undefined,
    run: typeof search.run === "string" ? search.run : undefined,
  }),
  component: AutomationRulesPage,
  head: () => ({ meta: [{ title: "自动规则 · TK-ADA" }] }),
})
