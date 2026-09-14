import { useEffect, useRef, useState } from "react"
import {
  BuildsService,
  type DraftInputPublic,
  type DraftSummary,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { mutationKey } from "./api"
import { validMinisUrl } from "./manualLinks"
import { preparationLabel } from "./preparationProgress"
import { BuildError, reportError, unknownOutcome } from "./presentation"

export function ManualLinkSheet({
  tenantId,
  summary,
  input,
  onClose,
  onSaved,
}: {
  tenantId: string
  summary: DraftSummary
  input: DraftInputPublic
  onClose: () => void
  onSaved: () => void
}) {
  const initial = useRef({
    url: String(input.manual_link?.url || input.preparation?.url || ""),
    external_drama_id: String(
      input.preparation?.display_drama_id ||
        input.manual_link?.external_drama_id ||
        "",
    ),
    protected_base: String(
      input.manual_link?.protected_base ||
        input.preparation?.protected_base ||
        "",
    ),
  })
  const [values, setValues] = useState(initial.current)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const [validation, setValidation] = useState("")
  const [unknownId, setUnknownId] = useState<string | null>(null)
  const controller = useRef(new AbortController())
  useEffect(() => {
    const c = new AbortController()
    controller.current = c
    return () => c.abort()
  }, [])
  const key = mutationKey(tenantId, summary.bc_id, summary.draft_id)
  const preparing = summary.status === "PREPARING"
  async function save() {
    if (busy || (preparing && !unknownId)) return
    if (
      !unknownId &&
      (!validMinisUrl(values.url) ||
        (summary.provider_kind === "wangyan" && !values.protected_base.trim()))
    ) {
      setValidation(
        !validMinisUrl(values.url)
          ? "请填写完整的 HTTPS TikTok Minis 推广链接"
          : "请填写网眼提供的归因名称",
      )
      return
    }
    const requestId = unknownId || crypto.randomUUID()
    setBusy(true)
    setError(undefined)
    try {
      if (unknownId) {
        const { data } = await BuildsService.savedMutation({
          path: { tenant_id: tenantId, request_id: requestId },
          signal: controller.current.signal,
        })
        if (data.draft_id !== summary.draft_id)
          throw new Error("保存结果不属于当前草稿")
      } else {
        // 与原有草稿修改共用回执恢复入口；响应未知时不重复发起新修改。
        sessionStorage.setItem(
          key,
          JSON.stringify({ requestId, kind: "manual_link", prepare: true }),
        )
        await BuildsService.putManualLink({
          path: {
            tenant_id: tenantId,
            draft_id: summary.draft_id,
            input_id: input.id,
          },
          body: {
            request_id: requestId,
            expected_revision: summary.revision,
            link: { line_no: input.line_no, ...values },
          },
          signal: controller.current.signal,
        })
      }
      if (controller.current.signal.aborted) return
      sessionStorage.removeItem(key)
      onClose()
      onSaved()
    } catch (e) {
      if (controller.current.signal.aborted) return
      if (unknownOutcome(e)) setUnknownId(requestId)
      else if (!unknownId) sessionStorage.removeItem(key)
      setError(e)
      reportError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <ManagementSheet
      title={initial.current.url ? "修改推广链接" : "补充推广链接"}
      description={`第 ${input.line_no} 行 · ${input.raw_text}`}
      dirty={JSON.stringify(values) !== JSON.stringify(initial.current)}
      pending={busy}
      onClose={onClose}
      actions={
        <Button
          disabled={busy || (preparing && !unknownId)}
          onClick={() => void save()}
        >
          {unknownId ? "查询保存结果" : busy ? "正在保存…" : "保存并继续准备"}
        </Button>
      }
    >
      <FieldGroup>
        {preparing && (
          <Alert>
            <AlertDescription>
              {preparationLabel(summary)}
              可以先查看和编辑链接，当前准备完成后即可保存。
            </AlertDescription>
          </Alert>
        )}
        {!!error && <BuildError error={error} />}
        {unknownId && (
          <p role="status">
            保存结果尚待确认，请查询原请求；刷新页面后也可继续恢复。
          </p>
        )}
        <Field data-invalid={!!validation}>
          <FieldLabel htmlFor="manual-url">推广链接</FieldLabel>
          <Textarea
            id="manual-url"
            value={values.url}
            maxLength={8192}
            disabled={busy || !!unknownId}
            aria-invalid={!!validation}
            onChange={(e) => {
              setValues((v) => ({ ...v, url: e.target.value }))
              setValidation("")
            }}
          />
          {validation && (
            <p role="alert" className="text-sm text-destructive">
              {validation}
            </p>
          )}
        </Field>
        <Field>
          <FieldLabel htmlFor="manual-external-id">
            版权方剧目 ID（选填）
          </FieldLabel>
          <Input
            id="manual-external-id"
            maxLength={255}
            value={values.external_drama_id}
            disabled={busy || !!unknownId}
            onChange={(e) =>
              setValues((v) => ({ ...v, external_drama_id: e.target.value }))
            }
          />
          <p className="text-sm text-muted-foreground">
            没有剧目 ID 也可使用已有推广链接搭建。
          </p>
        </Field>
        <Field>
          <FieldLabel htmlFor="manual-attribution">
            归因名称
            {summary.provider_kind === "wangyan" ? "（必填）" : "（选填）"}
          </FieldLabel>
          <Input
            id="manual-attribution"
            maxLength={1000}
            value={values.protected_base}
            disabled={busy || !!unknownId}
            onChange={(e) => {
              setValues((v) => ({ ...v, protected_base: e.target.value }))
              setValidation("")
            }}
          />
          <p className="text-sm text-muted-foreground">
            版权方要求固定广告名称前缀时，请完整粘贴对应的归因名称。
          </p>
        </Field>
      </FieldGroup>
    </ManagementSheet>
  )
}
