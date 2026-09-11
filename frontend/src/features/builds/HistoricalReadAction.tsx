import { useQuery } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import { BuildsService, type HistoricalReadReceipt } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Identifier } from "@/features/accounts/presentation"
import { canManage, isForbidden } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { BuildReason, reportError, unknownOutcome } from "./presentation"

type Saved = { requestId: string; receipt?: HistoricalReadReceipt }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
function validReceipt(
  data: HistoricalReadReceipt,
  requestId: string,
  stepId: string,
) {
  if (
    !uuid.test(data.read_id) ||
    data.request_id !== requestId ||
    data.source_step_id !== stepId ||
    data.requires_new_preparation !== true ||
    !["PENDING", "RUNNING", "CONFIRMED", "UNKNOWN", "BLOCKED"].includes(
      data.state,
    )
  )
    throw new Error("核查回执范围不一致")
  return data
}

export function HistoricalReadAction({
  tenantId,
  bcId,
  submissionId,
  stepId,
  eligible,
}: {
  tenantId: string
  bcId: string
  submissionId: string
  stepId: string
  eligible: boolean
}) {
  const { scope } = useTenantScope()
  const key = `historical-read:${tenantId}:${bcId}:${submissionId}:${stepId}`
  const [saved, setSaved] = useState<Saved | null>(() => {
    try {
      const value = JSON.parse(
        sessionStorage.getItem(key) || "null",
      ) as Saved | null
      if (!value || !uuid.test(value.requestId)) return null
      if (value.receipt) validReceipt(value.receipt, value.requestId, stepId)
      return value
    } catch {
      return null
    }
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const lock = useRef(false)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  function keep(value: Saved) {
    // 先保存原请求编号，关闭抽屉或响应丢失后仍只核实这一次请求。
    sessionStorage.setItem(key, JSON.stringify(value))
    setSaved(value)
  }
  const progress = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "builds",
      bcId,
      submissionId,
      "historical-read",
      stepId,
      saved?.receipt?.read_id,
    ],
    enabled: !!saved?.receipt,
    queryFn: async ({ signal }) => {
      const { data } = await BuildsService.getHistoricalRead({
        path: { tenant_id: tenantId, read_id: saved!.receipt!.read_id },
        signal,
      })
      return validReceipt(data, saved!.requestId, stepId)
    },
    refetchInterval: (q) =>
      !q.state.error &&
      ["PENDING", "RUNNING"].includes(
        q.state.data?.state || saved?.receipt?.state || "",
      )
        ? 2000
        : false,
    retry: false,
  })
  const denied = isForbidden(error) || isForbidden(progress.error)
  const mayStart = eligible && canManage(scope?.role)
  const current = progress.data || saved?.receipt
  const mayRestart =
    mayStart &&
    !!saved?.receipt &&
    !!current &&
    ["UNKNOWN", "BLOCKED"].includes(current.state)
  async function request(action: "start" | "lookup" | "restart") {
    const lookup = action === "lookup"
    if (
      lock.current ||
      denied ||
      (lookup
        ? !saved || !!saved.receipt
        : action === "restart"
          ? !mayRestart
          : !mayStart || !!saved)
    )
      return
    lock.current = true
    setBusy(true)
    setError(undefined)
    const ctrl = new AbortController()
    controller.current = ctrl
    // 明确终态后必须再次点击才生成新请求；受理未知或运行中的请求只查询原编号。
    const requestId = lookup ? saved!.requestId : crypto.randomUUID()
    let persisted = !!saved
    try {
      if (!lookup) {
        keep({ requestId })
        persisted = true
      }
      const { data } = lookup
        ? await BuildsService.getHistoricalReadRequest({
            path: { tenant_id: tenantId, request_id: requestId },
            signal: ctrl.signal,
          })
        : await BuildsService.authorizeHistoricalRead({
            path: { tenant_id: tenantId, step_id: stepId },
            body: { request_id: requestId },
            signal: ctrl.signal,
          })
      validReceipt(data, requestId, stepId)
      if (!ctrl.signal.aborted) keep({ requestId, receipt: data })
    } catch (e) {
      if (!ctrl.signal.aborted) {
        reportError(e)
        setError(e)
        if (
          !lookup &&
          persisted &&
          !unknownOutcome(e) &&
          !(e instanceof AxiosError && e.response?.status === 408)
        ) {
          sessionStorage.removeItem(key)
          setSaved(null)
        }
      }
    } finally {
      lock.current = false
      if (!ctrl.signal.aborted) setBusy(false)
    }
  }
  if (!saved && !mayStart && !denied) return null
  return (
    <Alert>
      <AlertTitle>按新授权核查原对象</AlertTitle>
      <AlertDescription>
        <div className="flex flex-col gap-2">
          <p>本次只读取原对象，不会继续创建广告。</p>
          <p>核查结果单独保存；需要继续投放时，请重新准备并确认新的预览。</p>
          {denied ? (
            <p>当前无权执行或查看此次核查。</p>
          ) : (
            <>
              {!saved && mayStart && (
                <Button disabled={busy} onClick={() => void request("start")}>
                  按新授权只读核查
                </Button>
              )}
              {saved && !saved.receipt && (
                <>
                  <p>
                    {error instanceof AxiosError &&
                    error.response?.status === 404
                      ? "尚未查到原请求，请继续核实。"
                      : "原核查请求的受理结果尚未确认。"}
                  </p>
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() => void request("lookup")}
                  >
                    查询原核查请求
                  </Button>
                </>
              )}
              {current && (
                <>
                  <p role="status">
                    {current.state === "CONFIRMED"
                      ? current.mismatch
                        ? "已找到原对象，存在差异"
                        : "已核实原对象"
                      : current.state === "UNKNOWN"
                        ? "原对象结果仍待核实"
                        : current.state === "BLOCKED"
                          ? "此次核查已阻断"
                          : "已受理，正在只读核查"}
                  </p>
                  {current.remote_id && (
                    <Identifier value={current.remote_id} />
                  )}
                  {current.reason_code && (
                    <BuildReason code={current.reason_code} />
                  )}
                  {mayRestart && (
                    <Button
                      disabled={busy}
                      onClick={() => void request("restart")}
                    >
                      再次只读核查
                    </Button>
                  )}
                  <Button
                    variant="outline"
                    disabled={progress.isFetching}
                    onClick={() => void progress.refetch()}
                  >
                    刷新核查结果
                  </Button>
                </>
              )}
              {!!error && !saved && (
                <p>此次核查未受理，请先刷新任务和连接状态。</p>
              )}
              {!!progress.error && <p>暂时无法读取最新核查结果。</p>}
            </>
          )}
        </div>
      </AlertDescription>
    </Alert>
  )
}
