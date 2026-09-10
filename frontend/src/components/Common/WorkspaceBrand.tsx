import { ArrowUpRight } from "lucide-react"

export function WorkspaceBrand() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="brand-mark flex size-8 shrink-0 items-center justify-center rounded-lg">
        <ArrowUpRight className="size-4" />
      </span>
      <div className="brand-copy min-w-0">
        <p className="text-lg leading-6 font-semibold">TK-ADA</p>
        <p className="text-[11px] leading-4 text-muted-foreground">
          广告投放工具
        </p>
      </div>
    </div>
  )
}
