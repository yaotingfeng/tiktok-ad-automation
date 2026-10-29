import { useState, useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import type {
  CompositionPlanPublic,
  CompositionRunPublic,
  DraftDramaPublic,
  DraftSummary,
  ImageRule,
  ImageRulePatch,
} from "@/client"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  CardFooter,
} from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { BuildGuard } from "./presentation"
import { RequestError } from "@/features/tenants/shared"
import {
  imageApi,
  imagePath,
  imageKey,
  imageRuleDefault,
  change,
} from "./imageCompositionApi"
import { ImageRuleFields } from "./ImageRuleFields"
import { ImageStructureOverride } from "./ImageStructureOverride"
import { ImageCompositionDiff } from "./ImageCompositionDiff"
import { ImageCandidateRefresh } from "./ImageCandidateRefresh"
import { CarouselCreativeSheet } from "./CarouselCreativeSheet"
import { ImageDramaBatchActions } from "./ImageDramaBatchActions"
export function ImageBatchConfig({
  tenantId,
  summary,
  write,
  drama,
  selectedDramas,
  onCloseDrama,
  onChanged,
}: {
  tenantId: string
  summary: DraftSummary
  write: boolean
  selectedDramas: Record<string, string>
  drama: DraftDramaPublic | null
  onCloseDrama: () => void
  onChanged: () => void
}) {
  const draftId = summary.draft_id,
    path = imagePath(tenantId, draftId),
    [localRule, setLocalRule] = useState<ImageRule | null>(null),
    [localPatches, setLocalPatches] = useState<Record<
      string,
      ImageRulePatch
    > | null>(null),
    [plan, setPlan] = useState<CompositionPlanPublic | null>(null),
    [run, setRun] = useState<CompositionRunPublic | null>(null),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false)
  const state = useQuery({
    queryKey: imageKey(tenantId, draftId),
    queryFn: async ({ signal }) =>
      (await imageApi.compositionState({ path, signal })).data,
    refetchInterval: 2000,
  })
  const runQuery = useQuery({
    queryKey: [...imageKey(tenantId, draftId), "run", run?.run_id],
    enabled: !!run && run.status === "PENDING",
    queryFn: async ({ signal }) =>
      (
        await imageApi.progress({
          path: { ...path, run_id: run!.run_id },
          signal,
        })
      ).data,
    refetchInterval: (q) => (q.state.data?.status === "PENDING" ? 1500 : false),
  })
  useEffect(() => {
    if (runQuery.data && runQuery.data.status !== "PENDING") {
      setRun(runQuery.data)
      void state.refetch()
      onChanged()
    }
  }, [runQuery.data])
  const current = state.data,
    rule = localRule ?? { ...imageRuleDefault, ...current?.rule },
    patches = localPatches ?? current?.drama_patches ?? {},
    activeRun = current?.run ?? runQuery.data ?? run,
    working = activeRun?.status === "PENDING",
    disabled = !write || busy || working || !current
  const changed = () => {
    void state.refetch()
    onChanged()
  }
  async function trial(
    mode: "PRESERVE_MANUAL" | "REPLACE_ALL" = "PRESERVE_MANUAL",
  ) {
    if (!current) return
    setBusy(true)
    setError(undefined)
    try {
      const result = await imageApi.preview({
        path,
        body: {
          ...change(current.revision),
          rule,
          drama_patches: patches,
          mode,
        },
      })
      setPlan(result.data)
      setLocalRule(null)
      setLocalPatches(null)
      changed()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  async function apply() {
    if (!plan) return
    setBusy(true)
    try {
      const result = await imageApi.apply({
        path,
        body: { ...change(plan.base_revision), plan_id: plan.plan_id },
      })
      setRun(result.data)
      setPlan(null)
      changed()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const unapplied =
    !!localRule ||
    !!localPatches ||
    current?.applied_rule_revision !== current?.revision
  return (
    <>
      <BuildGuard
        dirty={!!localRule || !!localPatches}
        title="有尚未查看生成结果的图片规则"
      />
      <Card>
        <CardHeader>
          <CardTitle>整批图片规则</CardTitle>
          <CardDescription>
            每部剧独立组合。规则生成与选曲只保存草稿，不创建广告。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {(error || state.error) && (
            <RequestError
              error={error || state.error}
              retry={() => void state.refetch()}
            />
          )}
          <ImageRuleFields
            tenantId={tenantId}
            draftId={draftId}
            value={rule}
            onChange={setLocalRule}
            disabled={disabled}
          />
          <ImageStructureOverride
            tenantId={tenantId}
            summary={{
              ...summary,
              revision: current?.revision ?? summary.revision,
            }}
            disabled={disabled}
            onSaved={changed}
          />
          <ImageCandidateRefresh
            tenantId={tenantId}
            draftId={draftId}
            revision={current?.revision ?? summary.revision}
            progress={current?.candidate_build ?? null}
            disabled={disabled}
            onChanged={changed}
          />
          {unapplied && (
            <Alert>
              <AlertDescription>
                有规则尚未应用，已有轮播保持原样。
              </AlertDescription>
            </Alert>
          )}
          {activeRun && (
            <Alert>
              <AlertDescription>
                {working
                  ? `生成中 ${activeRun.processed}/${activeRun.total}`
                  : activeRun.status === "FAILED"
                    ? "生成失败，原结果保留，请重新查看生成结果后重试。"
                    : activeRun.status === "CANCELLED"
                      ? "已取消生成，原结果保留。"
                      : "轮播已生成，正在准备音乐。"}
                {working && (
                  <Button
                    variant="outline"
                    disabled={busy || !write}
                    onClick={async () => {
                      try {
                        const result = await imageApi.cancel({
                          path: { ...path, run_id: activeRun.run_id },
                          body: change(current!.revision),
                        })
                        setRun(result.data)
                        changed()
                      } catch (e) {
                        setError(e)
                      }
                    }}
                  >
                    取消生成
                  </Button>
                )}
              </AlertDescription>
            </Alert>
          )}
          <p role="status">
            已选音乐 {current?.music_counts.READY ?? 0} · 待选音乐{" "}
            {current?.music_counts.NEEDS_SELECTION ?? 0} · 配乐中{" "}
            {(current?.music_counts.PENDING ?? 0) +
              (current?.music_counts.RUNNING ?? 0)}{" "}
            · 配乐失败 {current?.music_counts.FAILED ?? 0}
          </p>
          <p className="text-sm text-muted-foreground">
            请从下方剧目行「查看与调整轮播」处理待选音乐与手动调整。图片投放预览尚待完整规划与目标资格核验。
          </p>
        </CardContent>
        <CardFooter>
          <Button
            disabled={
              disabled ||
              !current?.snapshot_id ||
              rule.images_per_unit! < 1 ||
              rule.images_per_unit! > 35
            }
            onClick={() => void trial()}
          >
            查看生成结果
          </Button>
        </CardFooter>
      </Card>
      <ImageDramaBatchActions
        tenantId={tenantId}
        draftId={draftId}
        revision={current?.revision ?? summary.revision}
        selected={selectedDramas}
        rule={rule}
        disabled={disabled}
        onChanged={changed}
        onPatch={(patch) => {
          const next = { ...patches }
          for (const id of Object.keys(selectedDramas)) {
            if (patch === null) delete next[id]
            else next[id] = { ...next[id], ...patch }
          }
          setLocalPatches(next)
        }}
      />
      {plan && (
        <ImageCompositionDiff
          key={plan.plan_id}
          tenantId={tenantId}
          draftId={draftId}
          plan={plan}
          busy={busy}
          onClose={() => setPlan(null)}
          onApply={() => void apply()}
          onReplace={() => void trial("REPLACE_ALL")}
          onResolve={() => {
            setPlan(null)
            changed()
          }}
        />
      )}
      {drama && current && (
        <CarouselCreativeSheet
          tenantId={tenantId}
          draftId={draftId}
          drama={drama}
          revision={current.revision}
          generation={current.active_generation}
          rule={rule}
          patch={patches[drama.drama_id] ?? {}}
          write={!disabled}
          onPatch={(patch) => {
            const next = { ...patches }
            if (Object.keys(patch).length) next[drama.drama_id] = patch
            else delete next[drama.drama_id]
            setLocalPatches(next)
          }}
          onChanged={changed}
          onClose={onCloseDrama}
        />
      )}
    </>
  )
}
