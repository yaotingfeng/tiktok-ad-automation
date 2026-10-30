import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import type { CompositionPlanPublic, ImageDifference } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { FilterSelect } from "@/features/accounts/presentation"
import { Pager, RequestError, useCursorPage } from "@/features/tenants/shared"
import { imageApi, imagePath, musicLabel, change } from "./imageCompositionApi"
export function ImageCompositionDiff({
  tenantId,
  draftId,
  plan,
  busy,
  onClose,
  onApply,
  onReplace,
  onResolve,
}: {
  tenantId: string
  draftId: string
  plan: CompositionPlanPublic
  busy: boolean
  onClose: () => void
  onApply: () => void
  onReplace: () => void
  onResolve: () => void
}) {
  const paging = useCursorPage(),
    [detail, setDetail] = useState(false)
  const diffPaging = useCursorPage()
  const [kind, setKind] = useState<
    | "conflicts"
    | "excluded_images"
    | "warnings"
    | "removed_ids"
    | "retained_ids"
    | "protection_losses"
  >(plan.mode === "REPLACE_ALL" ? "protection_losses" : "conflicts")
  const [acknowledged, setAcknowledged] = useState(false)
  const lossCount = plan.dramas.reduce(
    (sum, drama) => sum + drama.protection_losses_count,
    0,
  )
  const [error, setError] = useState<unknown>(),
    [resolving, setResolving] = useState(false)
  const differences = useQuery({
    queryKey: [
      "image-differences",
      plan.plan_id,
      kind,
      diffPaging.cursor,
      diffPaging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await imageApi.differences({
          path: { ...imagePath(tenantId, draftId), plan_id: plan.plan_id },
          query: { kind, cursor: diffPaging.cursor, limit: diffPaging.limit },
          signal,
        })
      ).data,
  })
  async function resolve(conflict: ImageDifference) {
    setResolving(true)
    try {
      if (conflict.code === "CROSS_DRAMA_IMAGE" && conflict.material_id)
        await imageApi.allocate({
          path: imagePath(tenantId, draftId),
          body: {
            ...change(plan.base_revision),
            material_id: conflict.material_id,
            drama_ids: conflict.drama_ids ?? [],
          },
        })
      else if (conflict.unit_id)
        await imageApi.edit({
          path: imagePath(tenantId, draftId),
          body: {
            ...change(plan.base_revision),
            command: {
              kind: "SET_PROTECTION",
              unit_id: conflict.unit_id,
              ...(conflict.code === "PROTECTED_MUSIC_COMPOSITION_CHANGED"
                ? { music_protected: false }
                : { composition_protected: false }),
            },
          },
        })
      onResolve()
    } catch (e) {
      setError(e)
    } finally {
      setResolving(false)
    }
  }
  const units = useQuery({
    queryKey: ["image-plan", plan.plan_id, paging.cursor, paging.limit],
    queryFn: async ({ signal }) =>
      (
        await imageApi.planUnits({
          path: { ...imagePath(tenantId, draftId), plan_id: plan.plan_id },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose()
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>查看生成结果</DialogTitle>
          <DialogDescription>
            试算结果尚未核验广告数量；应用后只更新轮播和配乐，不创建广告。
          </DialogDescription>
        </DialogHeader>
        <p>
          共 {plan.unit_count} 个创意 · 新增 {plan.added_unit_count} · 保留{" "}
          {plan.retained_unit_count} · 移除 {plan.removed_unit_count}
        </p>
        <p>
          保留手动保护 {plan.protected_unit_count} 项 · 待选音乐{" "}
          {plan.music_counts.NEEDS_SELECTION ?? 0} 项
        </p>
        {plan.mode === "PRESERVE_MANUAL" ? (
          <Button variant="outline" disabled={busy} onClick={onReplace}>
            同时替换手工调整
          </Button>
        ) : (
          <Alert>
            <AlertDescription>
              已选择替换手工调整。请核对将失去的图片与音乐保护后再应用。
            </AlertDescription>
          </Alert>
        )}
        {plan.conflict_count > 0 && (
          <Alert>
            <AlertDescription>
              有 {plan.conflict_count} 项冲突，处理后请重新查看生成结果。
            </AlertDescription>
          </Alert>
        )}
        <FilterSelect
          allLabel="待处理冲突"
          label="差异明细"
          value={kind}
          onChange={(v) => {
            setKind(v === "all" ? "conflicts" : (v as typeof kind))
            diffPaging.reset()
          }}
          choices={{
            conflicts: "待处理冲突",
            excluded_images: "本次不使用图片",
            warnings: "提示",
            removed_ids: "移除轮播",
            retained_ids: "保留轮播",
            protection_losses: "将失去的保护",
          }}
        />
        {!!(error || differences.error) && (
          <RequestError error={error || differences.error} />
        )}
        {differences.data?.items.map((conflict, i) => (
          <div key={i} className="flex flex-col gap-2 rounded-md border p-3">
            <strong>
              {conflict.drama_title ||
                conflict.drama_titles?.join("、") ||
                "剧目名称未记录"}
              {conflict.unit_position
                ? ` · 创意 ${conflict.unit_position}`
                : ""}
            </strong>
            {!!conflict.materials?.length && (
              <ul className="list-inside list-disc text-sm">
                {conflict.materials.map((material) => (
                  <li key={material.material_id}>{material.file_name}</li>
                ))}
              </ul>
            )}
            {conflict.music_title && <p>已选曲目：{conflict.music_title}</p>}
            {kind === "protection_losses" && (
              <p>
                将失去
                {[
                  conflict.composition_protected && "图片保护",
                  conflict.music_protected && "音乐保护",
                ]
                  .filter(Boolean)
                  .join("、") || "手工保护"}
              </p>
            )}
            <span>
              {conflict.code === "PROTECTED_MUSIC_COMPOSITION_CHANGED"
                ? "已保护音乐的图片组合将改变"
                : conflict.code === "CROSS_DRAMA_IMAGE"
                  ? "图片命中多部剧"
                  : conflict.code === "NO_IMAGES"
                    ? "本剧没有可用图片，请返回补充或恢复候选"
                    : conflict.reason === "REMAINDER"
                      ? "尾部图片本次不使用"
                      : conflict.reason === "DISABLED"
                        ? "已明确排除图片"
                        : kind === "protection_losses"
                          ? "此手工修改将被替换"
                          : "轮播或图片差异"}
            </span>
            {((conflict.unit_id && conflict.code) ||
              conflict.code === "CROSS_DRAMA_IMAGE") && (
              <Button
                variant="outline"
                disabled={busy || resolving}
                onClick={() => void resolve(conflict)}
              >
                {conflict.code === "CROSS_DRAMA_IMAGE"
                  ? `确认允许 ${conflict.drama_ids?.length ?? 0} 部剧复用此图`
                  : conflict.code === "PROTECTED_MUSIC_COMPOSITION_CHANGED"
                    ? "确认解除音乐保护"
                    : "确认解除图片保护"}
              </Button>
            )}
          </div>
        ))}
        <Pager
          paging={diffPaging}
          total={differences.data?.total}
          nextCursor={differences.data?.next_cursor}
          busy={differences.isFetching}
        />
        <Button variant="ghost" onClick={() => setDetail(!detail)}>
          {detail ? "收起" : "查看"}逐剧差异
        </Button>
        {detail &&
          plan.dramas.map((d) => (
            <p key={d.drama_id}>
              {d.drama_title} · 保留 {d.retained_ids_count} · 移除{" "}
              {d.removed_ids_count} · 不使用图片 {d.excluded_images_count} ·
              失去保护 {d.protection_losses_count}
            </p>
          ))}
        {units.error && <RequestError error={units.error} />}
        <div className="flex flex-col gap-2">
          {units.data?.items.map((u) => (
            <p key={u.id}>
              {u.drama_title || "剧目名称未记录"} · 创意 {u.position} ·{" "}
              {u.ordered_material_ids.length} 张 ·{" "}
              {musicLabel[u.music_status ?? "PENDING"]}
              {u.composition_protected && " · 图片已保护"}
            </p>
          ))}
        </div>
        <Pager
          paging={paging}
          total={units.data?.total}
          nextCursor={units.data?.next_cursor}
          busy={units.isFetching}
        />
        {plan.mode === "REPLACE_ALL" && lossCount > 0 && (
          <Field orientation="horizontal">
            <Checkbox
              id="ack-protection-loss"
              checked={acknowledged}
              onCheckedChange={(v) => setAcknowledged(!!v)}
            />
            <FieldLabel htmlFor="ack-protection-loss">
              我已核对所列保护损失
            </FieldLabel>
          </Field>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            返回修改
          </Button>
          <Button
            disabled={
              busy ||
              plan.conflict_count > 0 ||
              (plan.mode === "REPLACE_ALL" &&
                lossCount > 0 &&
                (!acknowledged ||
                  differences.isFetching ||
                  !!differences.error))
            }
            onClick={onApply}
          >
            应用并生成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
