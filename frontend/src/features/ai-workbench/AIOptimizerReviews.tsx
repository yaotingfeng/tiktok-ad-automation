import { Check, X } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { aiRequest, createRequestIdentity, errorMessage } from "./api"

type Proposal = {
  id: string
  status: string
  title?: string
  summary?: string
  kind?: string
  before?: unknown
  after?: unknown
  evidence?: unknown
  authorization?: unknown
  base_optimizer_version?: number
  base_strategy_version_id?: string
  base_rule_version?: number
  target_id?: string
  decision_reason?: string
}
type ReviewTask = {
  id: string
  status: string
  error?: unknown
  error_code?: string
  error_message?: string
  result?: { summary?: string; [key: string]: unknown }
}
type Props = { tenantId: string; optimizerId: string; pendingOnly?: boolean }

function Evidence({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <h3 className="text-sm font-medium">{label}</h3>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">
        {value === undefined ? "暂无记录" : JSON.stringify(value, null, 2)}
      </pre>
    </div>
  )
}

/** 每个视图请求都随优化师卸载取消，迟到的复盘/决定不得覆盖新优化师状态。 */
export function AIOptimizerReviews({
  tenantId,
  optimizerId,
  pendingOnly = false,
}: Props) {
  const [items, setItems] = useState<Proposal[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [history, setHistory] = useState<ReviewTask[]>([])
  const [runtimeReady, setRuntimeReady] = useState<boolean | undefined>()
  const [task, setTask] = useState<ReviewTask | null>(null)
  const decisionIdentity = useRef(createRequestIdentity())
  const reviewIdentity = useRef(createRequestIdentity())
  const lifecycle = useRef(new AbortController())
  const base = `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}`
  const load = useCallback(
    async (signal: AbortSignal) => {
      if (!optimizerId) return
      const result = await aiRequest<Proposal[]>(
        `${base}/proposals${pendingOnly ? "?status=PENDING" : ""}`,
        { signal },
      )
      if (!signal.aborted)
        setItems(
          pendingOnly
            ? result.filter((item) => item.status === "PENDING")
            : result,
        )
    },
    [base, pendingOnly, optimizerId],
  )
  useEffect(() => {
    const controller = new AbortController()
    lifecycle.current = controller
    setItems([])
    setError(null)
    setTask(null)
    setHistory([])
    setRuntimeReady(undefined)
    setBusy(null)
    void load(controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setError(errorMessage(reason))
    })
    if (!pendingOnly && optimizerId) {
      void aiRequest<{ runtime_ready?: boolean; items: ReviewTask[] }>(
        `${base}/reviews`,
        { signal: controller.signal },
      )
        .then((result) => {
          if (!controller.signal.aborted) {
            setHistory((previous) =>
              previous.length ? previous : result.items,
            )
            setTask((previous) => previous ?? result.items[0] ?? null)
            setRuntimeReady(result.runtime_ready)
          }
        })
        .catch((reason) => {
          if (!controller.signal.aborted) setError(errorMessage(reason))
        })
    }
    return () => controller.abort()
  }, [load, pendingOnly, optimizerId, base])

  useEffect(() => {
    if (
      !task ||
      !["PENDING", "QUEUED", "RUNNING", "CREATED"].includes(task.status)
    )
      return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      void aiRequest<ReviewTask>(`${base}/reviews/${task.id}`, {
        signal: controller.signal,
      })
        .then(async (next) => {
          if (controller.signal.aborted) return
          await load(controller.signal)
          if (!controller.signal.aborted) {
            setTask(next)
            setHistory((previous) =>
              previous.map((item) => (item.id === next.id ? next : item)),
            )
          }
        })
        .catch((reason) => {
          if (!controller.signal.aborted) setError(errorMessage(reason))
        })
    }, 2000)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [base, load, task])

  async function decide(item: Proposal, decision: "accept" | "reject") {
    if (item.status !== "PENDING" || busy) return
    const signal = lifecycle.current.signal
    setBusy(item.id)
    setError(null)
    try {
      const updated = await aiRequest<Proposal>(
        `${base}/proposals/${item.id}/${decision}`,
        {
          method: "POST",
          signal,
          body: {
            request_id: decisionIdentity.current({
              optimizerId,
              proposalId: item.id,
              decision,
            }),
            ...(decision === "reject" ? { reason: "工作台人工不采纳" } : {}),
          },
        },
      )
      if (signal.aborted) return
      setItems((previous) =>
        pendingOnly
          ? previous.filter((entry) => entry.id !== item.id)
          : previous.map((entry) => (entry.id === item.id ? updated : entry)),
      )
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  async function review() {
    const signal = lifecycle.current.signal
    setBusy("review")
    setError(null)
    try {
      const result = await aiRequest<{
        task: ReviewTask
        proposals: Proposal[]
      }>(`${base}/reviews`, {
        method: "POST",
        signal,
        body: {
          request_id: reviewIdentity.current({
            optimizerId,
            previousTask: task?.id,
          }),
          evidence_ids: [],
          knowledge_refs: [],
        },
      })
      if (!signal.aborted) {
        setTask(result.task)
        setHistory((previous) => [
          result.task,
          ...previous.filter((item) => item.id !== result.task.id),
        ])
        await load(signal)
      }
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  async function controlTask(action: "refresh" | "cancel" | "retry") {
    if (!task) return
    const signal = lifecycle.current.signal
    setBusy("task")
    setError(null)
    try {
      const next = await aiRequest<ReviewTask>(
        `${base}/reviews/${task.id}${action === "refresh" ? "" : `/${action}`}`,
        {
          signal,
          ...(action === "refresh" ? {} : { method: "POST", body: {} }),
        },
      )
      if (!signal.aborted) {
        setTask(next)
        setHistory((previous) =>
          previous.map((item) => (item.id === next.id ? next : item)),
        )
        await load(signal)
      }
    } catch (reason) {
      if (!signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!signal.aborted) setBusy(null)
    }
  }
  return (
    <Card data-testid={pendingOnly ? "ai-pending-view" : "ai-review-view"}>
      <CardHeader className="flex flex-wrap items-start justify-between gap-3">
        <CardTitle>{pendingOnly ? "待确认" : "复盘与建议"}</CardTitle>
        {!pendingOnly && (
          <Button
            disabled={
              !optimizerId ||
              runtimeReady === false ||
              !!busy ||
              (!!task &&
                ["PENDING", "QUEUED", "RUNNING", "CREATED"].includes(
                  task.status,
                ))
            }
            onClick={() => void review()}
          >
            发起复盘
          </Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {runtimeReady === false && (
          <p className="text-sm text-muted-foreground">
            复盘服务尚未配置，暂不能发起复盘。
          </p>
        )}
        {history.length > 1 && (
          <div className="flex flex-wrap gap-2">
            {history.map((item) => (
              <Button
                key={item.id}
                size="sm"
                variant="outline"
                onClick={() => setTask(item)}
              >
                复盘 {item.id.slice(0, 8)} · {item.status}
              </Button>
            ))}
          </div>
        )}
        {task && (
          <div role="status" className="text-sm">
            复盘任务 {task.id} · {task.status}
            {task.error_message && <p>{task.error_message}</p>}
            {task.error_code && <p>{task.error_code}</p>}
            {task.result?.summary && <p>{task.result.summary}</p>}
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!!busy}
                onClick={() => void controlTask("refresh")}
              >
                刷新复盘状态
              </Button>
              {["PENDING", "QUEUED", "RUNNING"].includes(task.status) && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!!busy}
                  onClick={() => void controlTask("cancel")}
                >
                  取消复盘
                </Button>
              )}
              {task.status === "FAILED" && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!!busy || runtimeReady === false}
                  onClick={() => void controlTask("retry")}
                >
                  重试复盘
                </Button>
              )}
            </div>
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        {!items.length && !error && (
          <p className="text-sm text-muted-foreground">
            暂无{pendingOnly ? "待确认建议" : "复盘建议"}。
          </p>
        )}
        {items.map((item) => (
          <Card key={item.id}>
            <CardHeader>
              <CardTitle className="flex flex-wrap justify-between gap-2">
                {item.title ??
                  (item.kind === "rule"
                    ? "自动规则调整"
                    : item.kind === "strategy"
                      ? "投放策略调整"
                      : item.id)}
                <Badge variant="outline">{item.status}</Badge>
              </CardTitle>
              <p className="text-sm text-muted-foreground">
                {item.summary ?? item.decision_reason ?? ""}
              </p>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <p className="break-all text-xs text-muted-foreground">
                优化师基准 v{item.base_optimizer_version ?? "—"} · 目标{" "}
                {item.target_id ?? "—"} · 策略版本{" "}
                {item.base_strategy_version_id ?? "—"} · 规则版本{" "}
                {item.base_rule_version ?? "—"}
              </p>
              <div className="grid min-w-0 gap-4 lg:grid-cols-2">
                <Evidence label="调整前" value={item.before} />
                <Evidence label="调整后" value={item.after} />
                <Evidence label="决策证据" value={item.evidence} />
                <Evidence label="授权与影响范围" value={item.authorization} />
              </div>
              {item.status === "PENDING" && (
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={!!busy}
                    onClick={() => void decide(item, "accept")}
                  >
                    <Check />
                    接受
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!!busy}
                    onClick={() => void decide(item, "reject")}
                  >
                    <X />
                    拒绝
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </CardContent>
    </Card>
  )
}
