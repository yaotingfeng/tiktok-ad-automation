import { queryOptions } from "@tanstack/react-query"
import { AutomationRulesService } from "@/client"

export const ruleKeys = (tenantId: string) =>
  ["tenant", tenantId, "automation-rules"] as const

export const catalogQuery = (tenantId: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "catalog"],
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.getCatalog({
          path: { tenant_id: tenantId },
          signal,
        })
      ).data!,
  })

export const rulesQuery = (tenantId: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "list"],
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.listRules({
          path: { tenant_id: tenantId },
          signal,
        })
      ).data!,
  })

export const runsQuery = (tenantId: string, ruleId?: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "runs", ruleId ?? "all"],
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.listRuns({
          path: { tenant_id: tenantId },
          query: { rule_id: ruleId, limit: 100 },
          signal,
        })
      ).data!,
  })

export const runQuery = (tenantId: string, runId: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "run", runId],
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.getRun({
          path: { tenant_id: tenantId, run_id: runId },
          signal,
        })
      ).data!,
  })

export const settingsQuery = (tenantId: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "settings"],
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.getSettings({
          path: { tenant_id: tenantId },
          signal,
        })
      ).data!,
  })
