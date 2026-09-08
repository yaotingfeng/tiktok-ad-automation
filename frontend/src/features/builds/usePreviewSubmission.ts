import { useNavigate } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { useCallback, useEffect, useRef, useState } from "react"
import { flushSync } from "react-dom"
import {
  BuildsService,
  type PreviewSummary,
  type SubmissionReceipt,
} from "@/client"
import { reportError, unknownOutcome } from "./presentation"

type Record = { requestId: string; receipt?: SubmissionReceipt }
export function usePreviewSubmission(
  tenantId: string,
  bcId: string,
  previewId: string,
  write: boolean,
) {
  const key = `build-submit:${tenantId}:${bcId}:${previewId}`
  const [record, setRecord] = useState<Record | null>(() => {
      try {
        const value = JSON.parse(sessionStorage.getItem(key) || "null")
        return value && typeof value.requestId === "string" ? value : null
      } catch {
        return null
      }
    }),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [forbidden, setForbidden] = useState(false)
  const controller = useRef<AbortController | null>(null),
    lock = useRef(false),
    navigate = useNavigate()
  useEffect(() => () => controller.current?.abort(), [])
  const go = useCallback(
    async (receipt: SubmissionReceipt) => {
      await navigate({
        to: "/tenants/$tenantId/build-tasks/$submissionId",
        params: { tenantId, submissionId: receipt.submission_id },
        search: { bc_id: bcId },
      })
    },
    [tenantId, bcId, navigate],
  )
  async function accepted(requestId: string, receipt: SubmissionReceipt) {
    const accepted = { requestId, receipt }
    sessionStorage.setItem(key, JSON.stringify(accepted))
    flushSync(() => {
      setRecord(accepted)
      setBusy(false)
    })
    await go(receipt)
  }
  async function submit(preview: PreviewSummary) {
    if (
      lock.current ||
      record ||
      forbidden ||
      !write ||
      preview.preview_id !== previewId ||
      preview.bc_id !== bcId ||
      preview.status !== "FROZEN" ||
      preview.campaign_count === 0
    )
      return
    lock.current = true
    setBusy(true)
    setError(undefined)
    const ctrl = new AbortController()
    controller.current = ctrl
    const requestId = crypto.randomUUID()
    try {
      sessionStorage.setItem(key, JSON.stringify({ requestId }))
      setRecord({ requestId })
      const { data } = await BuildsService.submitPreview({
        path: { tenant_id: tenantId, preview_id: previewId },
        body: { request_id: requestId },
        signal: ctrl.signal,
      })
      if (!ctrl.signal.aborted) await accepted(requestId, data)
    } catch (e) {
      if (!ctrl.signal.aborted) {
        reportError(e)
        setError(e)
        if (!unknownOutcome(e)) {
          sessionStorage.removeItem(key)
          setRecord(null)
        }
        if (e instanceof AxiosError && e.response?.status === 403)
          setForbidden(true)
      }
    } finally {
      lock.current = false
      if (!ctrl.signal.aborted) setBusy(false)
    }
  }
  async function recover() {
    if (!record || record.receipt || lock.current) return
    lock.current = true
    setBusy(true)
    setError(undefined)
    const ctrl = new AbortController()
    controller.current = ctrl
    try {
      const { data } = await BuildsService.savedSubmission({
        path: { tenant_id: tenantId, request_id: record.requestId },
        signal: ctrl.signal,
      })
      if (!ctrl.signal.aborted) await accepted(record.requestId, data)
    } catch (e) {
      if (!ctrl.signal.aborted) {
        reportError(e)
        setError(e)
      }
    } finally {
      lock.current = false
      if (!ctrl.signal.aborted) setBusy(false)
    }
  }
  return {
    submit,
    recover,
    go,
    record,
    busy,
    error,
    forbidden,
    unknown: !!record && !record.receipt,
  }
}
