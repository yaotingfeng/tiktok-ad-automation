import { cn } from "@/lib/utils"

export function paginationPageCount(total: number, pageSize: number) {
  return Math.max(1, Math.ceil(total / pageSize))
}

export function PaginationSummary({
  total,
  page,
  pageSize,
  className,
}: {
  total: number | undefined
  page: number
  pageSize: number
  className?: string
}) {
  return (
    <span className={cn("text-sm text-muted-foreground", className)}>
      {total === undefined
        ? `第 ${page} 页`
        : `共 ${total} 条 · 第 ${page} / ${paginationPageCount(total, pageSize)} 页`}
    </span>
  )
}
