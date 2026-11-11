import { useState } from "react"
import { DramaPlanGrid, type DramaPlanRow } from "./DramaPlanGrid"

type Props = { tenantId: string; optimizerId: string }

export function AIWorkbenchPage({ tenantId, optimizerId }: Props) {
  const [businessDate, setBusinessDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [rows, setRows] = useState<DramaPlanRow[]>([])
  const [revision, setRevision] = useState<number | null>(null)
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="ai-workbench-page">
      <header>
        <h1 className="workspace-title">AI 优化师</h1>
        <p className="text-sm text-muted-foreground">租户 {tenantId} · 优化师 {optimizerId}</p>
        <p className="mt-1 text-xs text-muted-foreground">继承优化师的 BC、广告账户与单一投放策略。只读上下文，执行时再匹配素材。</p>
      </header>
      <DramaPlanGrid
        businessDate={businessDate}
        optimizerId={optimizerId}
        rows={rows}
        onChange={setRows}
        onDateChange={(date) => { setBusinessDate(date); setRows([]); setRevision(null) }}
        onSave={async (nextRows) => {
          if (!optimizerId) return
          const rowsToSave = nextRows.filter((row) => row.title.trim()).map((row) => ({
            ...row,
            business_date: businessDate,
            optimizer_id: optimizerId,
          }))
          const response = await fetch(
            `/api/tenants/${tenantId}/ai-optimizers/${optimizerId}/daily-plans`,
            {
            method: revision ? "PATCH" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ business_date: businessDate, rows: rowsToSave, ...(revision ? { revision } : {}) }),
          })
          if (!response.ok) throw new Error("daily plan save failed")
          const saved = (await response.json()) as { revision?: number }
          if (saved.revision) setRevision(saved.revision)
        }}
      />
    </div>
  )
}

