import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useRouterState } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { useState } from "react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { BuildGuard } from "@/features/builds/presentation"
import { canManage, RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import {
  type CopyConfiguration,
  type CopyDraftPublic,
  type CopyMutation,
  configurationError,
  copyKey,
  getCopyDraft,
  mutateCopyDraft,
  pendingKey,
  readPending,
  scopeKey,
} from "./api"
import {
  CopyEditor,
  CopyMaterials,
  type Editor,
  type EditorKind,
} from "./CopyEditors"
import { CopyPreview, CopyTable } from "./CopyTable"

import { copyErrorLabel } from "./presentation"

const statusLabel = {
  DRAFT: "草稿",
  PREPARING: "正在生成预览",
  READY: "预览已就绪",
  SUBMITTED: "已提交搭建",
  FAILED: "预览失败",
}
export function CopyWorkspace() {
  const { tenantId, scope, bc } = useTenantScope()
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const draftId = /\/campaign-copies\/([^/]+)/.exec(pathname)?.[1]
  const knownBC =
    tenantId && draftId
      ? sessionStorage.getItem(scopeKey(tenantId, draftId))
      : null
  if (
    !tenantId ||
    !scope?.bcId ||
    !bc ||
    !draftId ||
    bc.ownership_conflict ||
    bc.binding_status === "DISABLED" ||
    bc.binding_status === "ERROR" ||
    (knownBC && knownBC !== scope.bcId)
  ) {
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请选择草稿所属的有效 BC"
        description="复制草稿绑定创建时的租户与 BC，请切回原 BC 并确认连接可用。"
      />
    )
  }
  return (
    <CopyLoader
      key={`${tenantId}:${scope.bcId}:${draftId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      draftId={draftId}
      write={scope.role !== "viewer"}
    />
  )
}
function CopyLoader({
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
  const query = useQuery({
    queryKey: copyKey(tenantId, bcId, draftId),
    queryFn: ({ signal }) => getCopyDraft(tenantId, bcId, draftId, signal),
    refetchInterval: (q) =>
      q.state.data?.status === "PREPARING" ? 2000 : false,
    refetchOnWindowFocus: false,
  })
  if (query.isPending) return <p role="status">正在恢复复制草稿…</p>
  if (query.error)
    return (
      <RequestError error={query.error} retry={() => void query.refetch()} />
    )
  if (query.data.bc_id !== bcId)
    return (
      <Alert variant="destructive">
        <AlertTitle>草稿不属于当前 BC</AlertTitle>
        <AlertDescription>请切回草稿所属 BC 后继续。</AlertDescription>
      </Alert>
    )
  return (
    <CopyPanel
      tenantId={tenantId}
      bcId={bcId}
      data={query.data}
      write={write}
      refresh={() => void query.refetch()}
    />
  )
}
function CopyPanel({
  tenantId,
  bcId,
  data,
  write,
  refresh,
}: {
  tenantId: string
  bcId: string
  data: CopyDraftPublic
  write: boolean
  refresh: () => void
}) {
  const client = useQueryClient()
  const [rows, setRows] = useState(data.rows)
  const [baseline, setBaseline] = useState(data.rows)
  const [revision, setRevision] = useState(data.revision)
  const [selected, setSelected] = useState<string[]>([])
  const [editor, setEditor] = useState<Editor | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const [notice, setNotice] = useState("")
  const ledgerKey = pendingKey(tenantId, bcId, data.draft_id)
  const [pending, setPending] = useState(() => readPending(ledgerKey))
  const dirty = JSON.stringify(rows) !== JSON.stringify(baseline)
  const conflict = revision !== data.revision
  const editable =
    write &&
    !busy &&
    !pending &&
    !conflict &&
    !["PREPARING", "SUBMITTED"].includes(data.status)
  const invalid = configurationError(rows)
  const count = rows.reduce(
    (total, row) =>
      total +
      row.configuration.target_advertiser_ids.length * row.configuration.copies,
    0,
  )
  function change(ids: string[], patch: Partial<CopyConfiguration>) {
    if (!editable) return
    setRows((current) =>
      current.map((row) =>
        ids.includes(row.row_id)
          ? { ...row, configuration: { ...row.configuration, ...patch } }
          : row,
      ),
    )
    setNotice("")
  }
  function open(kind: EditorKind, ids: string[]) {
    if (editable && ids.length) setEditor({ kind, rowIds: ids })
  }
  function accept(next: CopyDraftPublic) {
    client.setQueryData(copyKey(tenantId, bcId, data.draft_id), next)
    setRows(next.rows)
    setBaseline(next.rows)
    setRevision(next.revision)
  }
  async function send(mutation: CopyMutation) {
    // 响应丢失时只重放原始修订与完整请求，避免产生第二份执行意图。
    sessionStorage.setItem(ledgerKey, JSON.stringify(mutation))
    setPending(mutation)
    try {
      const next = await mutateCopyDraft(tenantId, data.draft_id, mutation)
      sessionStorage.removeItem(ledgerKey)
      setPending(null)
      accept(next)
      return next
    } catch (caught) {
      const status =
        caught instanceof AxiosError ? caught.response?.status : undefined
      if (
        status &&
        status >= 400 &&
        status < 500 &&
        ![408, 429].includes(status)
      ) {
        sessionStorage.removeItem(ledgerKey)
        setPending(null)
      }
      throw caught
    }
  }
  async function run(action: "save" | "preview" | "submit" | "retry") {
    if (!write || busy || (action !== "retry" && (pending || conflict))) return
    setBusy(true)
    setError(undefined)
    setNotice("")
    try {
      if (action === "retry") {
        const replay = readPending(ledgerKey)
        if (replay) await send(replay)
        return
      }
      let current = data
      if (action === "save" || (action === "preview" && dirty)) {
        current = await send({
          kind: "save",
          body: {
            request_id: crypto.randomUUID(),
            expected_revision: revision,
            rows: rows.map(({ row_id, configuration }) => ({
              row_id,
              configuration,
            })),
          },
        })
        setNotice("复制草稿已保存。")
      }
      if (action === "preview")
        await send({
          kind: "preview",
          body: {
            request_id: crypto.randomUUID(),
            expected_revision: current.revision,
          },
        })
      if (
        action === "submit" &&
        !dirty &&
        current.preview_digest &&
        current.status === "READY"
      )
        await send({
          kind: "submit",
          body: {
            request_id: crypto.randomUUID(),
            expected_revision: current.revision,
            preview_digest: current.preview_digest,
          },
        })
    } catch (caught) {
      setError(caught)
      refresh()
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-col gap-5">
      <BuildGuard dirty={dirty || busy || !!pending} />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <WorkspacePageTitle>批量复制</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            从源系列实际配置创建副本，逐行调整；版权方与小程序分别沿用。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={!editable || !dirty}
          onClick={() => void run("save")}
        >
          保存草稿
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span>1 配置复制方案</span>
        <span aria-hidden>→</span>
        <span>2 核对新系列</span>
        <span aria-hidden>→</span>
        <span>3 搭建任务</span>
        <Badge variant="secondary">{statusLabel[data.status]}</Badge>
        {dirty && <Badge variant="outline">有未保存修改</Badge>}
      </div>
      {!write && (
        <Alert>
          <AlertDescription>
            当前为只读角色，可查看方案与任务，不能修改或提交。
          </AlertDescription>
        </Alert>
      )}
      {conflict && (
        <Alert variant="destructive">
          <AlertTitle>草稿修订已更新</AlertTitle>
          <AlertDescription>
            <p>
              其他操作已更新服务器草稿。请重新载入最新配置后编辑，本页修改不会覆盖新修订。
            </p>
            <Button
              variant="outline"
              onClick={() => {
                accept(data)
                setError(undefined)
              }}
            >
              载入最新草稿（丢弃本页修改）
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {pending && (
        <Alert>
          <AlertTitle>上次请求结果待确认</AlertTitle>
          <AlertDescription>
            <p>重试将使用原请求标识与内容，不会重复创建副本。</p>
            <Button
              variant="outline"
              disabled={busy || !write}
              onClick={() => void run("retry")}
            >
              重试原请求
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!!error && <RequestError error={error} />}
      {data.error_code && (
        <Alert variant="destructive">
          <AlertTitle>无法完成复制预览</AlertTitle>
          <AlertDescription>{copyErrorLabel(data.error_code)}</AlertDescription>
        </Alert>
      )}
      <CopyTable
        rows={rows}
        editable={editable}
        selected={selected}
        setSelected={setSelected}
        change={change}
        open={open}
      />
      {data.children.length > 0 && (
        <CopyPreview tenantId={tenantId} bcId={bcId} data={data} />
      )}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-col gap-1">
          <span className="text-sm">
            {rows.length} 条来源 · 将创建 {count} 个新系列
          </span>
          <span className="text-xs text-muted-foreground">
            币种：
            {[...new Set(rows.map((row) => row.source.currency))].join(" / ")}
            （来源币种；目标预算以预览为准）
          </span>
          {invalid && (
            <p role="alert" className="text-sm text-destructive">
              {invalid}
            </p>
          )}
          <p role="status" className="text-sm">
            {busy
              ? "正在处理…"
              : data.status === "PREPARING"
                ? "正在逐个生成并核验复制预览，可稍后返回此页。"
                : notice}
          </p>
        </div>
        <div className="flex gap-2">
          {data.status === "READY" && !dirty ? (
            <Button
              disabled={!editable || !!invalid || !data.preview_digest}
              onClick={() => void run("submit")}
            >
              创建并启用 {data.children.length} 个新系列
            </Button>
          ) : (
            data.status !== "SUBMITTED" && (
              <Button
                disabled={!editable || !!invalid}
                onClick={() => void run("preview")}
              >
                生成复制预览
              </Button>
            )
          )}
          {data.status === "SUBMITTED" && (
            <Button variant="outline" asChild>
              <Link
                to="/tenants/$tenantId/build-tasks"
                params={{ tenantId }}
                search={{ bc_id: bcId }}
              >
                查看搭建任务
              </Link>
            </Button>
          )}
        </div>
      </div>
      {editor?.kind === "materials" ? (
        <CopyMaterials
          row={rows.find((row) => row.row_id === editor.rowIds[0])!}
          onClose={() => setEditor(null)}
          onSave={(material_ids) => {
            change(editor.rowIds, { material_ids })
            setEditor(null)
          }}
        />
      ) : (
        editor && (
          <CopyEditor
            editor={editor}
            rows={rows}
            tenantId={tenantId}
            bcId={bcId}
            onClose={() => setEditor(null)}
            onSave={(patch) => {
              change(editor.rowIds, patch)
              setEditor(null)
            }}
          />
        )
      )}
    </div>
  )
}
