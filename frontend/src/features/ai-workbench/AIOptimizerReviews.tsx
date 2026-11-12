import { useCallback, useEffect, useState } from "react"
import { Check, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

type Proposal = { id: string; status?: string; title?: string; summary?: string; change?: Record<string, unknown> }

type Props = { tenantId: string; optimizerId: string; pendingOnly?: boolean }

/** 待确认操作沿用 Task 5 的 proposal 接口；每次决定都带幂等 request_id。 */
export function AIOptimizerReviews({ tenantId, optimizerId, pendingOnly = false }: Props) {
  const [items, setItems] = useState<Proposal[]>([])
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async () => {
    if (!optimizerId) return
    const response = await fetch(`/api/tenants/${tenantId}/ai-optimizers/${optimizerId}/proposals${pendingOnly ? "?status=PENDING" : ""}`)
    if (!response.ok) throw new Error("无法读取待确认建议")
    setItems((await response.json()) as Proposal[])
  }, [pendingOnly, tenantId, optimizerId])
  useEffect(() => { setItems([]); setError(null); void load().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "无法读取建议")) }, [load])
  async function decide(item: Proposal, decision: "accept" | "reject") {
    const response = await fetch(`/api/tenants/${tenantId}/ai-optimizers/${optimizerId}/proposals/${item.id}/${decision}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: crypto.randomUUID(), ...(decision === "reject" ? { reason: "操作台拒绝" } : {}) }) })
    if (!response.ok) throw new Error("建议处理失败")
    await load()
  }
  return <Card data-testid={pendingOnly ? "ai-pending-view" : "ai-review-view"}><CardHeader><CardTitle>{pendingOnly ? "待确认" : "复盘与建议"}</CardTitle></CardHeader><CardContent className="space-y-3">{error && <p role="alert" className="text-sm text-destructive">{error}</p>}{!items.length && !error && <p className="text-sm text-muted-foreground">暂无{pendingOnly ? "待确认建议" : "复盘建议"}。</p>}{items.map((item) => <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3" key={item.id}><div className="min-w-0"><p className="font-medium">{item.title ?? item.id}</p><p className="text-xs text-muted-foreground">{item.summary ?? item.status ?? "待处理"}</p></div>{pendingOnly && <div className="flex gap-2"><Button size="sm" onClick={() => void decide(item, "accept")}><Check />接受</Button><Button size="sm" variant="outline" onClick={() => void decide(item, "reject")}><X />拒绝</Button></div>}</div>)}</CardContent></Card>
}
