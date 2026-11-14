import { useQuery } from "@tanstack/react-query"
import { aiRequest } from "@/features/ai-workbench/api"
import type { AIOptimizer } from "@/features/ai-workbench/AIOptimizerList"
import { getRouteApi } from "@tanstack/react-router"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { canManage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { Confirmations } from "./Confirmations"
import { RuleList } from "./RuleList"
import { RunHistory } from "./RunHistory"

const route = getRouteApi("/_layout/tenants/$tenantId/automation-rules")

export function mergeAutomationSearch<T extends object, P extends object>(
  previous: T,
  patch: P,
): T & P {
  return { ...previous, ...patch }
}

export function canWriteAutomationContext(
  write: boolean,
  boundContext: boolean,
) {
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
  // 以实际绑定为准，URL 里的 rule_ids 不能决定显示范围。
  const optimizer = useQuery({
    queryKey: ["tenant", tenantId, "ai-optimizer", search.ai_optimizer_id],
    queryFn: ({ signal }) =>
      aiRequest<AIOptimizer>(
        `/api/tenants/${tenantId}/ai-optimizers/${search.ai_optimizer_id}`,
        { signal },
      ),
    enabled: boundContext,
  })
  const boundRuleIds = optimizer.data?.rule_ids ?? []
  const tab = boundContext ? "rules" : (search.tab ?? "rules")
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
          当前仅查看投手 {search.ai_optimizer_id}{" "}
          已绑定的规则；租户其他规则不会被修改。保存返回将回到{" "}
          {search.return_to ?? "AI 工作台"}。
        </p>
      )}
      <Tabs
        value={tab}
        onValueChange={(value) =>
          void navigate({
            search: (previous) =>
              mergeAutomationSearch(previous, {
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
          {!boundContext && (
            <TabsTrigger value="confirmations">待确认</TabsTrigger>
          )}
          {!boundContext && <TabsTrigger value="runs">执行记录</TabsTrigger>}
        </TabsList>
        <TabsContent value="rules">
          {boundContext && optimizer.isPending ? (
            <p role="status">正在读取绑定规则…</p>
          ) : boundContext && optimizer.error ? (
            <p role="alert">绑定规则读取失败，请刷新重试。</p>
          ) : (
            <RuleList
              tenantId={tenantId!}
              write={canWriteAutomationContext(write, boundContext)}
              admin={admin}
              boundOnly={boundContext}
              boundRuleIds={boundRuleIds}
            />
          )}
        </TabsContent>
        <TabsContent value="confirmations">
          <Confirmations
            tenantId={tenantId!}
            write={canWriteAutomationContext(write, boundContext)}
            openConfirmation={search.confirmation}
            onOpenConfirmation={(confirmation) =>
              void navigate({
                search: (previous) =>
                  mergeAutomationSearch(previous, {
                    tab: "confirmations",
                    confirmation,
                  }),
              })
            }
          />
        </TabsContent>
        <TabsContent value="runs">
          <RunHistory
            tenantId={tenantId!}
            openRun={search.run}
            onOpenRun={(run) =>
              void navigate({
                search: (previous) =>
                  mergeAutomationSearch(previous, { tab: "runs", run }),
              })
            }
          />
        </TabsContent>
      </Tabs>
    </section>
  )
}
