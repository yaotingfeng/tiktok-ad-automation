import { queryOptions } from "@tanstack/react-query"
import { StrategiesService } from "@/client"
export const DEFAULT_COPY_POOL = "02a4e656-a330-40dc-864c-26e81961f3ca"
export const strategyKey = (tenantId: string) =>
  ["tenant", tenantId, "strategies"] as const
export const strategyQuery = (tenantId: string, id: string) =>
  queryOptions({
    queryKey: [...strategyKey(tenantId), "record", id],
    queryFn: async ({ signal }) =>
      (
        await StrategiesService.getOne({
          path: { tenant_id: tenantId, strategy_id: id },
          signal,
        })
      ).data!,
  })
export const versionQuery = (tenantId: string, id: string) =>
  queryOptions({
    queryKey: [...strategyKey(tenantId), "version", id],
    queryFn: async ({ signal }) =>
      (
        await StrategiesService.version({
          path: { tenant_id: tenantId, version_id: id },
          signal,
        })
      ).data!,
  })
export const copyPoolQuery = (tenantId: string, id: string) =>
  queryOptions({
    queryKey: [...strategyKey(tenantId), "copy-pool", id],
    queryFn: async ({ signal }) =>
      (
        await StrategiesService.copyPool({
          path: { tenant_id: tenantId, version_id: id },
          signal,
        })
      ).data!,
  })
