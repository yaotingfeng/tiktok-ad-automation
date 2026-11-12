import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { AIOptimizer } from "./AIOptimizerList"

export function AIOptimizerOverview({ optimizer }: { optimizer: AIOptimizer | null }) {
  if (!optimizer) return <p className="text-sm text-muted-foreground">请选择一个 AI 优化师查看概览。</p>
  return (
    <div className="grid gap-4 lg:grid-cols-3" data-testid="ai-optimizer-overview">
      <Card><CardHeader><CardTitle>当前状态</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><div className="flex justify-between"><span className="text-muted-foreground">版本</span><span>v{optimizer.version}</span></div><div className="flex justify-between"><span className="text-muted-foreground">模式</span><Badge>{optimizer.optimization_mode}</Badge></div><div className="flex justify-between"><span className="text-muted-foreground">规则数</span><span>{optimizer.rule_ids?.length ?? 0}</span></div></CardContent></Card>
      <Card><CardHeader><CardTitle>目标</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><div className="flex justify-between"><span className="text-muted-foreground">日预算</span><span>{optimizer.daily_spend ?? "—"}</span></div><div className="flex justify-between"><span className="text-muted-foreground">优先级</span><span>{optimizer.priority ?? "BALANCED"}</span></div><div className="flex justify-between"><span className="text-muted-foreground">目标 ROAS</span><span>{optimizer.target_roas ?? "—"}</span></div></CardContent></Card>
      <Card><CardHeader><CardTitle>执行边界</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">计划只登记剧目和时间。素材匹配在执行证据中展示，计划表不会提前选择素材。</CardContent></Card>
    </div>
  )
}
