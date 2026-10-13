import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { RuleList } from "./RuleList"

export function AutomationRulesPage() {
  const { tenantId, scope } = useTenantScope()
  const write = !!scope && scope.role !== "viewer"
  return (
    <section
      aria-label="自动规则工作区"
      className="flex min-w-0 flex-col gap-6"
    >
      <div className="flex min-w-0 flex-col gap-1">
        <WorkspacePageTitle>自动规则</WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          按条件自动检查跨账户、跨 BC 的投放数据，并发送飞书预警。
        </p>
      </div>
      <RuleList tenantId={tenantId!} write={write} />
    </section>
  )
}
