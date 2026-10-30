import { useState } from "react"
import { useQuery, keepPreviousData } from "@tanstack/react-query"
import type {
  DraftDramaPublic,
  ImageUnitPublic,
  Edit,
  ImageRule,
  ImageRulePatch,
} from "@/client"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Pager, RequestError } from "@/features/tenants/shared"
import { MaterialBatchPicker } from "./MaterialBatchPicker"
import { BuildGuard } from "./presentation"
import { imageApi, imagePath, change } from "./imageCompositionApi"
import { CarouselCreativeCard } from "./CarouselCreativeCard"
import { ImageRuleFields } from "./ImageRuleFields"
import { CarouselPresetList } from "@/features/materials/CarouselPresetList"
import {
  useImageCursorPage,
  type ImageEditorState,
} from "./imageWorkspaceState"
export function CarouselCreativeSheet({
  tenantId,
  bcId,
  draftId,
  drama,
  revision: serverRevision,
  generation,
  rule,
  patch,
  write,
  onPatch,
  onChanged,
  onClose,
}: {
  tenantId: string
  bcId: string
  draftId: string
  drama: DraftDramaPublic
  revision: number
  generation: number
  rule: ImageRule
  patch: ImageRulePatch
  write: boolean
  onPatch: (patch: ImageRulePatch) => void
  onChanged: () => void
  onClose: () => void
}) {
  const [mutationRevision, setMutationRevision] = useState(serverRevision)
  const revision = Math.max(serverRevision, mutationRevision)
  const scope = `${draftId}:${drama.drama_id}:${revision}:${generation}`
  const paging = useImageCursorPage(scope),
    candidatePaging = useImageCursorPage(scope),
    [editors, setEditors] = useState<Record<string, ImageEditorState>>({}),
    [leaving, setLeaving] = useState(false),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [rules, setRules] = useState(false),
    [library, setLibrary] = useState(false),
    [add, setAdd] = useState<{
      unit: ImageUnitPublic | null
      ids: string[]
      revision: number
    } | null>(null),
    [selected, setSelected] = useState<string[]>([]),
    [merge, setMerge] = useState(false),
    [reuse, setReuse] = useState(false)
  const addStale = !!add && add.revision !== revision
  const dirty = Object.keys(editors).length > 0
  function setEditor(id: string, value: ImageEditorState | null) {
    setEditors((current) => {
      const next = { ...current }
      if (value) next[id] = value
      else delete next[id]
      return next
    })
  }
  function changed(nextRevision?: number) {
    if (nextRevision) setMutationRevision(nextRevision)
    onChanged()
  }
  const path = imagePath(tenantId, draftId)
  const units = useQuery({
    queryKey: [
      "image-units",
      tenantId,
      draftId,
      drama.drama_id,
      revision,
      generation,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await imageApi.units({
          path,
          query: {
            drama_id: drama.drama_id,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    placeholderData: keepPreviousData,
    enabled: !busy,
    refetchInterval: 2000,
  })
  const songs = useQuery({
    queryKey: [
      "image-music-metadata",
      tenantId,
      draftId,
      units.data?.items.map((u) => `${u.id}:${u.music_revision}`).join(","),
    ],
    enabled: !!units.data?.items.some((u) => u.music_id),
    queryFn: async ({ signal }) =>
      (
        await imageApi.readMusic({
          path,
          body: {
            music_ids: [
              ...new Set(
                units.data!.items.flatMap((u) =>
                  u.music_id ? [u.music_id] : [],
                ),
              ),
            ],
          },
          signal,
        })
      ).data,
    retry: false,
    staleTime: 30000,
  })
  const candidates = useQuery({
    queryKey: [
      "image-candidates",
      tenantId,
      draftId,
      drama.drama_id,
      revision,
      generation,
      candidatePaging.cursor,
      candidatePaging.limit,
    ],
    enabled: !!add && !busy,
    queryFn: async ({ signal }) =>
      (
        await imageApi.candidates({
          path,
          query: {
            drama_id: drama.drama_id,
            cursor: candidatePaging.cursor,
            limit: candidatePaging.limit,
          },
          signal,
        })
      ).data,
  })
  async function edit(command: Edit["command"], expectedRevision = revision) {
    setBusy(true)
    try {
      const result = await imageApi.edit({
        path,
        body: { ...change(expectedRevision), command },
      })
      changed(result.data.revision)
      return result.data.revision
    } catch (e) {
      setError(e)
      throw e
    } finally {
      setBusy(false)
    }
  }
  function close() {
    if (dirty) setLeaving(true)
    else onClose()
  }
  const effective = { ...rule, ...patch } as ImageRule
  return (
    <>
      <BuildGuard
        dirty={dirty}
        title="有未保存图片调整"
        leaveLabel="放弃未保存调整"
      />
      <Sheet
        open
        onOpenChange={(open) => {
          if (!open) close()
        }}
      >
        <SheetContent className="flex w-full flex-col overflow-y-auto sm:max-w-3xl">
          <SheetHeader>
            <SheetTitle>{drama.title} · 查看与调整轮播</SheetTitle>
            <SheetDescription>
              图片调整需单独保存；完成只关闭侧栏。
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-4 p-4">
            {(error || units.error) && (
              <RequestError error={error || units.error} />
            )}
            <div className="flex flex-wrap gap-2">
              <MaterialBatchPicker
                tenantId={tenantId}
                mediaType="IMAGE"
                existingIds={[]}
                existingContentKeys={[]}
                disabled={!write || busy}
                onAdd={async (items) => {
                  setBusy(true)
                  try {
                    const result = await imageApi.addCandidates({
                      path,
                      body: {
                        ...change(revision),
                        drama_id: drama.drama_id,
                        material_ids: items.map((i) => i.material_id),
                      },
                    })
                    changed(result.data.revision)
                  } catch (e) {
                    setError(e)
                  } finally {
                    setBusy(false)
                  }
                }}
              />
              <Button
                variant="outline"
                disabled={!write || busy}
                onClick={() => setAdd({ unit: null, ids: [], revision })}
              >
                手工新建轮播
              </Button>
              <Button variant="outline" onClick={() => setRules(!rules)}>
                本剧规则
              </Button>
              <Button
                variant="outline"
                disabled={!write || busy}
                onClick={() => setLibrary(!library)}
              >
                从轮播库添加
              </Button>
              <Button
                variant="outline"
                disabled={!write || selected.length < 2 || busy}
                onClick={() => setMerge(true)}
              >
                合并选中轮播
              </Button>
            </div>
            {rules && (
              <>
                <p>仅修改的字段覆盖整批；恢复后仍需查看生成结果并应用。</p>
                <ImageRuleFields
                  tenantId={tenantId}
                  draftId={draftId}
                  value={effective}
                  disabled={!write}
                  onChange={(next) => {
                    const changes: ImageRulePatch = { ...patch }
                    for (const key of Object.keys(next) as (keyof ImageRule)[])
                      if (next[key] !== effective[key])
                        Object.assign(changes, { [key]: next[key] })
                    onPatch(changes)
                  }}
                />
                <Button
                  variant="outline"
                  disabled={!write}
                  onClick={() => onPatch({})}
                >
                  恢复整批配置
                </Button>
              </>
            )}
            {library && (
              <CarouselPresetList
                tenantId={tenantId}
                write={write}
                destination={{ draftId, dramaId: drama.drama_id, revision }}
                onLoaded={(nextRevision) => {
                  changed(nextRevision)
                  setLibrary(false)
                }}
              />
            )}
            {units.data?.items.map((unit) => (
              <div key={unit.id} className="flex flex-col gap-2">
                <FieldGroup>
                  <Field orientation="horizontal">
                    <Checkbox
                      id={`select-${unit.id}`}
                      checked={selected.includes(unit.id)}
                      onCheckedChange={(v) =>
                        setSelected(
                          v
                            ? [...selected, unit.id]
                            : selected.filter((id) => id !== unit.id),
                        )
                      }
                    />
                    <FieldLabel htmlFor={`select-${unit.id}`}>
                      选择创意 {unit.position}
                    </FieldLabel>
                  </Field>
                </FieldGroup>
                <CarouselCreativeCard
                  bcId={bcId}
                  tenantId={tenantId}
                  draftId={draftId}
                  revision={revision}
                  unit={{
                    ...unit,
                    music_snapshot: {
                      ...unit.music_snapshot,
                      ...songs.data?.items.find(
                        (song) => song.music_id === unit.music_id,
                      ),
                    },
                  }}
                  write={write && !busy && !units.isPlaceholderData}
                  recommend={effective.music_mode !== "FIXED"}
                  onEdit={edit}
                  editor={editors[unit.id]}
                  onEditor={(value) => setEditor(unit.id, value)}
                  onMusicChanged={changed}
                  onAdd={(unit, ids, expectedRevision) => {
                    setAdd({ unit, ids, revision: expectedRevision })
                    setReuse(false)
                  }}
                />
              </div>
            ))}
            <Pager
              paging={paging}
              nextCursor={units.data?.next_cursor}
              total={units.data?.total}
              busy={units.isFetching}
            />
            <Button variant="outline" onClick={close}>
              完成
            </Button>
          </div>
          {leaving && (
            <Dialog open onOpenChange={setLeaving}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>有未保存图片调整</DialogTitle>
                  <DialogDescription>
                    离开会放弃卡片中尚未保存的调整。
                  </DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setLeaving(false)}>
                    继续编辑
                  </Button>
                  <Button onClick={onClose}>放弃未保存调整</Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
          {merge && (
            <Dialog open onOpenChange={setMerge}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>确认合并轮播</DialogTitle>
                  <DialogDescription>
                    按所选顺序合并，图片不能重复，最多 35
                    张。新创意需配乐，音乐保护需先解除。
                  </DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setMerge(false)}>
                    取消
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={async () => {
                      try {
                        await edit({ kind: "MERGE", unit_ids: selected })
                      } catch {
                        return
                      }
                      setMerge(false)
                      setSelected([])
                    }}
                  >
                    确认合并
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
          {add && (
            <Dialog
              open
              onOpenChange={(v) => {
                if (!v) setAdd(null)
              }}
            >
              <DialogContent className="flex max-h-[90dvh] flex-col">
                <DialogHeader className="shrink-0">
                  <DialogTitle>添加本剧候选图片</DialogTitle>
                  <DialogDescription>
                    保持已选顺序；每个创意不重复，最多 35 张。
                  </DialogDescription>
                </DialogHeader>
                {candidates.error && <RequestError error={candidates.error} />}
                {addStale && (
                  <Alert>
                    <AlertDescription>
                      草稿已更新，请取消本次添加并核对最新内容后再编辑。
                    </AlertDescription>
                  </Alert>
                )}
                <FieldGroup className="min-h-0 overflow-y-auto">
                  {candidates.data?.items.map((c) => (
                    <Field orientation="horizontal" key={c.material_id}>
                      <Checkbox
                        id={`candidate-${c.material_id}`}
                        disabled={
                          addStale ||
                          busy ||
                          !c.enabled ||
                          (!add.ids.includes(c.material_id) &&
                            add.ids.length >= 35)
                        }
                        checked={add.ids.includes(c.material_id)}
                        onCheckedChange={(v) =>
                          setAdd({
                            ...add,
                            ids: v
                              ? [...add.ids, c.material_id]
                              : add.ids.filter((id) => id !== c.material_id),
                          })
                        }
                      />
                      <FieldLabel htmlFor={`candidate-${c.material_id}`}>
                        {c.file_name_snapshot}
                        {!c.enabled && " · 已排除"}
                      </FieldLabel>
                      {!c.enabled && (
                        <Button
                          variant="outline"
                          disabled={busy || addStale}
                          onClick={async () => {
                            if (addStale) return
                            try {
                              const nextRevision = await edit(
                                {
                                  kind: "RESTORE_CANDIDATES",
                                  drama_id: drama.drama_id,
                                  material_ids: [c.material_id],
                                },
                                add.revision,
                              )
                              // 本弹窗自己的恢复操作可推进版本，外部更新仍由 CAS 拦截。
                              setAdd(
                                (current) =>
                                  current && {
                                    ...current,
                                    revision: nextRevision,
                                  },
                              )
                            } catch {
                              return
                            }
                          }}
                        >
                          恢复本次使用
                        </Button>
                      )}
                    </Field>
                  ))}
                </FieldGroup>
                <Field orientation="horizontal" className="shrink-0">
                  <Checkbox
                    id="allow-image-reuse"
                    checked={reuse}
                    onCheckedChange={(v) => setReuse(!!v)}
                  />
                  <FieldLabel htmlFor="allow-image-reuse">
                    明确允许复用其他创意中的图片
                  </FieldLabel>
                </Field>
                <Pager
                  paging={candidatePaging}
                  nextCursor={candidates.data?.next_cursor}
                  total={candidates.data?.total}
                  busy={candidates.isFetching}
                />
                <DialogFooter>
                  <Button variant="outline" onClick={() => setAdd(null)}>
                    取消
                  </Button>
                  <Button
                    disabled={busy || addStale || !add.ids.length}
                    onClick={async () => {
                      if (addStale) return
                      try {
                        await edit(
                          add.unit
                            ? {
                                kind: "SET_IMAGES",
                                unit_id: add.unit.id,
                                material_ids: add.ids,
                                allow_reuse: reuse,
                              }
                            : {
                                kind: "CREATE",
                                drama_id: drama.drama_id,
                                material_ids: add.ids,
                                allow_reuse: reuse,
                              },
                          add.revision,
                        )
                        if (add.unit) setEditor(add.unit.id, null)
                      } catch {
                        return
                      }
                      setAdd(null)
                    }}
                  >
                    保存图片调整
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
        </SheetContent>
      </Sheet>
    </>
  )
}
