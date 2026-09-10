import { useQuery } from "@tanstack/react-query"
import { useNavigate, useRouterState } from "@tanstack/react-router"
import { useState } from "react"
import {
  BuildsService,
  type FrozenGroup,
  type PreviewSummary,
  type PreviewUnit,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { CopyField } from "@/features/providers/presentation"
import { normalizeDecimal } from "@/features/strategies/validation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  canManage,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { buildKey } from "./api"
import {
  BuildGuard,
  BuildReason,
  BuildStatus,
  BuildSteps,
} from "./presentation"
import { usePreviewSubmission } from "./usePreviewSubmission"
export type PreviewSubmit = (preview: PreviewSummary) => Promise<void>
export function PreviewWorkspace() {
  const { tenantId, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname }),
    previewId = /\/build-previews\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !previewId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description="冻结预览属于创建时的租户与 BC。"
      />
    )
  return (
    <BuildPreviewPanel
      key={`${tenantId}:${scope.bcId}:${previewId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      previewId={previewId}
      write={scope.role !== "viewer"}
    />
  )
}
export function BuildPreviewPanel({
  tenantId,
  bcId,
  previewId,
  write,
  onSubmit,
}: {
  tenantId: string
  bcId: string
  previewId: string
  write: boolean
  onSubmit?: PreviewSubmit
}) {
  const { scope } = useTenantScope()
  const [tab, setTab] = useState("dramas"),
    [dramaId, setDramaId] = useState<string | undefined>(),
    [unit, setUnit] = useState<PreviewUnit | null>(null),
    navigate = useNavigate()
  const summary = useQuery({
    queryKey: [...buildKey(tenantId, bcId), "preview", previewId, "summary"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.previewSummary({
          path: { tenant_id: tenantId, preview_id: previewId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data?.status === "BUILDING" ? 2000 : false,
  })
  const submission = usePreviewSubmission(
    tenantId,
    bcId,
    previewId,
    write && !summary.error,
  )
  const current = summary.data
  if (summary.isPending) return <Skeleton className="h-64 w-full" />
  if (summary.error && !current)
    return (
      <RequestError
        error={summary.error}
        retry={() => void summary.refetch()}
      />
    )
  if (!current || current.bc_id !== bcId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        reason="bc-mismatch"
        resourceBcId={current?.bc_id}
        title="当前 BC 与预览不一致"
        description="请切换到预览所属 BC 查看冻结范围。"
      />
    )
  const back = () =>
    void navigate({
      to: "/tenants/$tenantId/build-drafts/$draftId",
      params: { tenantId, draftId: current.draft_id },
      search: { bc_id: bcId },
    })
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <WorkspacePageTitle>搭建预览</WorkspacePageTitle>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          草稿 v{current.draft_revision} ·{" "}
          <BuildStatus value={current.status} />
        </p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void summary.refetch()}>
            刷新预览状态
          </Button>
          <Button variant="outline" onClick={back}>
            返回调整
          </Button>
        </div>
      </div>
      <BuildSteps step={3} />
      {summary.error && (
        <RequestError
          error={summary.error}
          retry={() => void summary.refetch()}
        />
      )}
      <BuildGuard
        dirty={submission.busy || submission.unknown}
        title="提交结果尚待核实"
        description="离开只停止本页等待；已受理的广告任务仍会继续。返回此预览可按原请求核实。"
        leaveLabel="离开并稍后核实"
      />
      {!!submission.error && <RequestError error={submission.error} />}
      {submission.unknown && (
        <Alert>
          <AlertDescription>
            <p role="status">
              正在确认提交结果。已保留原请求标识，不会再次整批创建。
            </p>
            <Button
              variant="outline"
              disabled={submission.busy}
              onClick={() => void submission.recover()}
            >
              查询原提交结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {submission.record?.receipt && (
        <Alert>
          <AlertDescription>
            <p>本预览已受理，请查看原任务结果。</p>
            <Button
              onClick={() => void submission.go(submission.record!.receipt!)}
            >
              查看已受理任务
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {current.status === "OBSOLETE" && (
        <Alert variant="destructive">
          <AlertDescription>
            草稿已更新，当前预览已过期。返回调整后重新生成预览，原冻结配置不会被修改。
          </AlertDescription>
        </Alert>
      )}
      {current.status === "FAILED" && (
        <Alert variant="destructive">
          <AlertDescription>
            预览未完成：{current.error_code || "请核实当前草稿后重新生成"}
          </AlertDescription>
        </Alert>
      )}
      {current.status === "BUILDING" || current.status === "FAILED" ? (
        <Card>
          <CardContent className="flex min-w-0 flex-col gap-2">
            <p role="status">
              {current.status === "FAILED"
                ? "尚未生成完整冻结预览，请返回调整并核实原因。"
                : "正在生成冻结预览，可离开后通过本页恢复。"}
            </p>
            <p className="text-sm text-muted-foreground">
              统计尚未完成，暂不展示最终提交数量与预算。
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <Alert>
            <AlertDescription>
              本次排除 {current.blocked_count} 个阻断组合；另有{" "}
              {current.input_issue_count}{" "}
              条输入问题，未形成组合。仅提交可用与提交后可分发范围，排除项不会自动补入。
            </AlertDescription>
          </Alert>
          <Tabs
            value={tab}
            onValueChange={(value) => {
              setTab(value)
              setDramaId(undefined)
            }}
          >
            <TabsList>
              <TabsTrigger value="dramas">剧目汇总</TabsTrigger>
              <TabsTrigger value="units">账户组合</TabsTrigger>
              <TabsTrigger value="excluded">排除组合</TabsTrigger>
              <TabsTrigger value="issues">输入问题</TabsTrigger>
            </TabsList>
          </Tabs>
          {tab === "dramas" ? (
            <PreviewDramaTable
              tenantId={tenantId}
              bcId={bcId}
              preview={current}
              onDrama={(id) => {
                setDramaId(id)
                setTab("units")
              }}
            />
          ) : tab === "issues" ? (
            <PreviewExclusions
              tenantId={tenantId}
              bcId={bcId}
              previewId={previewId}
              onEdit={back}
            />
          ) : (
            <PreviewUnitTable
              key={tab}
              tenantId={tenantId}
              bcId={bcId}
              previewId={previewId}
              dramaId={dramaId}
              excluded={tab === "excluded"}
              onUnit={setUnit}
            />
          )}
          <PreviewSummaryBar
            preview={current}
            write={write && !submission.forbidden && !summary.error}
            onSubmit={onSubmit || submission.submit}
            unavailable={!!submission.record || submission.busy}
          />
        </>
      )}
      {unit && (
        <FrozenUnitSheet
          tenantId={tenantId}
          bcId={bcId}
          unit={unit}
          onClose={() => setUnit(null)}
        />
      )}
    </div>
  )
}
export function PreviewSummaryBar({
  preview,
  write,
  onSubmit,
  unavailable = false,
}: {
  preview: PreviewSummary
  write: boolean
  onSubmit?: PreviewSubmit
  unavailable?: boolean
}) {
  const [pending, setPending] = useState(false),
    [error, setError] = useState<unknown>()
  return (
    <div className="sticky bottom-0 space-y-3 rounded-lg border bg-background p-4 shadow-sm">
      {!!error && <RequestError error={error} />}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <p className="font-semibold">
            {preview.campaign_count} Campaign · {preview.adgroup_count} Ad Group
            · {preview.ad_count} Ad
          </p>
          <p className="mt-1 text-sm">
            配置日预算合计 {preview.currency}{" "}
            {normalizeDecimal(preview.daily_budget_sum)}
          </p>
          <p className="text-xs text-muted-foreground">
            各 Campaign 配置日预算之和，非预计实际消耗。排除{" "}
            {preview.blocked_count} 个阻断组合，{preview.preparing_count}{" "}
            个组合将在提交后分发素材。
          </p>
        </div>
        {write && (
          <div className="space-y-2">
            <Button
              disabled={
                !onSubmit ||
                unavailable ||
                pending ||
                preview.status !== "FROZEN" ||
                preview.campaign_count === 0
              }
              aria-label={`创建并立即启用 ${preview.campaign_count} 个 Campaign / ${preview.adgroup_count} 个 Ad Group / ${preview.ad_count} 条 Ad`}
              onClick={async () => {
                if (!onSubmit || pending) return
                setPending(true)
                try {
                  await onSubmit(preview)
                } catch (e) {
                  setError(e)
                } finally {
                  setPending(false)
                }
              }}
            >
              {pending ? "正在确认提交结果" : "创建并立即启用"}
            </Button>
            <p className="text-xs text-muted-foreground">
              将直接创建并启用，审核与实际投放状态由 TikTok 决定。
            </p>
            {!onSubmit && (
              <p className="text-xs text-muted-foreground">
                提交功能尚未开放，当前可查看与调整预览。
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
export function PreviewUnitTable({
  tenantId,
  bcId,
  previewId,
  excluded,
  dramaId,
  onUnit,
}: {
  tenantId: string
  bcId: string
  previewId: string
  excluded: boolean
  dramaId?: string
  onUnit: (unit: PreviewUnit) => void
}) {
  const paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "preview",
        previewId,
        "units",
        excluded,
        dramaId,
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.previewUnits({
            path: { tenant_id: tenantId, preview_id: previewId },
            query: {
              cursor: paging.cursor,
              limit: paging.limit,
              readiness: excluded ? "BLOCKED" : undefined,
              drama_id: dramaId,
            },
            signal,
          })
        ).data,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ServerTable
        rows={query.data?.items || []}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={excluded}
        emptyTitle={excluded ? "没有阻断组合" : "尚无账户组合"}
        columns={[
          {
            header: "剧目 / 账户 ID",
            cell: ({ row }) => (
              <div>
                {row.original.title}
                <p className="break-all font-mono text-xs text-muted-foreground">
                  {row.original.advertiser_id}
                </p>
              </div>
            ),
          },
          {
            header: "准备状态",
            cell: ({ row }) => (
              <>
                {row.original.readiness === "PREPARING" ? (
                  <span>提交后分发</span>
                ) : (
                  <BuildStatus value={row.original.readiness} />
                )}
                <p className="break-all text-xs text-muted-foreground">
                  {row.original.reason_codes.map((code) => (
                    <BuildReason key={code} code={code} />
                  ))}
                </p>
              </>
            ),
          },
          {
            header: "Ad Group / Ad",
            cell: ({ row }) =>
              `${row.original.group_count} / ${row.original.ad_count}`,
          },
          {
            header: "Campaign 日预算",
            cell: ({ row }) =>
              `${row.original.currency} ${normalizeDecimal(row.original.budget)}`,
          },
          {
            header: "Campaign 名称",
            cell: ({ row }) => (
              <CopyField
                label="Campaign 名称"
                value={row.original.campaign_name}
              />
            ),
          },
          {
            header: "操作",
            cell: ({ row }) => (
              <Button variant="ghost" onClick={() => onUnit(row.original)}>
                查看冻结详情
              </Button>
            ),
          },
        ]}
      />
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        busy={query.isFetching}
      />
    </div>
  )
}
function PreviewExclusions({
  tenantId,
  bcId,
  previewId,
  onEdit,
}: {
  tenantId: string
  bcId: string
  previewId: string
  onEdit: () => void
}) {
  const [kind, setKind] = useState<"drama" | "account">("drama"),
    paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "preview",
        previewId,
        "issues",
        kind,
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.previewInputs({
            path: { tenant_id: tenantId, preview_id: previewId },
            query: {
              kind,
              issues_only: true,
              cursor: paging.cursor,
              limit: paging.limit,
            },
            signal,
          })
        ).data,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Tabs
        value={kind}
        onValueChange={(v) => {
          setKind(v as "drama" | "account")
          paging.reset()
        }}
      >
        <TabsList>
          <TabsTrigger value="drama">剧目输入</TabsTrigger>
          <TabsTrigger value="account">账户输入</TabsTrigger>
        </TabsList>
      </Tabs>
      <div className="flex min-w-0 flex-col gap-4">
        <ServerTable
          rows={query.data?.items || []}
          loading={query.isPending}
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          emptyTitle="没有输入问题"
          columns={[
            { header: "输入行", accessorKey: "line_no" },
            { header: "原始文本", accessorKey: "raw_text" },
            {
              header: "状态 / 原因",
              cell: ({ row }) => (
                <>
                  <BuildStatus value={row.original.status} />
                  <p className="break-all text-xs">
                    <BuildReason code={row.original.reason_code} />
                  </p>
                </>
              ),
            },
            {
              header: "操作",
              cell: () => (
                <Button variant="ghost" onClick={onEdit}>
                  返回修正
                </Button>
              ),
            },
          ]}
        />
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          busy={query.isFetching}
        />
      </div>
    </div>
  )
}
function FrozenUnitSheet({
  tenantId,
  bcId,
  unit,
  onClose,
}: {
  tenantId: string
  bcId: string
  unit: PreviewUnit
  onClose: () => void
}) {
  const paging = useCursorPage(),
    detail = useQuery({
      queryKey: [...buildKey(tenantId, bcId), "unit", unit.unit_id],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.frozenUnit({
            path: { tenant_id: tenantId, unit_id: unit.unit_id },
            signal,
          })
        ).data,
    }),
    groups = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "unit",
        unit.unit_id,
        "groups",
        paging.cursor,
        paging.limit,
      ],
      enabled: detail.data?.bc_id === bcId,
      queryFn: async ({ signal }) =>
        (
          await BuildsService.frozenGroups({
            path: { tenant_id: tenantId, unit_id: unit.unit_id },
            query: { cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
    })
  return (
    <ManagementSheet
      title={`冻结详情 · ${unit.title}`}
      description={`账户 ${unit.advertiser_id}。以下为预览冻结时的真实配置。`}
      dirty={false}
      onClose={onClose}
    >
      <div className="space-y-4">
        {detail.isPending && <Skeleton className="h-32" />}
        {detail.error && <RequestError error={detail.error} />}{" "}
        {detail.data && detail.data.bc_id === bcId && (
          <>
            <CopyField
              label="Campaign 名称"
              value={detail.data.campaign_name}
              expanded
            />
            <p className="text-sm">
              Campaign 日预算 {detail.data.currency}{" "}
              {normalizeDecimal(detail.data.budget)} · ROAS{" "}
              {detail.data.target_roas}
            </p>
            <CopyField label="推广链接" value={detail.data.url} expanded />
            <p className="break-all text-xs text-muted-foreground">
              策略版本 {detail.data.strategy_version_id}
            </p>
            <details className="rounded-md border p-3">
              <summary className="cursor-pointer text-sm">
                Minis、Identity 与场景能力快照
              </summary>
              <pre className="mt-2 whitespace-pre-wrap break-all text-xs">
                {JSON.stringify(detail.data.scene_snapshot, null, 2)}
              </pre>
            </details>
            <h3 className="font-semibold">冻结分组与 SP 创意</h3>
            {groups.error && <RequestError error={groups.error} />}{" "}
            {groups.isPending && <Skeleton className="h-32" />}
            {groups.data?.items.map((group) => (
              <GroupDetail key={group.group_id} group={group} />
            ))}
            <Pager
              paging={paging}
              nextCursor={groups.data?.next_cursor}
              busy={groups.isFetching}
            />
          </>
        )}
      </div>
    </ManagementSheet>
  )
}
function GroupDetail({ group }: { group: FrozenGroup }) {
  const [open, setOpen] = useState(false)
  return (
    <details
      className="rounded-md border p-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm">
        第 {group.group_no} 组 · {group.material_ids.length} 份素材 ·{" "}
        {group.ads.length} 条 SP 创意
      </summary>
      {open && (
        <div className="mt-3 space-y-3">
          <CopyField label="Ad Group 名称" value={group.name} expanded />
          <p className="break-all font-mono text-xs">
            素材 ID：{group.material_ids.join("、")}
          </p>
          {group.ads.map((ad) => (
            <div key={ad.ad_id} className="space-y-2 rounded-md bg-muted p-3">
              <h4 className="text-sm font-semibold">SP{ad.creative_no}</h4>
              <CopyField label="Ad 名称" value={ad.name} expanded />
              <p className="whitespace-pre-wrap text-sm">{ad.text}</p>
              <p className="break-all text-xs">
                CTA：{ad.cta_option_ids.join("、") || "无 CTA"}
              </p>
            </div>
          ))}
        </div>
      )}
    </details>
  )
}

function PreviewDramaTable({
  tenantId,
  bcId,
  preview,
  onDrama,
}: {
  tenantId: string
  bcId: string
  preview: PreviewSummary
  onDrama: (id: string) => void
}) {
  const paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        "preview",
        preview.preview_id,
        "dramas",
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.previewDramas({
            path: { tenant_id: tenantId, preview_id: preview.preview_id },
            query: { cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ServerTable
        rows={query.data?.items || []}
        columns={[
          { header: "剧目", accessorKey: "title" },
          {
            header: "素材 / 分组",
            cell: ({ row }) =>
              `${row.original.material_count} / ${row.original.material_group_count}`,
          },
          {
            header: "提交户 / 总户",
            cell: ({ row }) =>
              `${row.original.eligible_campaign_count} / ${row.original.account_count}`,
          },
          {
            header: "Campaign / Ad Group / Ad",
            cell: ({ row }) =>
              `${row.original.eligible_campaign_count} / ${row.original.eligible_adgroup_count} / ${row.original.eligible_ad_count}`,
          },
          {
            header: "配置日预算合计",
            cell: ({ row }) =>
              `${preview.currency} ${normalizeDecimal(row.original.daily_budget_sum)}`,
          },
          {
            header: "操作",
            cell: ({ row }) => (
              <Button
                variant="ghost"
                onClick={() => onDrama(row.original.drama_id)}
              >
                查看账户组合
              </Button>
            ),
          },
        ]}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="尚无冻结剧目"
      />
      <Pager
        paging={paging}
        nextCursor={query.data?.next_cursor}
        busy={query.isFetching}
      />
    </div>
  )
}
