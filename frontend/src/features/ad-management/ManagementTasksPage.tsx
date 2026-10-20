import { useQuery } from "@tanstack/react-query"
import { getRouteApi, Link } from "@tanstack/react-router"
import { AdManagementService } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { isForbidden, Pager, useCursorPage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import {
  displayTime,
  mutationText,
  resultParts,
  sourceText,
  statusLabel,
  targetText,
} from "./presentation"

const route = getRouteApi("/_layout/tenants/$tenantId/ad-management-tasks/")
const ALL = "__all__"
const ACTION_OPTIONS = [
  { value: "enable", label: "启用" },
  { value: "disable", label: "暂停" },
  { value: "budget", label: "预算" },
  { value: "roas", label: "目标 ROAS" },
  { value: "rename", label: "改名" },
] as const
const SOURCE_OPTIONS = [
  { value: "manual", label: "人工操作" },
  { value: "rule", label: "自动规则" },
] as const

export function ManagementTasksPage() {
  const { tenantId, scope } = useTenantScope()
  const bcId = scope?.bcId
  const { action, source } = route.useSearch()
  if (!tenantId || !bcId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={false}
        title="请先选择有效的 BC"
        description="操作记录按当前租户与 BC 展示。"
      />
    )
  // 范围或筛选改变立即重建分页，包含浏览器前进/后退，避免携带旧范围游标。
  return (
    <ManagementTaskList
      key={`${tenantId}:${bcId}:${action ?? ""}:${source ?? ""}`}
      tenantId={tenantId}
      bcId={bcId}
      action={action}
      source={source}
    />
  )
}

function ManagementTaskList({
  tenantId,
  bcId,
  action,
  source,
}: {
  tenantId: string
  bcId: string
  action?: (typeof ACTION_OPTIONS)[number]["value"]
  source?: (typeof SOURCE_OPTIONS)[number]["value"]
}) {
  const navigate = route.useNavigate()
  const paging = useCursorPage()
  // 与其它列表共用分页控件；每次只请求当前页，刷新不会串行重读已浏览页面。
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ad-management-tasks",
      bcId,
      action,
      source,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await AdManagementService.listTasks({
          path: { tenant_id: tenantId },
          query: {
            bc_id: bcId,
            limit: paging.limit,
            cursor: paging.cursor ?? undefined,
            action,
            source,
          },
          signal,
        })
      ).data,
    refetchOnWindowFocus: false,
  })
  const tasks = isForbidden(query.error) ? [] : (query.data?.items ?? [])
  const total = query.data?.total
  const setFilter = (key: "action" | "source", value: string) =>
    void navigate({
      search: (old) => ({ ...old, [key]: value === ALL ? undefined : value }),
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <WorkspacePageTitle>操作记录</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            BC：{bcId} · 启用、暂停、预算、出价等广告修改的执行回执与核查
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
      <div className="flex flex-wrap items-center gap-3">
        <Select
          value={action ?? ALL}
          onValueChange={(value) => setFilter("action", value)}
        >
          <SelectTrigger className="w-36" aria-label="动作">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value={ALL}>全部动作</SelectItem>
              {ACTION_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        <Select
          value={source ?? ALL}
          onValueChange={(value) => setFilter("source", value)}
        >
          <SelectTrigger className="w-36" aria-label="来源">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value={ALL}>全部来源</SelectItem>
              {SOURCE_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </div>
      {query.error && (
        <Alert variant="destructive">
          <AlertTitle>
            {isForbidden(query.error) ? "无权查看操作记录" : "操作记录读取失败"}
          </AlertTitle>
          <AlertDescription>请检查当前租户和 BC 权限后重试。</AlertDescription>
        </Alert>
      )}
      <Card className="py-0" aria-busy={query.isFetching}>
        <CardContent className="overflow-x-auto p-0">
          <Table className="min-w-[860px]">
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>动作</TableHead>
                <TableHead>对象</TableHead>
                <TableHead>来源</TableHead>
                <TableHead>结果</TableHead>
                <TableHead className="text-right">明细</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.isPending &&
                Array.from({ length: 6 }, (_, index) => (
                  <TableRow key={`loading-${index}`}>
                    {Array.from({ length: 6 }, (_, column) => (
                      <TableCell key={column}>
                        <Skeleton className="h-5 w-full min-w-16" />
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              {tasks.map((task) => {
                const parts = resultParts(task.counts)
                return (
                  <TableRow key={task.task_id}>
                    <TableCell className="whitespace-nowrap">
                      {displayTime(task.created_at)}
                    </TableCell>
                    <TableCell className="font-medium">
                      {mutationText(task.mutation)}
                    </TableCell>
                    <TableCell>{targetText(task)}</TableCell>
                    <TableCell>{sourceText(task)}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge
                          variant={
                            task.status === "FAILED" ||
                            task.status === "NEEDS_REVIEW"
                              ? "destructive"
                              : "outline"
                          }
                        >
                          {statusLabel[task.status] ?? task.status}
                        </Badge>
                        {parts.map((part) => (
                          <span
                            key={part.label}
                            className={
                              part.warn
                                ? "text-destructive"
                                : "text-muted-foreground"
                            }
                          >
                            {part.label} {part.value}
                          </span>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <Link
                        className="hover:underline"
                        to="/tenants/$tenantId/ad-management-tasks/$taskId"
                        params={{ tenantId, taskId: task.task_id }}
                        search={{ bc_id: task.bc_id }}
                        aria-label={`查看明细 ${task.task_id}`}
                      >
                        查看明细
                      </Link>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
          {!query.isPending && !query.error && !tasks.length && (
            <p className="p-8 text-center text-sm text-muted-foreground">
              {action || source ? "没有符合筛选条件的操作记录" : "暂无操作记录"}
            </p>
          )}
        </CardContent>
      </Card>
      <Pager
        paging={paging}
        nextCursor={query.error ? undefined : query.data?.next_cursor}
        total={total}
        busy={query.isFetching}
      />
    </div>
  )
}
