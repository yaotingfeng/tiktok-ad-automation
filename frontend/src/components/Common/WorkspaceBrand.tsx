import { ArrowUpRight } from "lucide-react"

export function WorkspaceBrand() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="brand-mark flex size-8 shrink-0 items-center justify-center rounded-lg">
        <ArrowUpRight className="size-4" />
      </span>
      <div className="brand-copy min-w-0">
        <p className="text-sm font-semibold">短剧投放</p>
        <p className="text-xs text-muted-foreground">TikTok 工作台</p>
      </div>
    </div>
  )
}
