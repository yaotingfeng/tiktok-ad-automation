import type { MaterialUsePublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

const STATUS: Record<string, string> = {
  ENABLE: "投放中",
  DISABLE: "已暂停",
  DELETE: "已删除",
}

/** 广告：列出素材使用位置；系列/广告组：给出汇总数并跳到素材维度查看。 */
export function MaterialsTab({
  materials,
  scopeCount,
  onShowMaterials,
}: {
  materials?: MaterialUsePublic[]
  scopeCount?: number
  onShowMaterials?: () => void
}) {
  if (!materials)
    return (
      <div className="flex flex-col items-start gap-2 text-sm">
        <p className="text-muted-foreground">
          {scopeCount !== undefined
            ? `该范围在所选日期有 ${scopeCount} 条素材使用记录；`
            : ""}
          该范围的素材请在素材维度查看。
        </p>
        {onShowMaterials && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onShowMaterials}
          >
            在素材维度查看
          </Button>
        )}
      </div>
    )
  if (!materials.length)
    return (
      <p className="text-sm text-muted-foreground">
        暂无素材使用记录；外部广告仍保留。
      </p>
    )
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>名称</TableHead>
            <TableHead>类型</TableHead>
            <TableHead>状态</TableHead>
            <TableHead>来源</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {materials.map((material) => (
            <TableRow
              key={`${material.use_ref.platform_material_id}:${material.use_ref.ad_material_id ?? ""}`}
            >
              <TableCell className="max-w-64 truncate" title={material.name}>
                {material.name || material.use_ref.platform_material_id}
              </TableCell>
              <TableCell>{material.use_ref.material_type}</TableCell>
              <TableCell>
                {material.operation_status
                  ? (STATUS[material.operation_status] ??
                    material.operation_status)
                  : "—"}
              </TableCell>
              <TableCell>
                <Badge variant={material.external ? "outline" : "secondary"}>
                  {material.external ? "外部素材" : "本地素材"}
                </Badge>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
