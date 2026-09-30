import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type EntityRef, type ReportRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { rowName } from "./search"
export function AdsDetails({
  ref,
  row,
  onClose,
}: {
  ref?: EntityRef | null
  row?: ReportRow | null
  onClose: () => void
}) {
  const { tenantId, bc } = useTenantScope()
  const query = useQuery({
    queryKey: ["tenant", tenantId, "ads", "detail", bc?.bc_id, ref],
    enabled: !!tenantId && !!bc && !!ref && ref.kind !== "creative",
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adDetail({
          path: {
            tenant_id: tenantId!,
            kind: ref!.kind as "campaign" | "adgroup" | "ad" | "creative",
            remote_id: ref!.remote_id,
          },
          query: { advertiser_id: ref!.advertiser_id, bc_id: bc!.bc_id },
          signal,
        })
      ).data,
  })
  const open = !!row || !!ref
  const detail = query.data
  return (
    <Sheet
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose()
      }}
    >
      <SheetContent>
        <SheetHeader>
          <SheetTitle>
            {detail?.name ?? (row ? rowName(row) : "广告详情")}
          </SheetTitle>
          <SheetDescription>
            {ref?.kind ?? "report"} · {ref?.remote_id ?? row?.row_key}
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-4 px-4 pb-4 text-sm">
          <div className="flex items-center gap-2">
            <Badge variant="outline">{detail?.ad_type ?? "外部广告"}</Badge>
            <span>
              {detail?.statuses?.operation_status ??
                row?.display?.status ??
                "状态待同步"}
            </span>
          </div>
          {detail?.parent && (
            <div className="rounded-md border p-3">
              <strong>父级限制</strong>
              <p className="text-muted-foreground">
                {detail.parent.reasons?.join("；") ?? "父级状态会限制实际投放"}
              </p>
            </div>
          )}
          <div>
            <h3 className="font-medium">素材使用位置</h3>
            <p className="text-muted-foreground">
              {detail?.materials?.length
                ? `${detail.materials.length} 个素材引用`
                : row?.material_uses?.length
                  ? `${row.material_uses.length} 个素材引用`
                  : "暂无本地素材映射；外部广告仍保留"}
            </p>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}
