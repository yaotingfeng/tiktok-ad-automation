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

export const confirmationsQuery = (tenantId: string, status?: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "confirmations", status ?? "all"],
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.listConfirmations({
          path: { tenant_id: tenantId },
          // 后端默认只列待确认；传空字符串列出全部状态。
          query: { status: status ?? "" },
          signal,
        })
      ).data!,
  })

export const confirmationQuery = (tenantId: string, confirmationId: string) =>
  queryOptions({
    queryKey: [...ruleKeys(tenantId), "confirmation", confirmationId],
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.getConfirmation({
          path: { tenant_id: tenantId, confirmation_id: confirmationId },
          signal,
        })
      ).data!,
  })
