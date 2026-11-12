import { ArrowUpRight, LockKeyhole } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { AIOptimizer } from "./AIOptimizerList"

type Props = {
  tenantId: string
  optimizer: AIOptimizer | null
  recordId?: string
  versionId?: string
  returnContext: string
}

/** 优化师只绑定一个策略版本；编辑入口复用既有页面，并携带完整返回上下文。 */
export function AIOptimizerConfig({ tenantId, optimizer, recordId, versionId, returnContext }: Props) {
  if (!optimizer)
    return <Card><CardContent className="py-10 text-sm text-muted-foreground">请选择一个启用中的 AI 优化师。</CardContent></Card>
  const scopeParams = `ai_optimizer_id=${encodeURIComponent(optimizer.id)}&record_id=${encodeURIComponent(recordId ?? "")}&version_id=${encodeURIComponent(versionId ?? optimizer.strategy_version_id)}&return_to=${encodeURIComponent(returnContext)}`
  const strategyHref = `/tenants/${tenantId}/strategies?${scopeParams}&scope=bound&readonly=true`
  const ruleIds = optimizer.rule_ids ?? []
  const rulesHref = `/tenants/${tenantId}/automation-rules?${scopeParams}&scope=bound&rule_ids=${encodeURIComponent(ruleIds.join(","))}`
  return (
    <div className="grid min-w-0 gap-4 xl:grid-cols-2" data-testid="ai-optimizer-config">
      <Card>
        <CardHeader>
          <CardTitle>继承配置</CardTitle>
          <CardDescription>当前版本保存后，执行会重新校验租户、BC 和授权范围。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <div className="flex items-center justify-between gap-4"><span className="text-muted-foreground">BC</span><strong>{optimizer.bc_id}</strong></div>
          <div className="flex items-start justify-between gap-4"><span className="text-muted-foreground">广告账户</span><span className="text-right font-medium">{optimizer.advertiser_ids.join("、")}</span></div>
          <div className="flex items-center justify-between gap-4"><span className="text-muted-foreground">优化模式</span><Badge>{optimizer.optimization_mode}</Badge></div>
          <div className="flex items-center justify-between gap-4"><span className="text-muted-foreground">目标优先级</span><span>{optimizer.priority ?? "BALANCED"}</span></div>
          <div className="flex items-center justify-between gap-4"><span className="text-muted-foreground">日预算</span><span>{optimizer.daily_spend ?? "—"}</span></div>
          <div className="flex items-center justify-between gap-4"><span className="text-muted-foreground">目标 ROAS</span><span>{optimizer.target_roas ?? "—"}</span></div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>唯一绑定与排期</CardTitle>
          <CardDescription>一个优化师只绑定一个策略版本；排期不选择素材。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <div className="grid gap-2"><Label htmlFor="ai-bound-strategy">绑定策略版本</Label><select id="ai-bound-strategy" aria-label="绑定策略版本" className="h-9 rounded-md border bg-background px-3" value={optimizer.strategy_version_id} disabled><option value={optimizer.strategy_version_id}>{optimizer.strategy_version_id}</option></select></div>
          <div className="grid gap-2"><Label htmlFor="ai-schedule-frequency">运行频率</Label><select id="ai-schedule-frequency" aria-label="运行频率（只读）" className="h-9 rounded-md border bg-background px-3" value="DAILY" disabled><option value="DAILY">每天</option></select></div>
          <div className="grid gap-2"><Label htmlFor="ai-schedule-time">执行时间</Label><Input id="ai-schedule-time" aria-label="执行时间（只读）" type="time" value="09:00" disabled /></div>
          <p className="text-xs text-muted-foreground">排期频率和时间由每日剧单保存接口管理，当前版本没有可持久化的频率字段，因此此处只读。</p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>复用现有配置</CardTitle>
          <CardDescription>策略和规则各只有一个编辑来源；这里不会创建第二份编辑器。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          <Button asChild variant="outline"><a href={strategyHref}><LockKeyhole />策略版本 <ArrowUpRight /></a></Button>
          <Button asChild variant="outline"><a href={rulesHref}><LockKeyhole />已绑定规则 <ArrowUpRight /></a></Button>
          <p className="basis-full text-xs text-muted-foreground">策略范围：当前绑定版本。规则范围：仅当前优化师已绑定规则；租户其他规则不会被静默修改。</p>
        </CardContent>
      </Card>
    </div>
  )
}
