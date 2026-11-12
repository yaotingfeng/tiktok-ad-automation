import { useNavigate } from "@tanstack/react-router"
import {
  BarChart3,
  ClipboardCheck,
  Cog,
  Eye,
  Play,
  Settings2,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { cn } from "@/lib/utils"
import { AIOptimizerConfig } from "./AIOptimizerConfig"
import { AIOptimizerEditor } from "./AIOptimizerEditor"
import { type AIOptimizer, AIOptimizerList } from "./AIOptimizerList"
import { AIOptimizerOverview } from "./AIOptimizerOverview"
import { AIOptimizerReviews } from "./AIOptimizerReviews"
import { AIWorkbenchPage } from "./AIWorkbenchPage"
import { aiRequest, businessToday, errorMessage } from "./api"
import { DailyReport } from "./DailyReport"

type View = "overview" | "schedule" | "execution" | "review" | "pending"

type Props = {
  tenantId: string
  optimizerId?: string
  view?: View
  recordId?: string
  versionId?: string
  returnContext?: string
}

export const aiWorkbenchMenu: Array<{
  id: View
  label: string
  icon: typeof Eye
}> = [
  { id: "overview", label: "概览", icon: Eye },
  { id: "schedule", label: "排期", icon: BarChart3 },
  { id: "execution", label: "执行", icon: Play },
  { id: "review", label: "复盘", icon: ClipboardCheck },
  { id: "pending", label: "待确认", icon: Settings2 },
]

export function buildAIWorkbenchSwitchSearch<T extends object>(
  previous: T,
  optimizerId: string,
): T & {
  optimizer_id: string
  record_id: undefined
  version_id: undefined
  return_to: undefined
} {
  return {
    ...previous,
    optimizer_id: optimizerId,
    record_id: undefined,
    version_id: undefined,
    return_to: undefined,
  }
}

export function aiOccurrenceEndpoint(tenantId: string, optimizerId: string) {
  return `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}/occurrences`
}

export function buildAIWorkbenchHref(
  tenantId: string,
  optimizerId: string,
  view: View,
  returnContext?: string,
) {
  const params = new URLSearchParams()
  if (optimizerId) params.set("optimizer_id", optimizerId)
  if (view !== "overview") params.set("view", view)
  if (returnContext) params.set("return_to", returnContext)
  return `/tenants/${tenantId}/ai-workbench${params.toString() ? `?${params.toString()}` : ""}`
}

/** AI 工作台独立于广告投放分组；左侧菜单和 URL 一起切换，切换优化师时整棵视图重新挂载。 */
export function AIWorkbenchShell(props: Props) {
  return <TenantWorkbenchShell key={props.tenantId} {...props} />
}

function TenantWorkbenchShell({
  tenantId,
  optimizerId = "",
  view = "overview",
  recordId,
  versionId,
  returnContext,
}: Props) {
  const navigate = useNavigate()
  const [optimizers, setOptimizers] = useState<AIOptimizer[]>([])
  const [refresh, setRefresh] = useState(0)
  const [editor, setEditor] = useState<"create" | "edit" | null>(null)
  const selected = optimizers.find((item) => item.id === optimizerId) ?? null
  const context =
    returnContext ||
    `/tenants/${tenantId}/ai-workbench?optimizer_id=${encodeURIComponent(optimizerId)}`
  const onLoaded = useCallback(
    (items: AIOptimizer[]) => setOptimizers(items),
    [],
  )
  const selectOptimizer = useCallback(
    (next: string) => {
      setEditor(null)
      // 先在 URL 清空旧记录标识，再由 key 强制丢弃表格、提案和执行详情状态。
      void navigate({
        to: "/tenants/$tenantId/ai-workbench",
        params: { tenantId },
        search: (previous) => buildAIWorkbenchSwitchSearch(previous, next),
      })
    },
    [navigate, tenantId],
  )
  const hrefFor = useMemo(
    () => (next: View) =>
      buildAIWorkbenchHref(tenantId, optimizerId, next, context),
    [context, optimizerId, tenantId],
  )
  return (
    <section
      className="flex min-w-0 flex-col gap-5"
      aria-label="AI 优化师工作台"
      data-testid="ai-workbench-shell"
    >
      <header className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="workspace-title">AI 优化师工作台</h1>
          <p className="text-sm text-muted-foreground">
            租户 {tenantId} · 策略、规则与执行证据共用原有配置。
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => setEditor("create")}>创建优化师</Button>
          <Button asChild variant="outline">
            <a href={`/tenants/${tenantId}/ai-workbench`}>
              <Cog />
              工作台首页
            </a>
          </Button>
        </div>
      </header>
      <div className="grid min-w-0 gap-5 lg:grid-cols-[220px_minmax(0,1fr)]">
        <aside className="min-w-0 space-y-3" aria-label="AI 工作台菜单">
          <AIOptimizerList
            tenantId={tenantId}
            selectedId={optimizerId}
            onSelect={selectOptimizer}
            onLoaded={onLoaded}
            key={`${tenantId}:${refresh}`}
          />
          <nav
            className="rounded-lg border bg-background p-2"
            aria-label="AI 二级菜单"
          >
            <p className="px-2 pb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              工作台
            </p>
            {aiWorkbenchMenu.map(({ id, label, icon: Icon }) => (
              <a
                key={id}
                href={hrefFor(id)}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2 py-2 text-sm hover:bg-muted",
                  view === id && "bg-muted font-medium",
                )}
                data-testid={`ai-nav-${id}`}
              >
                <Icon className="size-4" />
                <span>{label}</span>
              </a>
            ))}
          </nav>
        </aside>
        <main key={`${tenantId}:${optimizerId}`} className="min-w-0 space-y-4">
          <div
            className="flex flex-wrap items-center gap-2 rounded-lg border bg-background px-4 py-3 text-sm"
            data-testid="ai-inherited-context"
          >
            <span className="text-muted-foreground">继承上下文</span>
            <Separator orientation="vertical" className="h-4" />
            <strong>
              {selected?.bc_id ? `BC ${selected.bc_id}` : "BC 未读取"}
            </strong>
            <span className="text-muted-foreground">·</span>
            <span>{selected?.advertiser_ids.join("、") || "账户未读取"}</span>
            <span className="text-muted-foreground">·</span>
            <span>
              {selected?.strategy_version_id
                ? `策略 ${selected.strategy_version_id}`
                : "策略未读取"}
            </span>
            <span className="ml-auto rounded-full bg-muted px-2 py-0.5 text-xs">
              {selected?.optimization_mode ?? "—"}
            </span>
          </div>
          {view === "overview" && (
            <>
              <AIOptimizerOverview optimizer={selected} />
              <AIOptimizerConfig
                tenantId={tenantId}
                optimizer={selected}
                recordId={recordId}
                versionId={versionId}
                returnContext={context}
                onEdit={() => setEditor("edit")}
                onSaved={(updated) => {
                  setOptimizers((items) =>
                    items.map((item) =>
                      item.id === updated.id ? updated : item,
                    ),
                  )
                  setRefresh((value) => value + 1)
                }}
              />
              {selected && (
                <DailyReport
                  tenantId={tenantId}
                  optimizerId={selected.id}
                  businessDate={businessToday(selected.timezone)}
                />
              )}
            </>
          )}
          {view === "schedule" && (
            <AIWorkbenchPage
              key={`${tenantId}:${optimizerId}:schedule`}
              tenantId={tenantId}
              optimizerId={optimizerId}
            />
          )}
          {view === "execution" && (
            <ExecutionView
              tenantId={tenantId}
              optimizerId={optimizerId}
              recordId={recordId}
            />
          )}
          {view === "review" && (
            <AIOptimizerReviews tenantId={tenantId} optimizerId={optimizerId} />
          )}
          {view === "pending" && (
            <AIOptimizerReviews
              tenantId={tenantId}
              optimizerId={optimizerId}
              pendingOnly
            />
          )}
          {versionId && (
            <p className="text-xs text-muted-foreground">
              策略版本上下文：{versionId}
              {recordId ? ` · 记录 ${recordId}` : ""}
            </p>
          )}
        </main>
      </div>
      {editor && (
        <AIOptimizerEditor
          key={`${tenantId}:${editor}:${optimizerId}`}
          tenantId={tenantId}
          optimizer={editor === "edit" ? selected : null}
          onClose={() => setEditor(null)}
          onSaved={(saved) => {
            setOptimizers((items) => [
              ...items.filter((item) => item.id !== saved.id),
              saved,
            ])
            setRefresh((value) => value + 1)
            setEditor(null)
            selectOptimizer(saved.id)
          }}
        />
      )}
    </section>
  )
}

