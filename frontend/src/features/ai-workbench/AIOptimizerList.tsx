import { useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"

export type AIOptimizer = {
  id: string
  name: string
  active: boolean
  version: number
  bc_id: string
  advertiser_ids: string[]
  strategy_version_id: string
  optimization_mode: "AUTO" | "MANUAL"
  daily_spend?: string | number
  target_roas?: string | number | null
  priority?: "SPEND" | "ROAS" | "BALANCED"
  rule_ids?: string[]
  strategy_binding_id?: string
}

type Props = {
  tenantId: string
  selectedId: string
  onSelect: (optimizerId: string) => void
  onLoaded?: (optimizers: AIOptimizer[]) => void
}

/** 仅读取当前租户的优化师；切换由上层 URL 承载，避免跨租户或跨优化师残留状态。 */
export function AIOptimizerList({
  tenantId,
  selectedId,
  onSelect,
  onLoaded,
}: Props) {
  const [items, setItems] = useState<AIOptimizer[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setItems([])
    fetch(`/api/tenants/${tenantId}/ai-optimizers`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("无法读取 AI 优化师")
        return (await response.json()) as { items?: AIOptimizer[] }
      })
      .then((payload) => {
        const active = (payload.items ?? []).filter((item) => item.active)
        setItems(active)
        onLoaded?.(active)
        if (!selectedId && active[0]) onSelect(active[0].id)
      })
      .catch((reason: unknown) => {
        if ((reason as { name?: string }).name !== "AbortError")
          setError(reason instanceof Error ? reason.message : "无法读取 AI 优化师")
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [onLoaded, onSelect, selectedId, tenantId])

  return (
    <Card className="border-border/70" data-testid="ai-optimizer-list">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">优化师</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <Label htmlFor="ai-optimizer-select">当前优化师</Label>
        <select
          id="ai-optimizer-select"
          data-testid="ai-optimizer-select"
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          value={selectedId}
          onChange={(event) => onSelect(event.target.value)}
          disabled={loading || !items.length}
        >
          {!items.length && (
            <option value="">{loading ? "载入中…" : "暂无启用中的优化师"}</option>
          )}
          {items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        {error && <p className="text-xs text-destructive" role="alert">{error}</p>}
        <div className="space-y-2" aria-label="优化师列表">
          {items.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`flex w-full items-center justify-between rounded-md border px-3 py-2 text-left text-sm transition-colors hover:bg-muted ${item.id === selectedId ? "border-primary bg-muted" : "border-border/60"}`}
              onClick={() => onSelect(item.id)}
              data-testid={`optimizer-option-${item.id}`}
            >
              <span className="min-w-0 truncate">{item.name}</span>
              <Badge variant={item.optimization_mode === "AUTO" ? "default" : "secondary"}>
                {item.optimization_mode}
              </Badge>
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
