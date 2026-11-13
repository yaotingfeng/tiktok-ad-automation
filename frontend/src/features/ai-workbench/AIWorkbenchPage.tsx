import { isAxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import type { AIOptimizer } from "./AIOptimizerList"
import { aiRequest, businessToday, errorMessage } from "./api"
import { DramaPlanGrid, type DramaPlanRow, emptyPlanRow } from "./DramaPlanGrid"

type Props = { tenantId: string; optimizerId?: string }
type Plan = { revision: number; rows: DramaPlanRow[] }

export function planRowPayload(row: DramaPlanRow) {
  // 只提交输入合同，排除 read_only、plan_id、position 等响应元数据；ID 原样保留。
  return {
    ...(row.id ? { id: row.id } : {}),
    business_date: row.business_date,
    scheduled_time: row.scheduled_time,
    title: row.title,
    provider_connection_id: row.provider_connection_id,
    application_id: row.application_id,
    promotion_url: row.promotion_url || null,
    external_drama_id: row.external_drama_id || null,
    order_materials: row.order_materials,
    optimizer_id: row.optimizer_id,
  }
}

function rowFromApi(row: DramaPlanRow): DramaPlanRow {
  return {
    ...planRowPayload(row),
    promotion_url: row.promotion_url ?? "",
    external_drama_id: row.external_drama_id ?? "",
    read_only: row.read_only,
  }
}

export function AIWorkbenchPage({ tenantId, optimizerId = "" }: Props) {
  const [optimizer, setOptimizer] = useState<AIOptimizer | null>(null)
  const [businessDate, setBusinessDate] = useState(businessToday)
  const [rows, setRows] = useState<DramaPlanRow[]>([])
  const [revision, setRevision] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const currentScope = `${tenantId}:${optimizerId}:${businessDate}`
  const scope = useRef(currentScope)
  scope.current = currentScope
  const base = `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}`
  useEffect(() => {
    if (!optimizerId) {
      setLoading(false)
      return
    }
    const controller = new AbortController()
    setOptimizer(null)
    aiRequest<AIOptimizer>(base, { signal: controller.signal })
      .then((item) => {
        if (!controller.signal.aborted) {
          setOptimizer(item)
          setBusinessDate(businessToday(item.timezone))
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(errorMessage(reason))
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [base, optimizerId])
  useEffect(() => {
    if (!optimizer) return
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setRows([])
    setRevision(null)
    aiRequest<Plan>(`${base}/daily-plans?business_date=${businessDate}`, {
      signal: controller.signal,
    })
      .then((plan) => {
        if (controller.signal.aborted) return
        setRevision(plan.revision)
        setRows(plan.rows.map(rowFromApi))
      })
      .catch((reason) => {
        if (controller.signal.aborted) return
        if (isAxiosError(reason) && reason.response?.status === 404)
          setRows(
            Array.from({ length: 10 }, () =>
              emptyPlanRow(businessDate, optimizerId),
            ),
          )
        else setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [base, businessDate, optimizer, optimizerId])

  async function save(nextRows: DramaPlanRow[]) {
    if (!optimizer || loading) return
    const requestScope = currentScope
    const result = await aiRequest<Plan>(`${base}/daily-plans`, {
      method: revision === null ? "POST" : "PATCH",
      body: {
        business_date: businessDate,
        rows: nextRows.filter((row) => row.title.trim()).map(planRowPayload),
        ...(revision === null ? {} : { revision }),
      },
    })
    if (scope.current !== requestScope) return
    setRevision(result.revision)
    setRows(result.rows.map(rowFromApi))
  }
  return (
    <div
      className="flex min-w-0 flex-col gap-4"
      data-testid="ai-workbench-page"
    >
      <p className="text-sm text-muted-foreground">
        时间使用 {optimizer?.timezone ?? "Asia/Shanghai"}
        。账户与唯一策略继承当前投手；已开始执行的行只读。
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {optimizer ? (
        <DramaPlanGrid
          key={currentScope}
          businessDate={businessDate}
          optimizerId={optimizerId}
          rows={rows}
          onChange={setRows}
          onDateChange={setBusinessDate}
          onSave={error ? undefined : save}
          disabled={loading}
        />
      ) : (
        <p className="text-sm text-muted-foreground">
          {loading ? "正在读取投手…" : "请创建或选择一个投手。"}
        </p>
      )}
    </div>
  )
}
