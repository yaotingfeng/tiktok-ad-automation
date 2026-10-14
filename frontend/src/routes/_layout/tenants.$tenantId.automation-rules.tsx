import { createFileRoute } from "@tanstack/react-router"
import { AutomationRulesPage } from "@/features/automation-rules/AutomationRulesPage"

export type AutomationRulesSearch = {
  tab?: "rules" | "runs" | "confirmations"
  run?: string
  confirmation?: string
}

const TABS = ["runs", "confirmations"] as const

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/automation-rules",
)({
  // 飞书卡片链接带 tab=runs&run=<运行 ID> 或 tab=confirmations&confirmation=<批次 ID>，
  // 直接打开对应运行明细或待确认批次。
  validateSearch: (search: Record<string, unknown>): AutomationRulesSearch => ({
    tab: TABS.find((tab) => tab === search.tab),
    run: typeof search.run === "string" ? search.run : undefined,
    confirmation:
      typeof search.confirmation === "string" ? search.confirmation : undefined,
  }),
  component: AutomationRulesPage,
  head: () => ({ meta: [{ title: "自动规则 · TK-ADA" }] }),
})
