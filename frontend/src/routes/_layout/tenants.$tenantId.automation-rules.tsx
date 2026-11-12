import { createFileRoute } from "@tanstack/react-router"
import { AutomationRulesPage } from "@/features/automation-rules/AutomationRulesPage"

export type AutomationRulesSearch = {
  tab?: "rules" | "runs" | "confirmations"
  run?: string
  confirmation?: string
  ai_optimizer_id?: string
  scope?: "bound" | "tenant"
  rule_ids?: string
  record_id?: string
  version_id?: string
  return_to?: string
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
    ai_optimizer_id: typeof search.ai_optimizer_id === "string" ? search.ai_optimizer_id : undefined,
    scope: search.scope === "bound" ? "bound" : search.scope === "tenant" ? "tenant" : undefined,
    rule_ids: typeof search.rule_ids === "string" ? search.rule_ids : undefined,
    record_id: typeof search.record_id === "string" ? search.record_id : undefined,
    version_id: typeof search.version_id === "string" ? search.version_id : undefined,
    return_to: typeof search.return_to === "string" ? search.return_to : undefined,
  }),
  component: AutomationRulesPage,
  head: () => ({ meta: [{ title: "自动规则 · TK-ADA" }] }),
})
