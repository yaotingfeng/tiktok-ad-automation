import { useEffect, useRef, useState } from "react"
import { type DraftInputPublic, ProvidersService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  BuildError,
  BuildReason,
  reportError,
  unknownOutcome,
} from "./presentation"

export function canChooseDrama(input: DraftInputPublic) {
  const progress = input.preparation
  return (
    progress?.link_status === "needs_resolution" &&
    !!progress.provider_input_id &&
    !!progress.candidates?.length
  )
}

export function DramaInputSheet({
  tenantId,
  input,
  write,
  onClose,
  onChanged,
  onRefresh,
}: {
  tenantId: string
  input: DraftInputPublic
  write: boolean
  onClose: () => void
  onChanged: () => void
  onRefresh: () => Promise<unknown>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const [unknown, setUnknown] = useState(
    () =>
      !!sessionStorage.getItem(
        `build-candidate:${tenantId}:${input.preparation?.provider_input_id}`,
      ),
  )
  const controller = useRef(new AbortController())
  useEffect(() => {
    const ctrl = new AbortController()
    controller.current = ctrl
    return () => ctrl.abort()
  }, [])
  const progress = input.preparation
  const selectable = canChooseDrama(input)
  async function choose(externalId: string) {
    if (
      !write ||
      !selectable ||
      busy ||
      unknown ||
      !progress?.provider_input_id
    )
      return
    const key = `build-candidate:${tenantId}:${progress.provider_input_id}`
    setBusy(true)
    setError(undefined)
    // 沿用原输入任务的防重记录，响应不明时只刷新状态，不重发选择。
    sessionStorage.setItem(key, externalId)
    try {
      await ProvidersService.postCandidate({
        path: { tenant_id: tenantId, input_id: progress.provider_input_id },
        body: { external_drama_id: externalId },
        signal: controller.current.signal,
      })
      sessionStorage.removeItem(key)
      onClose()
      onChanged()
    } catch (e) {
      if (controller.current.signal.aborted) return
      if (unknownOutcome(e)) setUnknown(true)
      else sessionStorage.removeItem(key)
      reportError(e)
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <ManagementSheet
      title={write && selectable ? "选择剧目" : "输入详情"}
      description={`第 ${input.line_no} 行的原始输入与解析结果。`}
      dirty={false}
      pending={busy}
      onClose={onClose}
    >
      <div className="flex flex-col gap-4 text-sm">
        <div>
          <p className="text-muted-foreground">原始输入</p>
          <p className="whitespace-pre-wrap break-all">
            {input.raw_text || "（空行）"}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">剧目名称</p>
          <p>{progress?.title || "尚未确定"}</p>
        </div>
        <div>
          <p className="text-muted-foreground">版权方剧目 ID</p>
          <p className="break-all">
            {progress?.external_drama_id || "尚未确定"}
          </p>
        </div>
        <BuildReason code={progress?.reason_code || input.reason_code} />
        {input.duplicate_of != null && (
          <p>重复输入，合并到第 {input.duplicate_of} 行。</p>
        )}
        {!!error && <BuildError error={error} />}
        {unknown && selectable && (
          <Alert>
            <AlertDescription>
              选择结果尚待核实，正在刷新状态，请勿重复选择。
            </AlertDescription>
          </Alert>
        )}
        {selectable && write && (
          <p>该名称匹配到多个结果，请选择本次要投放的剧目。</p>
        )}
        {selectable &&
          write &&
          progress?.candidates?.map((candidate) => (
            <Button
              key={candidate.external_drama_id}
              variant="outline"
              className="h-auto justify-start whitespace-normal text-left"
              disabled={busy || unknown}
              onClick={() => void choose(candidate.external_drama_id)}
            >
              {candidate.title} · {candidate.external_drama_id}
              {candidate.language ? ` · ${candidate.language}` : ""}
            </Button>
          ))}
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => void onRefresh()}
        >
          刷新解析结果
        </Button>
      </div>
    </ManagementSheet>
  )
}
