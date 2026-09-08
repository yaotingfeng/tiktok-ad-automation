import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { BuildInputPage } from "./BuildInputPage"
export function BuildWorkspace() {
  const { tenantId, scope, bc, bcPending } = useTenantScope()
  if (!tenantId || !scope?.bcId || !bc)
    return (
      <WorkspaceEmpty
        title={scope?.bcId ? "请先选择有效的 BC" : "尚未连接 TikTok BC"}
        description={
          bcPending
            ? "正在读取 BC 上下文。"
            : "请通过顶栏选择本次搭建使用的 BC。"
        }
      />
    )
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">广告搭建</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          批量输入剧目和账户，自动准备推广链接与素材。
        </p>
      </div>
      <BuildInputPage
        key={`${tenantId}:${scope.bcId}`}
        tenantId={tenantId}
        bcId={scope.bcId}
        write={scope.role !== "viewer"}
      />
    </div>
  )
}
