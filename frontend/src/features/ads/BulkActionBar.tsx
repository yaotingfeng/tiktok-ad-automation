import { Button } from "@/components/ui/button"

export function BulkActionBar({
  selectedCount,
  allMatching,
  total,
  disabled,
  onOpenPreview,
}: {
  selectedCount: number
  allMatching: boolean
  total: number
  disabled?: boolean
  onOpenPreview: () => void
}) {
  return (
    <div className="sticky bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background p-3 shadow-lg">
      <span>
        已选 {allMatching ? `全部 ${total} 条` : `${selectedCount} 条`}
      </span>
      <Button disabled={disabled} onClick={onOpenPreview}>
        批量操作
      </Button>
    </div>
  )
}
