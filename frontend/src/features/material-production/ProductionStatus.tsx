import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { MaterialProductionService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { RequestError } from "@/features/tenants/shared"

const labels = {
  not_configured: "未开通",
  pending: "开通中",
  ready: "已开通",
  failed: "开通失败",
}

export function ProductionStatus({ tenantId }: { tenantId: string }) {
  const queryClient = useQueryClient()
  const key = ["tenant", tenantId, "material-production"]
  const query = useQuery({
    queryKey: key,
    queryFn: async ({ signal }) =>
      (
        await MaterialProductionService.productionGetMaterialProduction({
          path: { tenant_id: tenantId },
          signal,
        })
      ).data,
    refetchInterval: (query) =>
      query.state.data?.status === "pending" ? 5000 : false,
  })
  const retry = useMutation({
    mutationFn: () =>
      MaterialProductionService.productionRetryMaterialProduction({
        path: { tenant_id: tenantId },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: key }),
  })
  return (
    <section className="flex flex-col gap-3" aria-label="素材制作开通状态">
      <h3 className="font-medium">素材制作</h3>
      {query.isPending ? (
        <p role="status">正在读取开通状态…</p>
      ) : query.error ? (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      ) : query.data ? (
        <>
          <Badge
            variant={
              query.data.status === "failed" ? "destructive" : "secondary"
            }
          >
            {labels[query.data.status]}
          </Badge>
          {query.data.username && (
            <p className="text-sm">素材账号：{query.data.username}</p>
          )}
          {query.data.message && (
            <p className="text-sm text-muted-foreground">
              {query.data.message}
            </p>
          )}
          {query.data.status === "failed" && (
            <Button
              type="button"
              variant="outline"
              disabled={retry.isPending}
              onClick={() => retry.mutate()}
            >
              {retry.isPending ? "正在重试…" : "重试开通"}
            </Button>
          )}
          {retry.error && <RequestError error={retry.error} />}
        </>
      ) : null}
    </section>
  )
}
