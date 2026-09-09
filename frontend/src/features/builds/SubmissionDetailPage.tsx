import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { BuildsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { displayTime, Identifier } from "@/features/accounts/presentation"
import { normalizeDecimal } from "@/features/strategies/validation"
import { canManage, isForbidden, RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import {
  countObjects,
  SubmissionBadge,
  SubmissionProgress,
} from "./SubmissionPresentation"
import { SubmissionRecoveryActions } from "./SubmissionRecoveryActions"
import {
  SubmissionEventsTable,
  SubmissionFilterBar,
  SubmissionStepsTable,
  SubmissionUnitsTable,
} from "./SubmissionTables"
import { rememberedList, submissionKey } from "./submission-page"

type Filters = {
  advertiserId?: string
  dramaId?: string
  kind?: string
  result?: string
}
export function SubmissionDetailPage() {
  const { tenantId, tenant, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname }),
    id = /\/build-tasks\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !id)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description="搭建任务属于提交时的租户与 BC。"
      />
    )
  return (
    <Detail
      key={`${tenantId}:${scope.bcId}:${id}`}
      tenantId={tenantId}
      tenantName={tenant?.name || tenantId}
      bcId={scope.bcId}
      submissionId={id}
      write={scope.role !== "viewer"}
    />
  )
}
function Detail({
  tenantId,
  tenantName,
  bcId,
  submissionId,
  write,
}: {
  tenantId: string
  tenantName: string
  bcId: string
  submissionId: string
  write: boolean
}) {
  const { scope } = useTenantScope()
  const navigate = useNavigate(),
    client = useQueryClient(),
    search = useRouterState({
      select: (s) => s.location.search as Record<string, unknown>,
    }),
    tab = ["issues", "excluded", "events"].includes(String(search.tab))
      ? String(search.tab)
      : "details",
    stateKey = `submission-filters:${tenantId}:${bcId}:${submissionId}`
  const [filters, setFilters] = useState<Record<string, Filters>>(() => {
    try {
      const v = JSON.parse(sessionStorage.getItem(stateKey) || "{}")
      if (typeof search.result === "string")
        v[tab] = { ...v[tab], result: search.result }
      return v
    } catch {
      return {
        [tab]: {
          result: typeof search.result === "string" ? search.result : undefined,
        },
      }
    }
  })
  useEffect(() => {
    try {
      sessionStorage.setItem(stateKey, JSON.stringify(filters))
    } catch {}
  }, [stateKey, filters])
  const current = filters[tab] || {},
    patch = (value: Filters) => {
      setFilters((prev) => ({ ...prev, [tab]: { ...prev[tab], ...value } }))
      if ("result" in value)
        void navigate({
          to: "/tenants/$tenantId/build-tasks/$submissionId",
          params: { tenantId, submissionId },
          search: { bc_id: bcId, tab, result: value.result || undefined },
          replace: true,
        })
    }
  const query = useQuery({
    queryKey: [...submissionKey(tenantId, bcId), submissionId, "summary"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.getSubmission({
          path: { tenant_id: tenantId, submission_id: submissionId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      ["QUEUED", "RUNNING"].includes(q.state.data?.status || "") ? 2000 : false,
  })
  const data = query.data
  const previousProgress = useRef<string | undefined>(undefined)
  const progressSignature = data
    ? JSON.stringify([
        data.updated_at,
        data.status,
        data.stage_counts,
        data.succeeded,
        data.failed,
        data.unknown,
        data.pending,
      ])
    : undefined
  useEffect(() => {
    if (
      previousProgress.current &&
      progressSignature &&
      previousProgress.current !== progressSignature
    ) {
      void client.invalidateQueries({
        queryKey: [...submissionKey(tenantId, bcId), submissionId],
        predicate: (q) => q.queryKey[5] !== "summary",
      })
    }
    previousProgress.current = progressSignature
  }, [progressSignature, client, tenantId, bcId, submissionId])
  const refresh = () =>
    void client.invalidateQueries({
      queryKey: [...submissionKey(tenantId, bcId), submissionId],
    })
  const back = (
    <Button variant="outline" asChild>
      <Link
        to="/tenants/$tenantId/build-tasks"
        params={{ tenantId }}
        search={rememberedList(tenantId, bcId)}
      >
        返回任务列表
      </Link>
    </Button>
  )
  if (query.isPending) return <Skeleton className="h-64 w-full" />
  if (query.error && (!data || isForbidden(query.error)))
    return (
      <div className="flex flex-col gap-4">
        <RequestError error={query.error} retry={() => void query.refetch()} />
        {back}
      </div>
    )
  if (!data || data.bc_id !== bcId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        reason="bc-mismatch"
        resourceBcId={data?.bc_id}
        title="当前 BC 与任务不一致"
        description="请切换到提交时的 BC 查看任务。"
      />
    )
  return (
    <div className="flex flex-col gap-5">
      {query.error && (
        <div className="flex flex-col gap-2">
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
          <p className="text-sm text-muted-foreground">
            当前展示上次成功读取的结果，读取于{" "}
            {displayTime(new Date(query.dataUpdatedAt).toISOString())}。
          </p>
        </div>
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">任务 {data.batch_short_id}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <SubmissionBadge status={data.status} />
            <span className="text-sm">
              {data.drama_count} 剧 · {data.account_count} 户 · 另有{" "}
              {data.excluded_unit_count} 个排除组合
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={refresh}>
            刷新任务结果
          </Button>
          {back}
        </div>
      </div>
      <div className="flex flex-col gap-2 rounded-lg border bg-card p-4 text-sm">
        <div className="flex flex-wrap gap-4">
          <span>
            任务所属：{tenantName} · BC {data.bc_id}
          </span>
          <span>提交人：{data.actor_name || "暂未获取"}</span>
        </div>
        <div className="flex flex-wrap gap-4">
          <span>版权方：{data.provider_name || "暂无冻结版权方"}</span>
          <span>策略：{data.strategy_label || "暂未获取"}</span>
        </div>
        <details>
          <summary className="cursor-pointer text-muted-foreground">
            完整编号与时间
          </summary>
          <div className="mt-2 flex flex-col gap-2">
            <Identifier value={data.submission_id} />
            <span>
              提交于 {displayTime(data.created_at)} · 最近更新{" "}
              {displayTime(data.updated_at)}
            </span>
            <Identifier value={data.preview_id} />
          </div>
        </details>
      </div>
      <SubmissionProgress data={data} />
      <Alert>
        <AlertDescription>
          <div className="flex flex-col gap-2">
            <p>
              {data.status === "COMPLETED"
                ? "本次提交已完成。"
                : data.status === "NEEDS_REVIEW"
                  ? "先确认是否已创建，避免重复创建。"
                  : data.status === "QUEUED"
                    ? "任务已受理，等待执行。"
                    : data.status === "RUNNING"
                      ? "正在准备素材、创建或核查结果，其他可执行组合继续处理。"
                      : "当前任务存在确定失败，请查看原因和可处理范围。"}
            </p>
            {countObjects(data.succeeded) > 0 && (
              <p>已创建部分保持现状；启用状态、审核和实际投放分别核实。</p>
            )}
            <p>
              配置日预算合计 {data.currency}{" "}
              {normalizeDecimal(data.daily_budget_sum)}，为已提交 Campaign
              配置之和，非预计实际消耗。
            </p>
            {!data.expanded && <p>后台正在展开执行范围，统计将继续更新。</p>}
          </div>
        </AlertDescription>
      </Alert>
      <SubmissionRecoveryActions
        tenantId={tenantId}
        bcId={bcId}
        submissionId={submissionId}
        recovery={data.recovery}
        write={write}
      />
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" asChild>
          <Link
            to="/tenants/$tenantId/build-previews/$previewId"
            params={{ tenantId, previewId: data.preview_id }}
            search={{ bc_id: bcId }}
          >
            查看冻结预览
          </Link>
        </Button>
        <Button variant="outline" asChild>
          <Link
            to="/tenants/$tenantId/accounts"
            params={{ tenantId }}
            search={{ bc_id: bcId, tab: "connections" }}
          >
            账户与授权
          </Link>
        </Button>
      </div>
      <Tabs
        value={tab}
        onValueChange={(value) =>
          void navigate({
            to: "/tenants/$tenantId/build-tasks/$submissionId",
            params: { tenantId, submissionId },
            search: { bc_id: bcId, tab: value },
          })
        }
      >
        <TabsList>
          <TabsTrigger value="details">搭建明细</TabsTrigger>
          <TabsTrigger value="issues">异常与待核实</TabsTrigger>
          <TabsTrigger value="excluded">排除项</TabsTrigger>
          <TabsTrigger value="events">操作记录</TabsTrigger>
        </TabsList>
      </Tabs>
      {tab !== "events" && (
        <SubmissionFilterBar
          key={tab}
          tenantId={tenantId}
          bcId={bcId}
          previewId={data.preview_id}
          {...current}
          onAccount={(v) => patch({ advertiserId: v })}
          onDrama={(v) => patch({ dramaId: v })}
          {...(tab === "issues"
            ? {
                onKind: (v: string) => patch({ kind: v }),
                onResult: (v: string) => patch({ result: v }),
              }
            : {})}
        />
      )}
      {tab === "events" ? (
        <SubmissionEventsTable {...{ tenantId, bcId, submissionId }} />
      ) : tab === "issues" ? (
        <SubmissionStepsTable
          {...{ tenantId, bcId, submissionId, ...current }}
        />
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            {tab === "excluded"
              ? "排除组合未提交，不提供重试；修复后需在新预览明确提交。"
              : "已创建表示已有远端对象；ENABLE 表示启用，不表示已投放或已消耗。"}
          </p>
          <SubmissionUnitsTable
            {...{ tenantId, bcId, submissionId }}
            advertiserId={current.advertiserId}
            dramaId={current.dramaId}
            excluded={tab === "excluded"}
          />
        </>
      )}
    </div>
  )
}
