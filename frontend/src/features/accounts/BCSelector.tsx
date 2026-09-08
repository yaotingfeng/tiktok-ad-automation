import { AccountsService, type BCPublic } from "@/client"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"
import { useTenantScope } from "@/features/tenants/TenantScope"

export function BCSelector() {
  const { tenantId, bc, bcDirectory, bcPending, bcError, switchBC } =
    useTenantScope()
  if (bcPending) return <span role="status">正在读取 BC…</span>
  if (bcError) return <span className="text-destructive">BC 读取失败</span>
  if (!bcDirectory?.items.length)
    return <span className="text-muted-foreground">BC 未连接</span>
  if (bcDirectory.items.length === 1 && !bcDirectory.next_cursor && bc)
    return (
      <span className="flex min-w-0 flex-col text-sm">
        <strong className="break-all">{bc.name || "未命名 BC"}</strong>
        <span className="font-mono text-xs text-muted-foreground">
          {bc.bc_id}
        </span>
      </span>
    )
  return (
    <DirectoryPicker<BCPublic & { id: string }>
      label="当前 BC"
      valueLabel={bc ? `${bc.name || "未命名 BC"} · ${bc.bc_id}` : undefined}
      queryKey={["tenant", tenantId, "bcs", "selector"]}
      load={async (query, cursor, limit, signal) => {
        const { data } = await AccountsService.getBcs({
          path: { tenant_id: tenantId! },
          query: { query, cursor, limit },
          signal,
        })
        return {
          ...data,
          items: data.items.map((item) => ({ ...item, id: item.bc_id })),
        }
      }}
      renderItem={(item) => (
        <span>
          {item.name || "未命名 BC"}
          {item.ownership_conflict ? " · 归属冲突" : ""}
        </span>
      )}
      onSelect={switchBC}
    />
  )
}
