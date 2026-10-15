import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react"
import { Button } from "@/components/ui/button"

// 表头按钮去掉内边距（含小号按钮带图标时的 has-[>svg]:px-2.5），
// 否则表头文字比下方单元格多缩进，看起来与数据错位。
export function SortableHead({
  label,
  sortBy,
  activeSortBy,
  direction,
  onSort,
}: {
  label: string
  sortBy: string
  activeSortBy: string | undefined
  direction: "asc" | "desc"
  onSort: (sortBy: string) => void
}) {
  const active = activeSortBy === sortBy
  const Icon = active
    ? direction === "asc"
      ? ArrowUp
      : ArrowDown
    : ArrowUpDown
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-8 gap-1 px-0 font-medium text-inherit hover:bg-transparent has-[>svg]:px-0"
      onClick={() => onSort(sortBy)}
      aria-label={`${label}${active ? (direction === "asc" ? "，当前升序" : "，当前降序") : "，点击排序"}`}
    >
      {label}
      <Icon className="size-3.5" aria-hidden="true" />
    </Button>
  )
}
