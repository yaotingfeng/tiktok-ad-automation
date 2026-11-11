import { useEffect, useMemo, useState } from "react"
import { DramaPlanGrid, type DramaPlanRow, emptyPlanRow } from "./DramaPlanGrid"

type Optimizer = {
  id: string
  name: string
  active: boolean
  version: number
  bc_id: string
  advertiser_ids: string[]
  strategy_version_id: string
}

type Props = { tenantId: string; optimizerId?: string }

const today = () => new Date().toISOString().slice(0, 10)

function blankRows(date: string, optimizerId: string): DramaPlanRow[] {
  return Array.from({ length: 10 }, () => emptyPlanRow(date, optimizerId))
}

function rowFromApi(
  row: Record<string, unknown>,
  optimizerId: string,
): DramaPlanRow {
  return {
    business_date: String(row.business_date ?? ""),
    scheduled_time: String(row.scheduled_time ?? ""),
    title: String(row.title ?? ""),
    provider_connection_id: String(row.provider_connection_id ?? ""),
    application_id: String(row.application_id ?? ""),
    promotion_url: String(row.promotion_url ?? ""),
    external_drama_id: String(row.external_drama_id ?? ""),
    order_materials: Boolean(row.order_materials),
    optimizer_id: optimizerId,
  }
}

export function AIWorkbenchPage({
  tenantId,
  optimizerId: requestedOptimizerId = "",
}: Props) {
  const [businessDate, setBusinessDate] = useState(today)
  const [optimizers, setOptimizers] = useState<Optimizer[]>([])
  const [optimizerId, setOptimizerId] = useState(requestedOptimizerId)
  const [optimizer, setOptimizer] = useState<Optimizer | null>(null)
  const [rows, setRows] = useState<DramaPlanRow[]>([])
  const [revision, setRevision] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setOptimizer(null)
    setOptimizerId("")
    setRows([])
    setRevision(null)
    fetch(`/api/tenants/${tenantId}/ai-optimizers`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("无法读取 AI 优化师")
        return (await response.json()) as { items?: Optimizer[] }
      })
      .then((payload) => {
        const active = (payload.items ?? []).filter((item) => item.active)
        setOptimizers(active)
        const selected =
          active.find((item) => item.id === requestedOptimizerId) ?? active[0]
        setOptimizerId(selected?.id ?? "")
      })
      .catch((reason: unknown) => {
        if ((reason as { name?: string }).name !== "AbortError")
          setError(
            reason instanceof Error ? reason.message : "无法读取 AI 优化师",
          )
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [tenantId, requestedOptimizerId])

  useEffect(() => {
    const selected = optimizers.find((item) => item.id === optimizerId) ?? null
    setOptimizer(selected)
    setRows(selected ? blankRows(businessDate, selected.id) : [])
    setRevision(null)
    if (!selected) return
    const controller = new AbortController()
    fetch(
      `/api/tenants/${tenantId}/ai-optimizers/${selected.id}/daily-plans?business_date=${businessDate}`,
      { signal: controller.signal },
    )
      .then(async (response) => {
        if (response.status === 404) return null
        if (!response.ok) throw new Error("无法读取每日剧单")
        return (await response.json()) as {
          revision: number
          rows: Record<string, unknown>[]
        }
      })
      .then((plan) => {
        if (!plan) return
        setRevision(plan.revision)
        setRows(plan.rows.map((row) => rowFromApi(row, selected.id)))
      })
      .catch((reason: unknown) => {
        if ((reason as { name?: string }).name !== "AbortError")
          setError(
            reason instanceof Error ? reason.message : "无法读取每日剧单",
          )
      })
    return () => controller.abort()
  }, [businessDate, optimizerId, optimizers, tenantId])

  const contextLabel = useMemo(() => {
    if (!optimizer) return "请选择一个启用中的 AI 优化师"
    return `BC ${optimizer.bc_id} · 账户 ${optimizer.advertiser_ids.join(", ")} · 策略 ${optimizer.strategy_version_id}`
  }, [optimizer])

  async function save(nextRows: DramaPlanRow[]) {
    if (!optimizer) return
    const payloadRows = nextRows
      .filter((row) => row.title.trim())
      .map((row) => ({
        ...row,
        business_date: businessDate,
        optimizer_id: optimizer.id,
      }))
    const response = await fetch(
      `/api/tenants/${tenantId}/ai-optimizers/${optimizer.id}/daily-plans`,
      {
        method: revision === null ? "POST" : "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          business_date: businessDate,
          rows: payloadRows,
          ...(revision === null ? {} : { revision }),
        }),
      },
    )
    if (!response.ok) throw new Error("每日剧单保存失败")
    const saved = (await response.json()) as {
      revision: number
      rows: Record<string, unknown>[]
    }
    setRevision(saved.revision)
    setRows(saved.rows.map((row) => rowFromApi(row, optimizer.id)))
  }

  return (
    <div
      className="flex min-w-0 flex-col gap-6"
      data-testid="ai-workbench-page"
    >
      <header>
        <h1 className="workspace-title">AI 优化师</h1>
        <p className="text-sm text-muted-foreground">租户 {tenantId}</p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="text-sm" htmlFor="optimizer-select">
            优化师
          </label>
          <select
            id="optimizer-select"
            data-testid="optimizer-select"
            className="h-9 rounded-md border bg-background px-3 text-sm"
            value={optimizerId}
            onChange={(event) => setOptimizerId(event.target.value)}
            disabled={loading || !optimizers.length}
          >
            {!optimizers.length && <option value="">暂无启用中的优化师</option>}
            {optimizers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          继承上下文：{contextLabel}
        </p>
        {error && (
          <p className="mt-2 text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
      </header>
      {optimizer ? (
        <DramaPlanGrid
          key={`${tenantId}:${optimizer.id}:${businessDate}`}
          businessDate={businessDate}
          optimizerId={optimizer.id}
          rows={rows}
          onChange={setRows}
          onDateChange={setBusinessDate}
          onSave={save}
        />
      ) : (
        <p className="text-sm text-muted-foreground">
          {loading ? "正在载入优化师…" : "当前租户没有可用的 AI 优化师。"}
        </p>
      )}
    </div>
  )
}
