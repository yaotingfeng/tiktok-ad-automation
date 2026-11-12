import { getRouteApi } from "@tanstack/react-router"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { canManage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { Confirmations } from "./Confirmations"
import { RuleList } from "./RuleList"
import { RunHistory } from "./RunHistory"

const route = getRouteApi("/_layout/tenants/$tenantId/automation-rules")

export function canWriteAutomationContext(write: boolean, boundContext: boolean) {
  return write && !boundContext
}

export function AutomationRulesPage() {
  const { tenantId, scope } = useTenantScope()
  const search = route.useSearch()
  const navigate = route.useNavigate()
  const write = !!scope && scope.role !== "viewer"
  // 平台管理员与租户管理员都能保存自动执行规则（与后端 manage 权限一致）。
  const admin = canManage(scope?.role)
  const boundContext = !!search.ai_optimizer_id
  const boundRuleIds = search.rule_ids?.split(",").filter(Boolean) ?? []
  const tab = search.tab ?? "rules"
  return (
    <section
      aria-label="自动规则工作区"
      className="flex min-w-0 flex-col gap-6"
    >
      <div className="flex min-w-0 flex-col gap-1">
        <WorkspacePageTitle>自动规则</WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          按条件自动检查跨账户、跨 BC
          的投放数据，可仅发飞书预警、确认后执行或自动执行。
        </p>
      </div>
      {boundContext && (
        <p className="text-xs text-amber-700" data-testid="rule-bound-context">
          当前仅查看优化师 {search.ai_optimizer_id} 已绑定的规则；租户其他规则不会被修改。保存返回将回到 {search.return_to ?? "AI 工作台"}。
        </p>
      )}
      <Tabs
        value={tab}
        onValueChange={(value) =>
          void navigate({
            search: (previous) => ({
              ...previous,
              tab:
                value === "runs" || value === "confirmations"
                  ? value
                  : undefined,
            }),
          })
        }
        className="gap-4"
      >
        <TabsList>
          <TabsTrigger value="rules">规则管理</TabsTrigger>
          <TabsTrigger value="confirmations">待确认</TabsTrigger>
          <TabsTrigger value="runs">执行记录</TabsTrigger>
        </TabsList>
        <TabsContent value="rules">
          <RuleList tenantId={tenantId!} write={canWriteAutomationContext(write, boundContext)} admin={admin} boundOnly={boundContext} boundRuleIds={boundRuleIds} />
        </TabsContent>
        <TabsContent value="confirmations">
          <Confirmations
            tenantId={tenantId!}
            write={canWriteAutomationContext(write, boundContext)}
            openConfirmation={search.confirmation}
            onOpenConfirmation={(confirmation) =>
              void navigate({ search: (previous) => ({ ...previous, tab: "confirmations", confirmation }) })
            }
          />
        </TabsContent>
        <TabsContent value="runs">
          <RunHistory
            tenantId={tenantId!}
            openRun={search.run}
            onOpenRun={(run) => void navigate({ search: (previous) => ({ ...previous, tab: "runs", run }) })}
          />
        </TabsContent>
      </Tabs>
    </section>
  )
}
