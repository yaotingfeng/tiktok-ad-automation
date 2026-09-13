import { useQuery } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import {
  BuildsService,
  type DraftDramaPublic,
  type DraftInputPublic,
  type DraftSummary,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Pager, ServerTable, useCursorPage } from "@/features/tenants/shared"
import { buildKey } from "./api"
import { canChooseDrama, DramaInputSheet } from "./DramaInputSheet"
import { BuildReason, BuildStatus } from "./presentation"

function linkLabel(input: DraftInputPublic, status: DraftSummary["status"]) {
  const progress = input.preparation
  const state = progress?.link_status || input.status
  const labels: Record<string, string> = {
    ready: "已获取",
    duplicate: "重复输入",
    empty: "空输入",
    invalid: "输入无效",
    needs_resolution: "待选择剧目",
    not_found: "未找到剧目",
    failed: "获取失败",
    result_unknown: "结果待核实",
    retryable_error: "暂未获取，等待重试",
    blocked_auth: "连接需要重新认证",
    config_conflict: "链接配置需处理",
    checking: "正在核对链接…",
    creating: "正在获取链接…",
    verifying: "正在核实链接…",
  }
  if (
    status === "BLOCKED" &&
    ["pending", "resolving", "checking", "creating", "verifying"].includes(
      state,
    )
  )
    return "准备已暂停"
  if (labels[state]) return labels[state]
  // 只有确认正式剧名后才称为取链，排队及解析期间不提前宣称解析成功。
  if (["pending", "unresolved", "resolved", "resolving"].includes(state)) {
    if (status === "DRAFT") return "等待解析"
    if (status !== "PREPARING") return "准备已暂停"
    return progress?.title ? "正在获取链接…" : "正在解析剧目…"
  }
  return "待处理"
}

export function BuildDramaTable({
  tenantId,
  bcId,
  summary,
  onMaterial,
  onLink,
  write,
  onChanged,
  onEdit,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  onMaterial: (drama: DraftDramaPublic) => void
  onLink: (id: string) => void
  write: boolean
  onChanged: () => void
  onEdit: () => void
}) {
  const paging = useCursorPage()
  const [detailId, setDetailId] = useState<string | null>(null)
  const query = useQuery({
    // 状态变化仍沿用同一版本的数据，避免 PREPARING → READY 清空表格。
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      summary.revision,
      "drama-progress",
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.inputs({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: { kind: "drama", cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
    refetchInterval: summary.status === "PREPARING" || detailId ? 2000 : false,
  })
  const previousStatus = useRef(summary.status)
  const refetch = query.refetch
  useEffect(() => {
    if (previousStatus.current !== summary.status) {
      previousStatus.current = summary.status
      void refetch()
    }
  }, [summary.status, refetch])
  const detail = query.data?.items.find((input) => input.id === detailId)
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ServerTable
        fetching={query.isFetching}
        showRefreshStatus={false}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        rows={query.data?.items || []}
        loading={query.isPending}
        columns={[
          {
            header: "剧目 / 输入行",
            cell: ({ row }) => (
              <div>
                <span>
                  {row.original.preparation?.title ||
                    row.original.raw_text ||
                    "（空行）"}
                </span>
                <p className="text-xs text-muted-foreground">
                  第 {row.original.line_no} 行
                </p>
              </div>
            ),
          },
          {
            header: "版权方剧目 ID",
            cell: ({ row }) => (
              <span className="break-all">
                {row.original.preparation?.external_drama_id || "—"}
              </span>
            ),
          },
          {
            header: "推广链接",
            cell: ({ row }) => {
              const input = row.original
              const progress = input.preparation
              return (
                <div>
                  {progress?.link_status === "ready" && progress.drama ? (
                    <Button
                      variant="ghost"
                      onClick={() => onLink(progress.drama!.link_id)}
                    >
                      已获取
                    </Button>
                  ) : (
                    <span>{linkLabel(input, summary.status)}</span>
                  )}
                  {(progress?.reason_code ||
                    input.reason_code ||
                    input.duplicate_of != null) && (
                    <p className="text-xs text-muted-foreground">
                      <BuildReason
                        code={progress?.reason_code || input.reason_code}
                      />
                      {input.duplicate_of != null &&
                        `合并到第 ${input.duplicate_of} 行`}
                    </p>
                  )}
                </div>
              )
            },
          },
          {
            header: "匹配素材",
            cell: ({ row }) => {
              const drama = row.original.preparation?.drama
              // 未开始匹配与匹配后零素材是两种结果。
              return drama && drama.material_state !== "pending"
                ? drama.matched_count
                : "—"
            },
          },
          {
            header: "素材准备",
            cell: ({ row }) => {
              const drama = row.original.preparation?.drama
              if (!drama || drama.material_state === "pending")
                return "等待匹配"
              if (drama.material_state === "matching") return "正在匹配…"
              return <BuildStatus value={drama.material_state} />
            },
          },
          {
            header: "操作",
            cell: ({ row }) => {
              const input = row.original
              const drama = input.preparation?.drama
              return (
                <div className="flex flex-wrap gap-1">
                  {drama && (
                    <Button variant="ghost" onClick={() => onMaterial(drama)}>
                      查看与调整素材
                    </Button>
                  )}
                  <Button variant="ghost" onClick={() => setDetailId(input.id)}>
                    {write && canChooseDrama(input) ? "选择剧目" : "输入详情"}
                  </Button>
                  {write &&
                    ["failed", "invalid", "not_found"].includes(
                      input.preparation?.link_status || input.status,
                    ) && (
                      <Button variant="ghost" onClick={onEdit}>
                        返回修正
                      </Button>
                    )}
                </div>
              )
            },
          },
        ]}
        emptyTitle="尚未输入剧目"
      />
      {detail && (
        <DramaInputSheet
          key={detail.id}
          tenantId={tenantId}
          input={detail}
          write={write}
          onClose={() => setDetailId(null)}
          onChanged={() => {
            void query.refetch()
            onChanged()
          }}
          onRefresh={query.refetch}
        />
      )}
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        busy={query.isFetching}
      />
    </div>
  )
}
