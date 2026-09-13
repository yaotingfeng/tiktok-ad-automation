import { useQuery } from "@tanstack/react-query"
import { ProvidersService } from "@/client"
import { CopyField } from "@/features/providers/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { RequestError } from "@/features/tenants/shared"
import { BuildStatus } from "./presentation"
export function BuildLinkSheet({
  tenantId,
  linkId,
  onClose,
}: {
  tenantId: string
  linkId: string
  onClose: () => void
}) {
  const query = useQuery({
    queryKey: ["tenant", tenantId, "providers", "link", linkId],
    queryFn: async ({ signal }) =>
      (
        await ProvidersService.linkDetails({
          path: { tenant_id: tenantId, link_id: linkId },
          signal,
        })
      ).data,
  })
  return (
    <ManagementSheet
      title="推广链接详情"
      description="本次草稿使用的真实链接，属于当前租户。"
      dirty={false}
      onClose={onClose}
    >
      <div className="space-y-4 text-sm">
        {query.error && (
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
        )}{" "}
        {query.isPending && <p role="status">正在读取推广链接…</p>}
        {query.data && (
          <>
            <h3 className="font-semibold">{query.data.title}</h3>
            <p>
              {query.data.connection_name} · {query.data.application_name}
            </p>
            <p>
              <BuildStatus value={query.data.status} /> · v{query.data.version}
            </p>
            <CopyField label="推广链接" value={query.data.url} expanded />
            <CopyField
              label="归因名称"
              value={query.data.protected_base}
              expanded
            />
            <CopyField
              label="应用 ID"
              value={query.data.application_id}
              expanded
            />
            {query.data.config_display_incomplete && (
              <p>部分已有配置尚未核实。</p>
            )}
          </>
        )}
      </div>
    </ManagementSheet>
  )
}
