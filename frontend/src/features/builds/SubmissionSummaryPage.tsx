import { useQuery } from "@tanstack/react-query"
import { Link, useRouterState } from "@tanstack/react-router"
import { BuildsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { buildKey } from "./api"

const statuses: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "执行中",
  NEEDS_REVIEW: "结果待核实",
  COMPLETED: "执行已完成",
  PARTIAL: "部分完成",
  FAILED: "执行失败",
}
export function SubmissionSummaryPage() {
  const { tenantId, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname }),
    id = /\/build-tasks\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !id)
    return (
      <WorkspaceEmpty
        title="请先选择有效的 BC"
        description="搭建任务属于提交时的租户与 BC。"
      />
    )
  return (
    <Summary
      key={`${tenantId}:${scope.bcId}:${id}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      id={id}
    />
  )
}
function Summary({
  tenantId,
  bcId,
  id,
}: {
  tenantId: string
  bcId: string
  id: string
}) {
  const query = useQuery({
    queryKey: [...buildKey(tenantId, bcId), "submission", id],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmission({
          path: { tenant_id: tenantId, submission_id: id },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      ["QUEUED", "RUNNING"].includes(q.state.data?.status || "") ? 2000 : false,
  })
  const data = query.data
  if (query.isPending) return <p role="status">正在读取真实搭建任务…</p>
  if (query.error && !data)
    return (
      <RequestError error={query.error} retry={() => void query.refetch()} />
    )
  if (!data || data.bc_id !== bcId)
    return (
      <WorkspaceEmpty
        title="当前 BC 与任务不一致"
        description="请切换到提交时的 BC 查看任务。"
      />
    )
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">搭建任务</h1>
          <p className="mt-2 break-all font-mono text-xs text-muted-foreground">
            {data.submission_id}
          </p>
        </div>
        <Button variant="outline" onClick={() => void query.refetch()}>
          刷新任务结果
        </Button>
      </div>
      {query.error && (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      )}
      <Alert>
        <AlertDescription>
          <p role="status">{statuses[data.status] || data.status}</p>
          <p>
            已受理的任务由后台继续执行。创建、启用操作与审核/实际投放状态分开核实，此处仅展示当前任务统计。
          </p>
        </AlertDescription>
      </Alert>
      <Card className="py-4">
        <CardContent className="px-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>范围 / 结果</TableHead>{" "}
                <TableHead>Campaign</TableHead>
                <TableHead>Ad Group</TableHead>
                <TableHead>Ad</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(
                [
                  ["planned", "预览计划"],
                  ["submitted", "本次提交"],
                  ["succeeded", "已成功"],
                  ["failed", "已失败"],
                  ["excluded", "已排除"],
                  ["unknown", "结果待核实"],
                  ["pending", "待执行"],
                ] as const
              ).map(([key, label]) => (
                <TableRow key={key}>
                  <TableCell>{label}</TableCell>
                  <TableCell>{data[key].campaign_count}</TableCell>
                  <TableCell>{data[key].adgroup_count}</TableCell>
                  <TableCell>{data[key].ad_count}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-sm">
        本次提交配置日预算合计 {data.currency} {data.daily_budget_sum}
        ，非预计实际消耗。
        {!data.expanded && "后台正在展开执行范围，统计将继续更新。"}
      </p>
      <Button variant="outline" asChild>
        <Link
          to="/tenants/$tenantId/build-previews/$previewId"
          params={{ tenantId, previewId: data.preview_id }}
          search={{ bc_id: bcId }}
        >
          查看冻结预览
        </Link>
      </Button>
    </div>
  )
}
