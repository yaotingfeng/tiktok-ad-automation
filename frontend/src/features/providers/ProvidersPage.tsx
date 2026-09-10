import { useNavigate, useSearch } from "@tanstack/react-router"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { ConnectionPanel } from "./ConnectionPanel"
import { LinkHistory } from "./LinkHistory"
import { LinkResultTable } from "./LinkResultTable"
export function ProvidersPage() {
  const { tenantId, tenant } = useTenantScope(),
    search = useSearch({ from: "/_layout/tenants/$tenantId/providers" }),
    navigate = useNavigate()
  const go = (tab: "connections" | "links", connection_id?: string) => {
    void navigate({
      to: "/tenants/$tenantId/providers",
      params: { tenantId: tenantId! },
      search: { bc_id: search.bc_id, tab, connection_id },
    })
  }
  return (
    <>
      <div className="flex min-w-0 flex-col gap-1">
        <WorkspacePageTitle>版权方连接</WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          当前租户：{tenant?.name} · 连接与推广链接属于租户，不按 BC 筛选。
        </p>
      </div>
      <Tabs
        className="gap-4"
        value={search.tab}
        onValueChange={(tab) => go(tab === "links" ? "links" : "connections")}
      >
        <TabsList>
          <TabsTrigger value="connections">连接</TabsTrigger>
          <TabsTrigger value="links">链接记录</TabsTrigger>
        </TabsList>
        <TabsContent value="connections">
          <ConnectionPanel onLinks={(id) => go("links", id)} />
        </TabsContent>
        <TabsContent value="links">
          {search.task_id ? (
            <LinkResultTable key={search.task_id} taskId={search.task_id} />
          ) : (
            <LinkHistory
              key={search.connection_id || "all"}
              connectionId={search.connection_id}
              onConnection={(id) => go("links", id)}
            />
          )}
        </TabsContent>
      </Tabs>
    </>
  )
}
