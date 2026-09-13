import { useNavigate, useRouterState } from "@tanstack/react-router"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { canManage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { BuildInputPage } from "./BuildInputPage"
import { DraftList } from "./DraftList"
export function BuildWorkspace() {
  const { tenantId, scope, bc, bcPending } = useTenantScope()
  const navigate = useNavigate()
  const search = useRouterState({
    select: (state) => state.location.search as { view?: string },
  })
  const view = search.view === "drafts" ? "drafts" : "new"
  if (!tenantId || !scope?.bcId || !bc)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title={scope?.bcId ? "请先选择有效的 BC" : "尚未连接 TikTok BC"}
        description={
          bcPending
            ? "正在读取 BC 上下文。"
            : "请通过顶栏选择本次搭建使用的 BC。"
        }
      />
    )
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex min-w-0 flex-col gap-1">
        <WorkspacePageTitle>广告搭建</WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          批量输入剧目和账户，自动准备推广链接与素材。
        </p>
      </div>
      <Tabs
        value={view}
        onValueChange={(value) => {
          // 通过路由切换，让输入页既有离页守卫保护未保存的编辑。
          void navigate({
            to: "/tenants/$tenantId/builds/new",
            params: { tenantId },
            search: {
              bc_id: scope.bcId!,
              view: value === "drafts" ? "drafts" : undefined,
            },
          })
        }}
      >
        <TabsList aria-label="搭建入口">
          <TabsTrigger value="new">新建搭建</TabsTrigger>
          <TabsTrigger value="drafts">未完成搭建</TabsTrigger>
        </TabsList>
      </Tabs>
      {view === "drafts" ? (
        <DraftList
          key={`${tenantId}:${scope.bcId}`}
          tenantId={tenantId}
          bcId={scope.bcId}
          write={scope.role !== "viewer"}
        />
      ) : (
        <BuildInputPage
          key={`${tenantId}:${scope.bcId}`}
          tenantId={tenantId}
          bcId={scope.bcId}
          write={scope.role !== "viewer"}
        />
      )}
    </div>
  )
}
