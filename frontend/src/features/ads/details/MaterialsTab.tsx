import type { MaterialUsePublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

const STATUS: Record<string, string> = {
  ENABLE: "已开启",
  DISABLE: "已暂停",
  DELETE: "已删除",
}

/** 广告与素材详情：列出素材使用位置。 */
export function MaterialsTab({
  materials,
}: {
  materials: MaterialUsePublic[]
}) {
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
