import { optimizationModeLabel } from "./presentation"
import { useEffect, useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { allItems, errorMessage } from "./api"

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
  material_config?: { strategy_ids?: string[]; feishu_token?: string }
  authorization_limits?: Record<string, unknown>
  timezone?: string
  review_time?: string
  strategy_binding_id?: string
}

type Props = {
  tenantId: string
  selectedId: string
  onSelect: (optimizerId: string) => void
  onLoaded?: (optimizers: AIOptimizer[]) => void
}

/** 仅读取当前租户的投手；切换由上层 URL 承载，避免跨租户或跨投手残留状态。 */
export function AIOptimizerList({
  tenantId,
  selectedId,
  onSelect,
  onLoaded,
}: Props) {
  const [items, setItems] = useState<AIOptimizer[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const selection = useRef(selectedId)
  selection.current = selectedId
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setItems([])
    allItems<AIOptimizer>(
      `/api/tenants/${tenantId}/ai-optimizers`,
      controller.signal,
    )
      .then((items) => {
        if (controller.signal.aborted) return
        setItems(items)
        onLoaded?.(items)
        if (!items.some((item) => item.id === selection.current) && items[0])
          onSelect(items[0].id)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [onLoaded, onSelect, tenantId])

  return (
    <Card className="border-border/70" data-testid="ai-optimizer-list">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">投手</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <Label htmlFor="ai-optimizer-select">当前投手</Label>
        <select
          id="ai-optimizer-select"
          data-testid="ai-optimizer-select"
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          value={selectedId}
          onChange={(event) => onSelect(event.target.value)}
          disabled={loading || !items.length}
        >
          {!items.length && (
            <option value="">{loading ? "载入中…" : "暂无投手"}</option>
          )}
          {items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
              {!item.active ? "（已暂停）" : ""}
            </option>
          ))}
        </select>
        {error && (
          <p className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
        <fieldset className="space-y-2" aria-label="投手列表">
          {items.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`flex w-full items-center justify-between rounded-md border px-3 py-2 text-left text-sm transition-colors hover:bg-muted ${item.id === selectedId ? "border-primary bg-muted" : "border-border/60"}`}
              onClick={() => onSelect(item.id)}
              data-testid={`optimizer-option-${item.id}`}
            >
              <span className="min-w-0 truncate">{item.name}</span>
              <Badge
                variant={
                  item.optimization_mode === "AUTO" ? "default" : "secondary"
                }
              >
                {item.active
                  ? optimizationModeLabel(item.optimization_mode)
                  : "已暂停"}
              </Badge>
            </button>
          ))}
        </fieldset>
      </CardContent>
    </Card>
  )
}
