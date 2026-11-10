import { TargetingSummary } from "@/features/targeting/TargetingForm"
import type { CopyGroupSetting } from "./api"

// 复制预览的各组设置来自后端冻结合同，不读取普通搭建单元的默认场景值。
export function CopyFrozenGroups({
  groups,
  currency,
}: {
  groups: CopyGroupSetting[]
  currency: string
}) {
  return (
    <div className="flex flex-col gap-3">
      {groups.map((group, index) => (
        <div key={`${group.name}:${index}`} className="rounded-md border p-3">
          <p className="text-sm font-medium">{group.name}</p>
          <p className="text-sm text-muted-foreground">
            冻结组预算：
            {group.budget === null
              ? "系列统一预算"
              : `${currency} ${group.budget}`}{" "}
            · 冻结目标 ROAS：{group.target_roas ?? "最高价值"}
          </p>
          {group.targeting ? (
            <TargetingSummary value={group.targeting} />
          ) : (
            <p className="text-sm text-muted-foreground">
              {group.targeting_summary || "按来源实际定向保留"}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}
