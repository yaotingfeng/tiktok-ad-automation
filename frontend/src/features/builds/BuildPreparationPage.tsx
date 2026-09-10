import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import { useCallback, useEffect, useRef, useState } from "react"
import {
  BuildsService,
  type DraftDramaPublic,
  type DraftInputPublic,
  type DraftSummary,
  ProvidersService,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { FilterSelect } from "@/features/accounts/presentation"
import {
  canManage,
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import {
  buildKey,
  loadDraftInputs,
  mutationKey,
  readPendingMutation,
} from "./api"
import { BuildInputPage } from "./BuildInputPage"
import { BuildLinkSheet } from "./BuildLinkSheet"
import { DramaMaterialSheet } from "./DramaMaterialSheet"
import {
  BuildError,
  BuildReason,
  BuildStatus,
  BuildSteps,
  reportError,
  unknownOutcome,
} from "./presentation"
export function BuildPreparationPage() {
  const { tenantId, scope, bc } = useTenantScope(),
    pathname = useRouterState({ select: (s) => s.location.pathname })
  const draftId = /\/build-drafts\/([^/]+)/.exec(pathname)?.[1]
  if (!tenantId || !scope?.bcId || !bc || !draftId)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description="草稿属于创建时的租户与 BC。请通过顶栏选择。"
      />
    )
  return (
    <Preparation
      key={`${tenantId}:${scope.bcId}:${draftId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      draftId={draftId}
      write={scope.role !== "viewer"}
    />
  )
}
function Preparation({
  tenantId,
  bcId,
  draftId,
  write,
}: {
  tenantId: string
  bcId: string
  draftId: string
  write: boolean
}) {
  const { scope } = useTenantScope()
  const queryClient = useQueryClient(),
    navigate = useNavigate(),
    search = useRouterState({
      select: (s) => s.location.search as { edit?: boolean; prepare?: boolean },
    }),
    [tab, setTab] = useState("dramas"),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [forbidden, setForbidden] = useState(false),
    [material, setMaterial] = useState<DraftDramaPublic | null>(null),
    [linkId, setLinkId] = useState<string | null>(null),
    [restored, setRestored] = useState<{
      drama: string[]
      account: string[]
    } | null>(null),
    [restoring, setRestoring] = useState(false),
    [progress, setProgress] = useState(0),
    [restoreError, setRestoreError] = useState<unknown>()
  const controller = useRef(new AbortController()),
    restoreController = useRef<AbortController | null>(null),
    restoreStarted = useRef(false)
  const operationKey = `build-prepare:${tenantId}:${bcId}:${draftId}`
  const [pending, setPending] = useState<string | null>(() =>
      sessionStorage.getItem(operationKey),
    ),
    [previewRevision, setPreviewRevision] = useState<number | null>(() => {
      const v = sessionStorage.getItem(`build-preview:${tenantId}:${draftId}`)
      return v ? Number(v) : null
    })
  const summary = useQuery({
    queryKey: [...buildKey(tenantId, bcId), draftId, "summary"],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.summary({
          path: { tenant_id: tenantId, draft_id: draftId },
          signal,
        })
      ).data,
    refetchInterval: (q) =>
      q.state.data?.status === "PREPARING" ? 2000 : false,
  })
  const pendingMutation = readPendingMutation(
    mutationKey(tenantId, bcId, draftId),
  )
  const current = summary.data,
    scoped = current?.bc_id === bcId,
    allowed = write && !forbidden && !isForbidden(summary.error)
  useEffect(() => {
    const ctrl = new AbortController()
    controller.current = ctrl
    return () => {
      ctrl.abort()
      restoreController.current?.abort()
    }
  }, [])
  const restore = useCallback(async () => {
    if (!current || !scoped || restoring) return
    restoreController.current?.abort()
    const ctrl = new AbortController()
    restoreController.current = ctrl
    setRestoring(true)
    setRestoreError(undefined)
    setProgress(0)
    try {
      const data = await loadDraftInputs(
        tenantId,
        draftId,
        ctrl.signal,
        setProgress,
      )
      const { data: latest } = await BuildsService.summary({
        path: { tenant_id: tenantId, draft_id: draftId },
        signal: ctrl.signal,
      })
      if (latest.revision !== current.revision)
        throw new Error("草稿恢复期间已改变")
      if (!ctrl.signal.aborted) setRestored(data)
    } catch (e) {
      if (!ctrl.signal.aborted) setRestoreError(e)
    } finally {
      if (restoreController.current === ctrl) setRestoring(false)
    }
  }, [current, scoped, restoring, tenantId, draftId])
  useEffect(() => {
    if (search.edit && scoped && !restoreStarted.current) {
      const timer = setTimeout(() => {
        restoreStarted.current = true
        void restore()
      }, 0)
      return () => clearTimeout(timer)
    }
  }, [search.edit, scoped, restore])
  const refetchSummary = summary.refetch
  const prepare = useCallback(
    async (recover = false) => {
      if (
        busy ||
        !allowed ||
        !scoped ||
        readPendingMutation(mutationKey(tenantId, bcId, draftId))
      )
        return
      setBusy(true)
      setError(undefined)
      const requestId = pending || crypto.randomUUID()
      try {
        if (recover || pending) {
          await BuildsService.savedPrepareRequest({
            path: { tenant_id: tenantId, request_id: requestId },
            signal: controller.current.signal,
          })
        } else {
          sessionStorage.setItem(operationKey, requestId)
          setPending(requestId)
          await BuildsService.prepare({
            path: { tenant_id: tenantId, draft_id: draftId },
            body: { request_id: requestId },
            signal: controller.current.signal,
          })
        }
        sessionStorage.removeItem(operationKey)
        setPending(null)
        await refetchSummary()
        await navigate({
          to: "/tenants/$tenantId/build-drafts/$draftId",
          params: { tenantId, draftId },
          search: { bc_id: bcId },
        })
      } catch (e) {
        if (!controller.current.signal.aborted) {
          reportError(e)
          setError(e)
          if (!unknownOutcome(e) && !recover && !pending) {
            sessionStorage.removeItem(operationKey)
            setPending(null)
          }
          if ((e as { response?: { status: number } }).response?.status === 403)
            setForbidden(true)
        }
      } finally {
        setBusy(false)
      }
    },
    [
      busy,
      allowed,
      scoped,
      pending,
      tenantId,
      operationKey,
      draftId,
      bcId,
      navigate,
      refetchSummary,
    ],
  )
  const automatic = useRef(false)
  useEffect(() => {
    if (
      search.prepare &&
      scoped &&
      allowed &&
      !automatic.current &&
      (pending ||
        sessionStorage.getItem(`build-start:${tenantId}:${bcId}:${draftId}`))
    ) {
      const timer = setTimeout(() => {
        automatic.current = true
        sessionStorage.removeItem(`build-start:${tenantId}:${bcId}:${draftId}`)
        void prepare(!!pending)
      }, 0)
      return () => clearTimeout(timer)
    }
  }, [
    search.prepare,
    scoped,
    allowed,
    prepare,
    pending,
    tenantId,
    bcId,
    draftId,
  ])
  async function preview(recover = false) {
    if (!current || busy || !allowed || !scoped || pendingMutation) return
    setBusy(true)
    setError(undefined)
    const revision = previewRevision ?? current.revision
    try {
      let previewId: string
      if (recover || previewRevision !== null) {
        const { data } = await BuildsService.previewRequest({
          path: { tenant_id: tenantId, draft_id: draftId, revision },
          signal: controller.current.signal,
        })
        previewId = data.preview_id
      } else {
        sessionStorage.setItem(
          `build-preview:${tenantId}:${draftId}`,
          String(revision),
        )
        setPreviewRevision(revision)
        const { data } = await BuildsService.generatePreview({
          path: { tenant_id: tenantId, draft_id: draftId },
          body: { expected_revision: revision },
          signal: controller.current.signal,
        })
        previewId = data.preview_id
      }
      sessionStorage.removeItem(`build-preview:${tenantId}:${draftId}`)
      setPreviewRevision(null)
      await navigate({
        to: "/tenants/$tenantId/build-previews/$previewId",
        params: { tenantId, previewId },
        search: { bc_id: bcId },
      })
    } catch (e) {
      if (!controller.current.signal.aborted) {
        reportError(e)
        setError(e)
        if (!unknownOutcome(e) && !recover && previewRevision === null) {
          sessionStorage.removeItem(`build-preview:${tenantId}:${draftId}`)
          setPreviewRevision(null)
        }
      }
    } finally {
      setBusy(false)
    }
  }
  async function recoverMutation() {
    if (!pendingMutation || busy) return
    setBusy(true)
    setError(undefined)
    try {
      const { data } = await BuildsService.savedMutation({
        path: { tenant_id: tenantId, request_id: pendingMutation.requestId },
        signal: controller.current.signal,
      })
      if (data.draft_id !== draftId) throw new Error("修改结果不属于当前草稿")
      if (!controller.current.signal.aborted) {
        sessionStorage.removeItem(mutationKey(tenantId, bcId, draftId))
        await summary.refetch()
      }
    } catch (e) {
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const edit = () => {
    restoreStarted.current = false
    setRestored(null)
    void navigate({
      to: "/tenants/$tenantId/build-drafts/$draftId",
      params: { tenantId, draftId },
      search: { bc_id: bcId, edit: true },
    })
  }
  if (summary.isPending) return <Skeleton className="h-64 w-full" />
  if (summary.error && !summary.data)
    return (
      <RequestError
        error={summary.error}
        retry={() => void summary.refetch()}
      />
    )
  if (!current || !scoped)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        reason="bc-mismatch"
        resourceBcId={current?.bc_id}
        title="当前 BC 与草稿不一致"
        description="请返回草稿所属 BC 查看，或在当前 BC 新建搭建。"
      />
    )
  if (search.edit) {
    return (
      <div className="flex min-w-0 flex-col gap-6">
        <WorkspacePageTitle>编辑搭建输入</WorkspacePageTitle>
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted-foreground">
            服务器草稿 v{current.revision} · 本地输入会保留，保存时检查版本。
          </p>
          <Button variant="outline" onClick={() => void summary.refetch()}>
            检查最新草稿状态
          </Button>
        </div>
        {summary.error && (
          <RequestError
            error={summary.error}
            retry={() => void summary.refetch()}
          />
        )}
        {!restored ? (
          <Card>
            <CardContent className="flex min-w-0 flex-col gap-3">
              <p role="status">
                {restoring
                  ? `正在恢复完整原输入，已加载 ${progress} 行…`
                  : "原输入尚未完整恢复，编辑与保存尚未开放。"}
              </p>
              {!!restoreError && <RequestError error={restoreError} />}
              <Button
                variant="outline"
                onClick={() => {
                  if (restoring) {
                    restoreController.current?.abort()
                    setRestoring(false)
                  } else void restore()
                }}
              >
                {restoring ? "取消加载" : "重新加载完整输入"}
              </Button>
            </CardContent>
          </Card>
        ) : (
          <BuildInputPage
            tenantId={tenantId}
            bcId={bcId}
            write={allowed}
            summary={current}
            original={restored}
            onCancel={() =>
              void navigate({
                to: "/tenants/$tenantId/build-drafts/$draftId",
                params: { tenantId, draftId },
                search: { bc_id: bcId },
              })
            }
            onSaved={async (start) => {
              setRestored(null)
              await summary.refetch()
              await navigate({
                to: "/tenants/$tenantId/build-drafts/$draftId",
                params: { tenantId, draftId },
                search: { bc_id: bcId },
              })
              if (start) void prepare()
            }}
          />
        )}
      </div>
    )
  }
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <WorkspacePageTitle>准备与调整</WorkspacePageTitle>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          草稿 v{current.revision} · <BuildStatus value={current.status} />
        </p>
        <Button
          variant="outline"
          onClick={() =>
            void queryClient.invalidateQueries({
              queryKey: buildKey(tenantId, bcId),
            })
          }
        >
          刷新准备结果
        </Button>
      </div>
      <BuildSteps step={2} />
      {summary.error && (
        <RequestError
          error={summary.error}
          retry={() => void summary.refetch()}
        />
      )}
      {pendingMutation && (
        <Alert>
          <AlertDescription>
            <p>
              此草稿有尚待确认的修改。请先查询原修改结果，再准备或生成预览。
            </p>
            <Button disabled={busy} onClick={() => void recoverMutation()}>
              查询原修改结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!!error && <BuildError error={error} />}
      {pending && (
        <Alert>
          <AlertDescription>
            <p>准备请求结果待核实，请查询原请求；不会重复发起取链。</p>
            <Button disabled={busy} onClick={() => void prepare(true)}>
              查询原准备结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {previewRevision !== null && (
        <Alert>
          <AlertDescription>
            <p>正在确认 v{previewRevision} 的原预览结果。</p>
            <Button disabled={busy} onClick={() => void preview(true)}>
              查询原预览结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          ["有效剧目", current.drama_count],
          ["有效账户", current.account_count],
          [
            "剧目输入",
            Object.values(current.input_counts.drama || {}).reduce(
              (a, b) => a + b,
              0,
            ),
          ],
          [
            "账户输入",
            Object.values(current.input_counts.account || {}).reduce(
              (a, b) => a + b,
              0,
            ),
          ],
        ].map(([label, value]) => (
          <Card key={label}>
            <CardContent>
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="mt-1 text-xl font-semibold">{value}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <p role="status" className="text-sm text-muted-foreground">
        {current.status === "PREPARING"
          ? "后台正在解析与准备，可离开后通过本草稿恢复。"
          : "素材按完整剧名匹配，可在本次草稿中调整。全部有效剧目覆盖同一批全部有效账户。"}
      </p>
      {current.error_code && (
        <p role="alert" className="break-all text-sm text-destructive">
          准备异常：{current.error_code}
        </p>
      )}
      {current.provider_task_id && (
        <Button variant="outline" asChild>
          <Link
            to="/tenants/$tenantId/providers"
            params={{ tenantId }}
            search={{
              bc_id: bcId,
              tab: "links",
              task_id: current.provider_task_id,
            }}
          >
            查看取链核实进度与配置差异
          </Link>
        </Button>
      )}
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="dramas">剧目与素材</TabsTrigger>
          <TabsTrigger value="accounts">账户解析</TabsTrigger>
          <TabsTrigger value="inputs">剧目输入</TabsTrigger>
        </TabsList>
      </Tabs>
      {tab === "dramas" ? (
        <DramaTable
          tenantId={tenantId}
          bcId={bcId}
          summary={current}
          onMaterial={setMaterial}
          onLink={setLinkId}
        />
      ) : (
        <InputTable
          key={tab}
          tenantId={tenantId}
          bcId={bcId}
          summary={current}
          kind={tab === "accounts" ? "account" : "drama"}
          write={allowed}
          onChanged={() => void prepare()}
          onEdit={edit}
        />
      )}
      <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background p-4">
        <span className="text-sm">预览将明确列出可搭建范围与排除原因。</span>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={busy} onClick={edit}>
            {allowed ? "返回输入" : "查看原输入"}
          </Button>
          {allowed && (
            <>
              <Button
                variant="outline"
                disabled={
                  busy ||
                  !!pending ||
                  !!pendingMutation ||
                  current.status === "PREPARING"
                }
                onClick={() => void prepare()}
              >
                解析并准备
              </Button>
              <Button
                disabled={
                  busy ||
                  !!pending ||
                  previewRevision !== null ||
                  !!pendingMutation ||
                  current.status === "PREPARING"
                }
                onClick={() => void preview()}
              >
                生成搭建预览
              </Button>
            </>
          )}
        </div>
      </div>
      {linkId && (
        <BuildLinkSheet
          tenantId={tenantId}
          linkId={linkId}
          onClose={() => setLinkId(null)}
        />
      )}
      {material && (
        <DramaMaterialSheet
          tenantId={tenantId}
          bcId={bcId}
          summary={current}
          drama={material}
          write={allowed && current.status === "READY"}
          onClose={() => setMaterial(null)}
          onSaved={() => {
            setMaterial(null)
            void summary.refetch()
          }}
        />
      )}
    </div>
  )
}
function DramaTable({
  tenantId,
  bcId,
  summary,
  onMaterial,
  onLink,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  onMaterial: (d: DraftDramaPublic) => void
  onLink: (id: string) => void
}) {
  const paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        ...buildKey(tenantId, bcId),
        summary.draft_id,
        summary.revision,
        summary.status,
        "dramas",
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await BuildsService.dramas({
            path: { tenant_id: tenantId, draft_id: summary.draft_id },
            query: { cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
      refetchInterval: summary.status === "PREPARING" ? 2000 : false,
    })
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {query.error ? (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      ) : (
        <ServerTable
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          rows={query.data?.items || []}
          loading={query.isPending}
          columns={[
            {
              header: "确认剧名 / 输入行",
              cell: ({ row }) => (
                <div>
                  {row.original.title}
                  <p className="text-xs text-muted-foreground">
                    第 {row.original.first_line} 行
                  </p>
                </div>
              ),
            },
            {
              header: "推广链接",
              cell: ({ row }) => (
                <Button
                  variant="ghost"
                  onClick={() => onLink(row.original.link_id)}
                >
                  查看推广链接
                </Button>
              ),
            },
            { header: "匹配素材", accessorKey: "matched_count" },
            {
              header: "素材准备",
              cell: ({ row }) => (
                <BuildStatus value={row.original.material_state} />
              ),
            },
            {
              header: "操作",
              cell: ({ row }) => (
                <Button
                  variant="ghost"
                  onClick={() => onMaterial(row.original)}
                >
                  查看与调整素材
                </Button>
              ),
            },
          ]}
          emptyTitle="尚无已确认剧目"
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
function InputTable({
  tenantId,
  bcId,
  summary,
  kind,
  write,
  onChanged,
  onEdit,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  kind: "drama" | "account"
  write: boolean
  onChanged: () => void
  onEdit: () => void
}) {
  const paging = useCursorPage(),
    [status, setStatus] = useState("all"),
    [candidate, setCandidate] = useState<DraftInputPublic | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    ctrl = useRef(new AbortController())
  useEffect(() => {
    const controller = new AbortController()
    ctrl.current = controller
    return () => controller.abort()
  }, [])
  const query = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      summary.revision,
      summary.status,
      "inputs",
      kind,
      status,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.inputs({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: {
            kind,
            status: status === "all" ? undefined : status,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    refetchInterval: summary.status === "PREPARING" ? 2000 : false,
  })
  const candidateUnknown =
    !!candidate?.provider_input_id &&
    !!sessionStorage.getItem(
      `build-candidate:${tenantId}:${candidate.provider_input_id}`,
    )
  async function choose(externalId: string) {
    if (!candidate?.provider_input_id || busy || !write || candidateUnknown)
      return
    const key = `build-candidate:${tenantId}:${candidate.provider_input_id}`
    setBusy(true)
    setError(undefined)
    try {
      sessionStorage.setItem(key, externalId)
      await ProvidersService.postCandidate({
        path: { tenant_id: tenantId, input_id: candidate.provider_input_id },
        body: { external_drama_id: externalId },
        signal: ctrl.current.signal,
      })
      sessionStorage.removeItem(key)
      setCandidate(null)
      onChanged()
      void query.refetch()
    } catch (e) {
      if (!unknownOutcome(e)) sessionStorage.removeItem(key)
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <FilterSelect
        label="输入状态"
        value={status}
        onChange={(value) => {
          setStatus(value)
          paging.reset()
        }}
        choices={Object.fromEntries(
          Object.keys(summary.input_counts[kind] || {}).map((k) => [k, k]),
        )}
      />
      <div className="flex min-w-0 flex-col gap-4">
        {query.error ? (
          <RequestError error={query.error} />
        ) : (
          <ServerTable
            fetching={query.isFetching}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={false}
            rows={query.data?.items || []}
            loading={query.isPending}
            columns={[
              { header: "输入行", accessorKey: "line_no" },
              { header: "原始文本", accessorKey: "raw_text" },
              {
                header: "结果",
                cell: ({ row }) => (
                  <>
                    <BuildStatus value={row.original.status} />
                    <p className="break-all text-xs text-muted-foreground">
                      <BuildReason code={row.original.reason_code} />
                      {row.original.duplicate_of != null &&
                        ` · 合并到第 ${row.original.duplicate_of} 行`}
                    </p>
                  </>
                ),
              },
              {
                header: kind === "account" ? "账户 ID" : "剧目 ID",
                cell: ({ row }) => (
                  <span className="break-all font-mono text-xs">
                    {row.original.advertiser_id ||
                      row.original.drama_id ||
                      "尚未解析"}
                  </span>
                ),
              },
              {
                header: "操作",
                cell: ({ row }) =>
                  write && (
                    <Button
                      variant="ghost"
                      onClick={() =>
                        row.original.candidates.length
                          ? setCandidate(row.original)
                          : onEdit()
                      }
                    >
                      {row.original.candidates.length
                        ? kind === "account"
                          ? "查看账户候选"
                          : "选择对应剧目"
                        : "返回修正"}
                    </Button>
                  ),
              },
            ]}
            emptyTitle="没有符合条件的输入"
          />
        )}
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          busy={query.isFetching}
        />
      </div>
      <Dialog
        open={!!candidate}
        onOpenChange={(open) => {
          if (!open && !busy) setCandidate(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {kind === "drama" ? "选择对应剧目" : "账户名称存在歧义"}
            </DialogTitle>
            <DialogDescription>
              仅继续处理第 {candidate?.line_no} 行：{candidate?.raw_text}
            </DialogDescription>
          </DialogHeader>
          {!!error && <BuildError error={error} />}
          {candidateUnknown && !busy && (
            <Alert>
              <AlertDescription>
                <p>
                  候选选择结果尚待核实，不会重发或改选。请查看原取链任务中的这条输入。
                </p>
                {summary.provider_task_id && (
                  <Button variant="outline" asChild>
                    <Link
                      to="/tenants/$tenantId/providers"
                      params={{ tenantId }}
                      search={{
                        bc_id: bcId,
                        tab: "links",
                        task_id: summary.provider_task_id,
                      }}
                    >
                      查看原取链任务
                    </Link>
                  </Button>
                )}
              </AlertDescription>
            </Alert>
          )}
          <div className="max-h-80 space-y-2 overflow-y-auto">
            {candidate?.candidates.map((item, index) => (
              <Button
                key={String(item.external_drama_id || index)}
                className="h-auto w-full justify-start whitespace-normal text-left"
                variant="outline"
                disabled={
                  busy ||
                  candidateUnknown ||
                  (kind === "drama"
                    ? typeof item.external_drama_id !== "string"
                    : typeof item.advertiser_id !== "string")
                }
                onClick={() => {
                  if (kind === "drama")
                    void choose(String(item.external_drama_id))
                  else {
                    void navigator.clipboard.writeText(
                      String(item.advertiser_id),
                    )
                    setCandidate(null)
                    onEdit()
                  }
                }}
              >
                {kind === "account"
                  ? "复制账户 ID 并返回修正"
                  : String(item.title || "名称待核实")}{" "}
                ·{" "}
                {String(
                  item.external_drama_id || item.advertiser_id || "ID 待核实",
                )}
              </Button>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