function ExecutionView({
  tenantId,
  optimizerId,
  recordId,
}: {
  tenantId: string
  optimizerId: string
  recordId?: string
}) {
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([])
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!optimizerId) return
    const controller = new AbortController()
    setRows([])
    setError(null)
    aiRequest<Array<Record<string, unknown>>>(
      aiOccurrenceEndpoint(tenantId, optimizerId),
      { signal: controller.signal },
    )
      .then((items) => {
        if (!controller.signal.aborted) setRows(items)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
    return () => controller.abort()
  }, [tenantId, optimizerId])
  return (
    <div className="grid gap-4 lg:grid-cols-2" data-testid="ai-execution-view">
      <div className="rounded-lg border bg-background p-5">
        <h2 className="font-semibold">执行记录</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          执行时才匹配素材；排期表只保留剧目、推广链路和下单意图。
        </p>
        {recordId && (
          <p className="mt-3 text-xs text-muted-foreground">
            当前记录：{recordId}
          </p>
        )}
        {error && (
          <p className="mt-3 text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
        {!error && !rows.length && (
          <p className="mt-3 text-sm text-muted-foreground">暂无执行记录。</p>
        )}
        {rows.map((row) => (
          <div
            className="mt-3 rounded-md border p-3 text-sm"
            key={String(row.id)}
          >
            <div className="flex justify-between gap-2">
              <strong>{String(row.title ?? "未命名剧目")}</strong>
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                {String(row.status ?? "PENDING")}
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              计划时间：{String(row.scheduled_at ?? "—")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Submission：{String(row.submission_id ?? "—")}
            </p>
          </div>
        ))}
      </div>
      <div className="rounded-lg border bg-background p-5">
        <h2 className="font-semibold">素材匹配与回读证据</h2>
        {rows.length ? (
          rows.map((row) => (
            <pre
              className="mt-3 max-h-64 overflow-auto rounded bg-muted p-3 text-xs"
              key={`${String(row.id)}-evidence`}
            >
              {JSON.stringify(
                {
                  evidence: row.evidence,
                  error_code: row.error_code,
                  preview_id: row.preview_id,
                },
                null,
                2,
              )}
            </pre>
          ))
        ) : (
          <p className="mt-2 text-sm text-muted-foreground">
            等待优化师 {optimizerId || "—"} 的执行回读证据。
          </p>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          租户 {tenantId} 范围内展示。
        </p>
      </div>
    </div>
  )
}
