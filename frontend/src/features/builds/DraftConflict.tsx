import { useEffect, useRef, useState } from "react"
import { BuildsService, type DraftSummary } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { loadDraftInputs } from "./api"
import { BuildError } from "./presentation"
export function DraftConflict({
  tenantId,
  draftId,
  onApply,
}: {
  tenantId: string
  draftId: string
  onApply: (revision: number) => void
}) {
  const [latest, setLatest] = useState<{
      summary: DraftSummary
      drama: string[]
      account: string[]
    } | null>(null),
    [busy, setBusy] = useState(false),
    [count, setCount] = useState(0),
    [error, setError] = useState<unknown>(),
    controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  async function read() {
    controller.current?.abort()
    const ctrl = new AbortController()
    controller.current = ctrl
    setBusy(true)
    setError(undefined)
    setCount(0)
    try {
      const { data } = await BuildsService.summary({
          path: { tenant_id: tenantId, draft_id: draftId },
          signal: ctrl.signal,
        }),
        inputs = await loadDraftInputs(
          tenantId,
          draftId,
          ctrl.signal,
          setCount,
        ),
        { data: check } = await BuildsService.summary({
          path: { tenant_id: tenantId, draft_id: draftId },
          signal: ctrl.signal,
        })
      if (data.revision !== check.revision)
        throw new Error("草稿读取期间再次发生修改")
      if (!ctrl.signal.aborted) setLatest({ summary: data, ...inputs })
    } catch (e) {
      if (!ctrl.signal.aborted) setError(e)
    } finally {
      if (!ctrl.signal.aborted) setBusy(false)
    }
  }
  return (
    <Alert>
      <AlertDescription>
        <p>
          本地输入保留在下方。读取最新草稿后核对两份内容；应用本地输入会覆盖所展示服务器版本的输入配置。
        </p>
        {!!error && <BuildError error={error} />}
        <Button variant="outline" disabled={busy} onClick={() => void read()}>
          {busy ? `正在读取最新草稿，已加载 ${count} 行…` : "查看最新草稿"}
        </Button>
        {busy && (
          <Button
            variant="outline"
            onClick={() => {
              controller.current?.abort()
              setBusy(false)
            }}
          >
            取消读取最新草稿
          </Button>
        )}
        {latest && (
          <div className="space-y-3">
            <p>
              服务器版本 v{latest.summary.revision} · 策略版本{" "}
              {latest.summary.strategy_version_id} · 应用{" "}
              {latest.summary.application_id}
            </p>
            <details>
              <summary>查看服务器原始输入</summary>
              <Textarea
                aria-label="服务器剧目输入"
                readOnly
                value={latest.drama.join("\n")}
              />
              <Textarea
                aria-label="服务器账户输入"
                readOnly
                value={latest.account.join("\n")}
              />
            </details>
            <Button onClick={() => onApply(latest.summary.revision)}>
              已核对，保留本地输入并使用此版本保存
            </Button>
          </div>
        )}
      </AlertDescription>
    </Alert>
  )
}
