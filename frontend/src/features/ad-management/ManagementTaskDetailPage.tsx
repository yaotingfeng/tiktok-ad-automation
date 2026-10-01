import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router"
import { useState } from "react"
import { AdManagementService, type ManagementPreviewPublic } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { isForbidden } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { statusLabel } from "./ManagementTasksPage"

export function ManagementTaskDetailPage() {
  const { tenantId, scope } = useTenantScope()
  const { taskId } = useParams({ strict: false }) as { taskId: string }
  const search = useSearch({ strict: false }) as { bc_id?: string }
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [notice, setNotice] = useState<string | null>(null)
  const [restorePreview, setRestorePreview] =
    useState<ManagementPreviewPublic | null>(null)
  const query = useQuery({
    queryKey: ["tenant", tenantId, "ad-management-task", taskId],
    enabled: !!tenantId && !!taskId,
    queryFn: async ({ signal }) =>
      (
        await AdManagementService.getTask({
          path: { tenant_id: tenantId!, task_id: taskId },
          signal,
        })
      ).data,
    refetchInterval: (current) =>
      current.state.data?.status === "RUNNING" ||
      current.state.data?.status === "QUEUED"
        ? 5000
        : false,
  })
  const write = scope?.role !== "viewer"
  const action = useMutation({
    mutationFn: async (
      kind: "cancel" | "retry" | "restore" | "reconcile" | "refresh",
    ) => {
      if (kind === "cancel")
        return (
          await AdManagementService.cancelTask({
            path: { tenant_id: tenantId!, task_id: taskId },
          })
        ).data
      if (kind === "retry")
        return (
          await AdManagementService.retryTask({
            path: { tenant_id: tenantId!, task_id: taskId },
            body: { idempotency_key: crypto.randomUUID() },
          })
        ).data
      if (kind === "restore")
        return (
          await AdManagementService.prepareRestore({
            path: { tenant_id: tenantId!, task_id: taskId },
          })
        ).data
      if (kind === "refresh")
        return (
          await AdManagementService.retryTargetedRefresh({
            path: { tenant_id: tenantId!, task_id: taskId },
          })
        ).data
      return (
        await AdManagementService.reconcileTask({
          path: { tenant_id: tenantId!, task_id: taskId },
        })
      ).data
    },
    onSuccess: (result, kind) => {
      if (kind === "restore") {
        setRestorePreview(result as ManagementPreviewPublic)
        setNotice("已生成新的恢复预览；确认前不会写入平台。")
      } else
        setNotice(
          kind === "reconcile"
            ? "核查已完成，未知项不会自动重发。"
            : kind === "refresh"
              ? "已排队重试定向刷新，不会重发管理变更。"
              : "任务状态已更新。",
        )
      void queryClient.invalidateQueries({
        queryKey: ["tenant", tenantId, "ad-management-task", taskId],
      })
      if (kind === "retry" && "task_id" in result)
        void navigate({
          to: "/tenants/$tenantId/ad-management-tasks/$taskId",
          params: { tenantId: tenantId!, taskId: result.task_id },
          search: { bc_id: search.bc_id },
        })
    },
  })
  const submitRestore = useMutation({
    mutationFn: async () => {
      if (!restorePreview) throw new Error("恢复预览尚未生成")
      return (
        await AdManagementService.submitTask({
          path: { tenant_id: tenantId! },
          body: {
            preview_id: restorePreview.preview_id,
            preview_digest: restorePreview.digest,
            idempotency_key: crypto.randomUUID(),
          },
        })
      ).data
    },
    onSuccess: (result) => {
      setRestorePreview(null)
      void navigate({
        to: "/tenants/$tenantId/ad-management-tasks/$taskId",
        params: { tenantId: tenantId!, taskId: result.task_id },
        search: { bc_id: search.bc_id },
      })
    },
  })
  const task = query.data
  const counts = task?.counts
  const unknown =
    (counts as Record<string, number | undefined> | undefined)?.unknown ?? 0
  const refreshPending =
    (counts as Record<string, number | undefined> | undefined)
      ?.refresh_pending ?? 0
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <WorkspacePageTitle>管理任务详情</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            任务 {taskId} · BC：{task?.bc_id ?? search.bc_id ?? "—"}
          </p>
        </div>
        <Link
          className="text-sm underline"
          to="/tenants/$tenantId/ad-management-tasks"
          params={{ tenantId: tenantId! }}
          search={{ bc_id: task?.bc_id ?? search.bc_id }}
        >
          返回任务列表
        </Link>
      </div>
      {query.error && (
        <Alert variant="destructive">
          <AlertTitle>
            {isForbidden(query.error) ? "无权查看任务" : "任务读取失败"}
          </AlertTitle>
          <AlertDescription>请刷新后重试。</AlertDescription>
        </Alert>
      )}
      {notice && (
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      {restorePreview && (
        <Card>
          <CardHeader>
            <CardTitle>恢复预览</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p>恢复预览仅在确认提交后进入任务队列，不会直接写入平台。</p>
            <p className="text-muted-foreground">
              BC：{restorePreview.bc_id} · 五分钟内有效
            </p>
            <p>
              目标{" "}
              {restorePreview.counts?.targets ??
                restorePreview.items?.length ??
                0}{" "}
              · 冲突{" "}
              {restorePreview.items?.filter(
                (item) => item.execution_result === "CONFLICT",
              ).length ?? 0}
            </p>
            <Button
              onClick={() => submitRestore.mutate()}
              disabled={submitRestore.isPending || !write}
            >
              {submitRestore.isPending ? "提交中…" : "提交恢复任务"}
            </Button>
          </CardContent>
        </Card>
      )}
      {task && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>任务状态</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap items-center gap-4">
              <Badge variant="outline">
                {statusLabel[task.status] ?? task.status}
              </Badge>
              <span>目标 {counts?.targets ?? 0}</span>
              <span>不可用 {counts?.unsupported ?? 0}</span>
              <span>未知 {unknown}</span>
              {refreshPending > 0 && (
                <span>同步刷新待处理 {refreshPending}</span>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>操作</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              {write && ["QUEUED", "RUNNING"].includes(task.status) && (
                <Button
                  variant="outline"
                  onClick={() => action.mutate("cancel")}
                  disabled={action.isPending}
                >
                  取消未发送项
                </Button>
              )}
              {write && task.status === "PARTIAL" && unknown === 0 && (
                <Button
                  variant="outline"
                  onClick={() => action.mutate("retry")}
                  disabled={action.isPending}
                >
                  重试未发送项
                </Button>
              )}
              {write && refreshPending > 0 && (
                <Button
                  variant="outline"
                  onClick={() => action.mutate("refresh")}
                  disabled={action.isPending}
                >
                  刷新重试（{refreshPending}）
                </Button>
              )}
              {write && (
                <Button
                  variant="outline"
                  onClick={() => action.mutate("restore")}
                  disabled={action.isPending}
                >
                  生成恢复预览
                </Button>
              )}
              <Button
                variant="outline"
                onClick={() => action.mutate("reconcile")}
                disabled={action.isPending}
              >
                核查本地状态
              </Button>
              {unknown > 0 && (
                <p className="basis-full text-sm text-amber-700">
                  存在 UNKNOWN 项，仅可核查，不能重试未知变更。
                </p>
              )}
              {!write && (
                <p className="basis-full text-sm text-muted-foreground">
                  当前为只读成员，隐藏取消、重试和恢复入口。
                </p>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>逐项回执</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="max-h-80 overflow-auto rounded-md border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left">
                      <th className="p-2">目标</th>
                      <th className="p-2">执行</th>
                      <th className="p-2">交付</th>
                      <th className="p-2">观察</th>
                      <th className="p-2">请求</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(task.items ?? []).map((item) => (
                      <tr
                        key={`${item.ref.kind}:${item.ref.remote_id}`}
                        className="border-b"
                      >
                        <td
                          className="max-w-48 truncate p-2"
                          title={item.ref.remote_id}
                        >
                          {item.ref.kind} · {item.ref.remote_id}
                        </td>
                        <td className="p-2">{item.execution_result ?? "—"}</td>
                        <td className="p-2">{item.delivery_status ?? "—"}</td>
                        <td className="p-2">{item.observation_state ?? "—"}</td>
                        <td className="p-2">
                          {item.attempts?.length
                            ? item.attempts
                                .map(
                                  (attempt) =>
                                    `${attempt.attempt}:${attempt.outcome}`,
                                )
                                .join(", ")
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
