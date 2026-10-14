import { getRouteApi } from "@tanstack/react-router"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { canManage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { Confirmations } from "./Confirmations"
import { RuleList } from "./RuleList"
import { RunHistory } from "./RunHistory"

const route = getRouteApi("/_layout/tenants/$tenantId/automation-rules")

export function AutomationRulesPage() {
  const { tenantId, scope } = useTenantScope()
  const search = route.useSearch()
  const navigate = route.useNavigate()
  const write = !!scope && scope.role !== "viewer"
  // 平台管理员与租户管理员都能保存自动执行规则（与后端 manage 权限一致）。
  const admin = canManage(scope?.role)
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
      <Tabs
        value={tab}
        onValueChange={(value) =>
          void navigate({
            search: {
              tab:
                value === "runs" || value === "confirmations"
                  ? value
                  : undefined,
            },
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
          <RuleList tenantId={tenantId!} write={write} admin={admin} />
        </TabsContent>
        <TabsContent value="confirmations">
          <Confirmations
            tenantId={tenantId!}
            write={write}
            openConfirmation={search.confirmation}
            onOpenConfirmation={(confirmation) =>
              void navigate({ search: { tab: "confirmations", confirmation } })
            }
          />
        </TabsContent>
        <TabsContent value="runs">
          <RunHistory
            tenantId={tenantId!}
            openRun={search.run}
            onOpenRun={(run) => void navigate({ search: { tab: "runs", run } })}
          />
        </TabsContent>
      </Tabs>
    </section>
  )
}
