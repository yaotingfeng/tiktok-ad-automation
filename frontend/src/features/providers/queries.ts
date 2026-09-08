import { queryOptions } from "@tanstack/react-query"
import {
  ProvidersService,
  type providersGetPreparationData,
  type providersListConnectionsData,
} from "@/client"
export const providerKey = (tenantId: string) =>
  ["tenant", tenantId, "providers"] as const
export const connectionsQuery = (
  tenantId: string,
  cursor: string | null,
  limit: number,
  filters: Pick<
    NonNullable<providersListConnectionsData["query"]>,
    "query" | "kind" | "status"
  > = {},
) =>
  queryOptions({
    queryKey: [...providerKey(tenantId), "connections", filters, cursor, limit],
    queryFn: async ({ signal }) =>
      (
        await ProvidersService.listConnections({
          path: { tenant_id: tenantId },
          query: { cursor, limit, ...filters },
          signal,
        })
      ).data,
  })
export const applicationsQuery = (
  tenantId: string,
  connectionId: string,
  cursor: string | null,
  limit: number,
) =>
  queryOptions({
    queryKey: [
      ...providerKey(tenantId),
      "applications",
      connectionId,
      cursor,
      limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await ProvidersService.listApplications({
          path: { tenant_id: tenantId, connection_id: connectionId },
          query: { cursor, limit },
          signal,
        })
      ).data,
  })
export const resultsQuery = (
  tenantId: string,
  taskId: string,
  cursor: string | null,
  limit: number,
  filters: Pick<
    NonNullable<providersGetPreparationData["query"]>,
    "status" | "exceptions_only"
  > = {},
) =>
  queryOptions({
    queryKey: [
      ...providerKey(tenantId),
      "results",
      taskId,
      filters,
      cursor,
      limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await ProvidersService.getPreparation({
          path: { tenant_id: tenantId, task_id: taskId },
          query: { cursor, page_size: limit, ...filters },
          signal,
        })
      ).data,
  })
