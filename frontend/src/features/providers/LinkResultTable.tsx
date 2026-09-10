import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo, useRef, useState } from "react"
import { ProvidersService, type ResolvedLink } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { FilterSelect } from "@/features/accounts/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { CandidateDialog } from "./CandidateDialog"
import { ConfigFacts, CopyField, kinds, resultStates } from "./presentation"
import { providerKey, resultsQuery } from "./queries"
export function LinkResultTable({ taskId }: { taskId: string }) {
  const { tenantId, scope } = useTenantScope(),
    paging = useCursorPage()
  const client = useQueryClient()
  const [filter, setFilter] = useState("all")
  const summary = useQuery({
    queryKey: [...providerKey(tenantId!), "summary", taskId],
    queryFn: async ({ signal }) =>
      (
        await ProvidersService.getPreparationSummary({
          path: { tenant_id: tenantId!, task_id: taskId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data &&
      (q.state.data.pending_count > 0 || !!q.state.data.counts.result_unknown)
        ? 3000
        : false,
  })
  const previousSummary = useRef<string | undefined>(undefined)
  const signature = summary.data
    ? JSON.stringify([summary.data.status, summary.data.counts])
    : undefined
  useEffect(() => {
    if (
      signature &&
      previousSummary.current &&
      signature !== previousSummary.current
    ) {
      void client.invalidateQueries({
        queryKey: [...providerKey(tenantId!), "results", taskId],
      })
      void client.invalidateQueries({
        queryKey: [...providerKey(tenantId!), "history"],
      })
    }
    if (signature) previousSummary.current = signature
  }, [signature, tenantId, taskId, client])
  const query = useQuery({
    ...resultsQuery(tenantId!, taskId, paging.cursor, paging.limit, {
      exceptions_only: filter === "exceptions",
      status:
        filter === "all" || filter === "exceptions"
          ? undefined
          : (filter as ResolvedLink["status"]),
    }),
    refetchInterval:
      summary.data &&
      (summary.data.pending_count > 0 || !!summary.data.counts.result_unknown)
        ? 3000
        : false,
  })
  const data = useRetainedData(query.data, query.error),
    [candidate, setCandidate] = useState<ResolvedLink | null>(null),
    [selectedDetail, setDetail] = useState<ResolvedLink | null>(null),
    [showScope, setShowScope] = useState(false)
  const detail =
    data?.items.find((row) => row.input_id === selectedDetail?.input_id) ||
    selectedDetail
  const write = !!scope && scope.role !== "viewer"
  const columns = useMemo<ColumnDef<ResolvedLink>[]>(
    () => [
      {
        header: "原始输入 / 剧目",
        cell: ({ row: { original: r } }) => (
          <div className="flex max-w-64 flex-col gap-1">
            <p className="text-xs text-muted-foreground">
              第 {r.line_no} 行 · {r.raw_input}
            </p>
            <strong>{r.title || "待解析"}</strong>
            <p className="text-xs">{r.external_drama_id}</p>
            {r.error_code === "drama_not_found" && (
              <p className="text-xs">未找到剧目，请在来源搭建中修改原文。</p>
            )}
          </div>
        ),
      },
      {
        header: "版权方 / 应用",
        cell: ({ row: { original: r } }) => (
          <div>
            {summary.data?.connection_name ||
              kinds[r.provider_kind] ||
              r.provider_kind}
            <p>{summary.data?.application_name}</p>
            <p className="max-w-48 truncate text-xs" title={r.application_id}>
              {r.application_id}
            </p>
            <p className="text-xs">{r.language || "语言待核实"}</p>
          </div>
        ),
      },
      {
        header: "状态",
        cell: ({ row: { original: r } }) => (
          <Badge variant={r.status === "ready" ? "secondary" : "outline"}>
            {resultStates[r.status]}
          </Badge>
        ),
      },
      {
        header: "推广链接",
        cell: ({ row }) => (
          <CopyField
            label="推广链接"
            value={row.original.status === "ready" ? row.original.url : null}
          />
        ),
      },
      {
        header: "归因名称",
        cell: ({ row }) => (
          <CopyField label="归因名称" value={row.original.protected_base} />
        ),
      },
      {
        header: "操作",
        cell: ({ row: { original: r } }) => (
          <div className="flex gap-2">
            {r.status === "needs_resolution" &&
              (write ? (
                <Button size="sm" onClick={() => setCandidate(r)}>
                  处理候选
                </Button>
              ) : (
                <span className="text-xs">等待投手处理候选</span>
              ))}
            <Button variant="ghost" size="sm" onClick={() => setDetail(r)}>
              查看详情
            </Button>
          </div>
        ),
      },
    ],
    [write, summary.data?.connection_name, summary.data?.application_name],
  )
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold">本次取链结果</h2>
          {summary.data && (
            <>
              <p className="text-sm">
                {summary.data.connection_name} · {summary.data.application_name}
              </p>
              <p className="text-sm" role="status">
                共 {summary.data.total_count} 行 · 已就绪{" "}
                {summary.data.ready_count} · 处理中 {summary.data.pending_count}{" "}
                · 异常 {summary.data.exception_count}
              </p>
            </>
          )}
          <p className="break-all text-xs text-muted-foreground">
            任务 {taskId}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={!summary.data}
            onClick={() => setShowScope(true)}
          >
            查看任务范围
          </Button>
          <Button
            variant="outline"
            disabled={query.isFetching}
            onClick={() =>
              void client.invalidateQueries({
                queryKey: providerKey(tenantId!),
              })
            }
          >
            刷新结果
          </Button>
        </div>
      </div>
      {summary.error && !query.error && (
        <RequestError
          error={summary.error}
          retry={() => void summary.refetch()}
        />
      )}
      <Card className="min-w-0">
        <CardHeader>
          <FilterSelect
            label="结果状态"
            value={filter}
            choices={{ exceptions: "仅异常", ...resultStates }}
            onChange={(v) => {
              setFilter(v)
              paging.reset()
            }}
          />
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          <ServerTable
            rows={data?.items || []}
            columns={columns}
            loading={query.isPending && !data}
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={filter !== "all"}
            emptyTitle="本次任务尚无结果"
          />
          {query.error && data && (
            <p role="status" className="text-sm text-muted-foreground">
              保留上次读取的列表，请重试以获取当前结果。
            </p>
          )}
        </CardContent>
        <CardFooter className="block">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching}
          />
        </CardFooter>
      </Card>
      {showScope && summary.data && (
        <ManagementSheet
          title="取链任务范围"
          description="当前租户内本次请求的已记录范围"
          dirty={false}
          onClose={() => setShowScope(false)}
        >
          <div className="flex flex-col gap-4 text-sm">
            <p className="break-all">任务 ID：{taskId}</p>
            <p className="break-all">
              连接：{summary.data.connection_name} ·{" "}
              {summary.data.connection_id}
            </p>
            <p className="break-all">
              应用：{summary.data.application_name} ·{" "}
              {summary.data.application_id}
            </p>
            <h3 className="font-semibold">本次请求配置</h3>
            <ConfigFacts
              config={summary.data.config}
              incomplete={summary.data.config_display_incomplete}
            />
          </div>
        </ManagementSheet>
      )}
      {candidate && (
        <CandidateDialog
          tenantId={tenantId!}
          item={candidate}
          onClose={() => setCandidate(null)}
        />
      )}{" "}
      {detail && (
        <ManagementSheet
          title="取链结果详情"
          description={`第 ${detail.line_no} 行 · ${resultStates[detail.status]}`}
          dirty={false}
          onClose={() => setDetail(null)}
        >
          <div className="flex flex-col gap-5 text-sm">
            <p className="break-all">原文：{detail.raw_input}</p>
            <p>
              {detail.title || "尚未解析剧目"} ·{" "}
              {detail.external_drama_id || "尚无剧目 ID"}
            </p>
            <p className="break-all">
              应用：{detail.application_id} · {detail.language || "语言待核实"}
            </p>
            <h3 className="font-semibold">推广链接</h3>
            <CopyField expanded label="推广链接" value={detail.url} />
            <h3 className="font-semibold">归因名称</h3>
            <CopyField
              expanded
              label="归因名称"
              value={detail.protected_base}
            />
            {detail.status === "blocked_auth" && (
              <p>请联系租户管理员检查账号凭据、应用配置或操作权限。</p>
            )}
            {detail.status === "result_unknown" && (
              <p>
                远端结果尚未核实；超时不代表未创建。刷新结果可查看后续核实进度。
              </p>
            )}
            {detail.status === "config_conflict" && (
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <h3 className="mb-2 font-semibold">已有配置</h3>
                  <ConfigFacts config={detail.existing_config} />
                </div>
                <div>
                  <h3 className="mb-2 font-semibold">本次请求</h3>
                  <ConfigFacts config={detail.requested_config} />
                </div>
              </div>
            )}
            {detail.error_message && <p>{detail.error_message}</p>}
            {detail.error_code && (
              <p className="text-xs text-muted-foreground">
                错误代码：{detail.error_code}
              </p>
            )}
          </div>
        </ManagementSheet>
      )}
    </div>
  )
}
