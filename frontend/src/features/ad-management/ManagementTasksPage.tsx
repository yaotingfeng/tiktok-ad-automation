import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AdManagementService } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { isForbidden } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"

const statusLabel: Record<string, string> = {
  PREPARING: "准备中",
  READY: "待提交",
  QUEUED: "排队中",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  NEEDS_REVIEW: "需要核查",
  CANCELLED: "已取消",
}

export function ManagementTasksPage() {
  const { tenantId, scope } = useTenantScope()
  const bcId = scope?.bcId
  const query = useQuery({
    queryKey: ["tenant", tenantId, "ad-management-tasks", bcId],
    enabled: !!tenantId && !!bcId,
    queryFn: async ({ signal }) =>
      (
        await AdManagementService.listTasks({
          path: { tenant_id: tenantId! },
          query: { bc_id: bcId!, limit: 50 },
          signal,
        })
      ).data,
    refetchOnWindowFocus: false,
  })
  if (!tenantId || !bcId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={false}
        title="请先选择有效的 BC"
        description="管理任务按当前租户与 BC 展示。"
      />
    )
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <WorkspacePageTitle>管理任务</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            BC：{bcId} · 变更回执与核查记录
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          {query.isFetching ? "刷新中…" : "刷新"}
        </Button>
      </div>
      {query.error && (
        <Alert variant="destructive">
          <AlertTitle>
            {isForbidden(query.error) ? "无权查看管理任务" : "任务读取失败"}
          </AlertTitle>
          <AlertDescription>请检查当前租户和 BC 权限后重试。</AlertDescription>
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle>任务列表</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto p-0">
          <table className="w-full min-w-[680px] text-sm">
            <thead>
              <tr className="border-b text-left">
                <th className="p-3">任务</th>
                <th className="p-3">状态</th>
                <th className="p-3">目标</th>
                <th className="p-3">异常</th>
              </tr>
            </thead>
            <tbody>
              {(query.data?.items ?? []).map((task) => (
                <tr key={task.task_id} className="border-b">
                  <td className="p-3">
                    <Link
                      className="font-medium hover:underline"
                      to="/tenants/$tenantId/ad-management-tasks/$taskId"
                      params={{ tenantId, taskId: task.task_id }}
                      search={{ bc_id: task.bc_id }}
                    >
                      {task.task_id.slice(0, 8)}…
                    </Link>
                  </td>
                  <td className="p-3">
                    <Badge variant="outline">
                      {statusLabel[task.status] ?? task.status}
                    </Badge>
                  </td>
                  <td className="p-3">{task.counts?.targets ?? 0}</td>
                  <td className="p-3">
                    {(
                      task.counts as
                        | Record<string, number | undefined>
                        | undefined
                    )?.unknown ?? 0}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!query.isPending && !(query.data?.items ?? []).length && (
            <p className="p-8 text-center text-sm text-muted-foreground">
              暂无管理任务
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

export { statusLabel }
