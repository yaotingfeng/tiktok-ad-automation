import { useQuery } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import {
  BuildsService,
  type DraftDramaPublic,
  type DraftMaterialPublic,
  type DraftSummary,
  type MaterialPublic,
  MaterialsService,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { Pager, RequestError, useCursorPage } from "@/features/tenants/shared"
import { loadDraftMaterials, mutationKey } from "./api"
import { BuildError, reportError, unknownOutcome } from "./presentation"

type Item = DraftMaterialPublic
export function DramaMaterialSheet({
  tenantId,
  bcId,
  summary,
  drama,
  write,
  onClose,
  onSaved,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  drama: DraftDramaPublic
  write: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const key = mutationKey(tenantId, bcId, summary.draft_id)
  const [pending, setPending] = useState<{
    requestId: string
    items?: Item[]
    dramaId?: string
  } | null>(() => {
    try {
      return JSON.parse(sessionStorage.getItem(key) || "null")
    } catch {
      return null
    }
  })
  const savedItems =
    pending?.dramaId === drama.drama_id ? pending.items : undefined
  const [items, setItems] = useState<Item[]>(savedItems || []),
    [initial, setInitial] = useState<Item[] | null>(savedItems || null),
    [forbidden, setForbidden] = useState(false),
    [loading, setLoading] = useState(false),
    [progress, setProgress] = useState(0),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [offset, setOffset] = useState(0),
    [revision, setRevision] = useState(summary.revision),
    [latest, setLatest] = useState<{ revision: number; items: Item[] } | null>(
      null,
    ),
    controller = useRef(new AbortController())
  const dirty =
    !!pending ||
    (!!initial && JSON.stringify(items) !== JSON.stringify(initial))
  const paging = useCursorPage()
  const page = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "builds",
      bcId,
      summary.draft_id,
      summary.revision,
      drama.drama_id,
      "materials",
      paging.cursor,
      paging.limit,
    ],
    enabled: !initial && !loading,
    queryFn: async ({ signal }) =>
      (
        await BuildsService.materials({
          path: {
            tenant_id: tenantId,
            draft_id: summary.draft_id,
            drama_id: drama.drama_id,
          },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  useEffect(() => () => controller.current.abort(), [])
  async function startEditing() {
    const ctrl = new AbortController()
    controller.current = ctrl
    setLoading(true)
    setError(undefined)
    setProgress(0)
    await (async () => {
      try {
        const all = await loadDraftMaterials(
          tenantId,
          summary.draft_id,
          drama.drama_id,
          ctrl.signal,
          setProgress,
        )
        if (!ctrl.signal.aborted) {
          setItems(all)
          setInitial(all)
        }
      } catch (e) {
        if (!ctrl.signal.aborted) setError(e)
      } finally {
        if (!ctrl.signal.aborted) setLoading(false)
      }
    })()
  }
  const groups = () => {
    const map = new Map<number, string[]>()
    for (const item of items) {
      const group = map.get(item.group_no) || []
      group.push(item.material_id)
      map.set(item.group_no, group)
    }
    return [...map.entries()].sort(([a], [b]) => a - b).map(([, ids]) => ids)
  }
  async function save() {
    if (!write || forbidden || pending || !dirty || busy || loading || !initial)
      return
    setBusy(true)
    setError(undefined)
    try {
      const operation = {
        requestId: crypto.randomUUID(),
        kind: "groups",
        dramaId: drama.drama_id,
        items,
        prepare: false,
      }
      sessionStorage.setItem(key, JSON.stringify(operation))
      setPending(operation)
      await BuildsService.editGroups({
        path: {
          tenant_id: tenantId,
          draft_id: summary.draft_id,
          drama_id: drama.drama_id,
        },
        body: {
          request_id: operation.requestId,
          expected_revision: revision,
          groups: groups(),
        },
        signal: controller.current.signal,
      })
      if (!controller.current.signal.aborted) {
        sessionStorage.removeItem(key)
        setPending(null)
        onSaved()
      }
    } catch (e) {
      if (!unknownOutcome(e)) {
        sessionStorage.removeItem(key)
        setPending(null)
      }
      if ((e as { response?: { status: number } }).response?.status === 403)
        setForbidden(true)
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  async function compare() {
    if (busy || pending) return
    const ctrl = new AbortController()
    controller.current = ctrl
    setBusy(true)
    try {
      const { data } = await BuildsService.summary({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          signal: ctrl.signal,
        }),
        rows = await loadDraftMaterials(
          tenantId,
          summary.draft_id,
          drama.drama_id,
          ctrl.signal,
          setProgress,
        ),
        { data: check } = await BuildsService.summary({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          signal: ctrl.signal,
        })
      if (data.revision !== check.revision) throw new Error("读取期间版本变化")
      if (!ctrl.signal.aborted)
        setLatest({ revision: data.revision, items: rows })
    } catch (e) {
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  async function recover() {
    if (!pending || busy) return
    const ctrl = new AbortController()
    controller.current = ctrl
    setBusy(true)
    setError(undefined)
    try {
      await BuildsService.savedMutation({
        path: { tenant_id: tenantId, request_id: pending.requestId },
        signal: ctrl.signal,
      })
      if (!ctrl.signal.aborted) {
        sessionStorage.removeItem(key)
        setPending(null)
        onSaved()
      }
    } catch (e) {
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <ManagementSheet
      title={`调整素材 · ${drama.title}`}
      description="修改仅作用于本次草稿中的这部剧，并覆盖其全部账户目标。保存后旧预览过期。"
      dirty={dirty}
      pending={busy}
      onClose={onClose}
      actions={
        write ? (
          <Button
            disabled={
              !dirty || busy || loading || !initial || !!pending || forbidden
            }
            onClick={() => void save()}
          >
            保存素材分组
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-4">
        {!!error && <BuildError error={error} />}
        {error instanceof AxiosError && error.response?.status === 409 && (
          <div className="space-y-3">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void compare()}
            >
              查看服务器最新分组
            </Button>
            {latest && (
              <>
                <p className="text-sm">
                  服务器 v{latest.revision}，{latest.items.length}{" "}
                  份素材。下方本地分组已保留。
                </p>
                <details>
                  <summary>对比服务器分组内容</summary>
                  <Textarea
                    aria-label="服务器分组内容"
                    readOnly
                    value={latest.items
                      .map((i) => `第 ${i.group_no} 组 · ${i.file_name}`)
                      .join("\n")}
                  />
                </details>
                <Button
                  onClick={() => {
                    setRevision(latest.revision)
                    setError(undefined)
                  }}
                >
                  已核对，保留本地分组并使用此版本保存
                </Button>
              </>
            )}
          </div>
        )}
        {pending && (
          <Alert>
            <AlertDescription>
              <p>正在核实原修改结果。分组输入已保留，不会再次提交修改。</p>
              <Button disabled={busy} onClick={() => void recover()}>
                查询原修改结果
              </Button>
            </AlertDescription>
          </Alert>
        )}
        <Alert>
          <AlertDescription>
            同一素材可被多部剧命中。本次修改不会永久归剧，也不会改变其他剧目的分组。
          </AlertDescription>
        </Alert>
        {!initial && !loading && (
          <div className="space-y-3">
            {page.error && <RequestError error={page.error} />}{" "}
            {page.isPending && <p role="status">正在读取素材…</p>}
            {page.data?.items.map((item) => (
              <div key={item.material_id} className="rounded-md border p-3">
                <p className="break-all text-sm">{item.file_name}</p>
                <p className="text-xs text-muted-foreground">
                  第 {item.group_no} 组
                  {item.shared_with_other_drama
                    ? " · 此素材也匹配了其他剧目"
                    : ""}
                </p>
              </div>
            ))}
            <Pager
              paging={paging}
              nextCursor={page.data?.next_cursor}
              busy={page.isFetching}
            />
            {write && (
              <Button
                disabled={page.isPending || !!page.error}
                onClick={() => void startEditing()}
              >
                开始调整完整分组
              </Button>
            )}
            <p className="text-xs text-muted-foreground">
              进入编辑将逐页读取完整分组，加载完成前不能保存，避免覆盖未加载素材。
            </p>
          </div>
        )}
        {loading ? (
          <div>
            <p role="status">正在读取完整分组，已加载 {progress} 份素材…</p>
            <Button
              variant="outline"
              onClick={() => {
                controller.current.abort()
                setLoading(false)
              }}
            >
              取消加载
            </Button>
          </div>
        ) : (
          initial && (
            <>
              <p className="text-sm">
                {items.length} 份素材 · {groups().length}{" "}
                组。空组保存时自动移除。
              </p>
              {write && (
                <DirectoryPicker<MaterialPublic & { id: string }>
                  label="添加素材"
                  disabled={!!pending || busy || forbidden}
                  queryKey={[
                    "tenant",
                    tenantId,
                    "builds",
                    bcId,
                    "material-picker",
                  ]}
                  load={async (query, cursor, limit, signal) => {
                    const { data } = await MaterialsService.getMaterials({
                      path: { tenant_id: tenantId },
                      query: { bc_id: bcId, query, cursor, limit },
                      signal,
                    })
                    return {
                      ...data,
                      items: data.items.map((i) => ({
                        ...i,
                        id: i.material_id,
                      })),
                    }
                  }}
                  renderItem={(item) => <span>{item.file_name}</span>}
                  onSelect={(item) => {
                    if (items.some((i) => i.material_id === item.id)) return
                    setItems((old) => [
                      ...old,
                      {
                        material_id: item.id,
                        file_name: item.file_name,
                        group_no: Math.max(1, ...old.map((i) => i.group_no)),
                        position: old.length + 1,
                        shared_with_other_drama: false,
                      },
                    ])
                  }}
                />
              )}
              <div className="space-y-2">
                {items.slice(offset, offset + 50).map((item) => (
                  <div
                    key={item.material_id}
                    className="space-y-2 rounded-md border p-3"
                  >
                    <p className="break-all text-sm">{item.file_name}</p>
                    {item.shared_with_other_drama && (
                      <p className="text-xs text-muted-foreground">
                        此素材也匹配了其他剧目
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      <label
                        className="text-xs"
                        htmlFor={`group-${item.material_id}`}
                      >
                        组号
                      </label>
                      <Input
                        id={`group-${item.material_id}`}
                        type="number"
                        min={1}
                        max={100000}
                        className="w-24"
                        disabled={!write || busy || !!pending || forbidden}
                        value={item.group_no}
                        onChange={(e) => {
                          const n = Number(e.target.value)
                          if (Number.isInteger(n) && n > 0)
                            setItems((old) =>
                              old.map((i) =>
                                i.material_id === item.material_id
                                  ? { ...i, group_no: n }
                                  : i,
                              ),
                            )
                        }}
                      />
                      {write && (
                        <Button
                          variant="ghost"
                          disabled={busy || !!pending || forbidden}
                          onClick={() =>
                            setItems((old) =>
                              old.filter(
                                (i) => i.material_id !== item.material_id,
                              ),
                            )
                          }
                        >
                          移除 {item.file_name}
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
              <div className="flex items-center justify-between">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset((i) => Math.max(0, i - 50))}
                >
                  上一页素材
                </Button>
                <span className="text-xs">
                  第 {Math.floor(offset / 50) + 1} 页 · 每页 50
                </span>
                <Button
                  variant="outline"
                  disabled={offset + 50 >= items.length}
                  onClick={() => setOffset((i) => i + 50)}
                >
                  下一页素材
                </Button>
              </div>
            </>
          )
        )}
      </div>
    </ManagementSheet>
  )
}
