import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { AdsSearch } from "./search"
export function AdsFilters({
  search,
  onChange,
  onApply,
  onReset,
}: {
  search: AdsSearch
  onChange: (next: Partial<AdsSearch>) => void
  onApply: () => void
  onReset: () => void
}) {
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        onApply()
      }}
    >
      <div className="space-y-1.5">
        <Label htmlFor="ads-start">开始日期</Label>
        <Input
          id="ads-start"
          type="date"
          value={search.start_date}
          onChange={(event) => onChange({ start_date: event.target.value })}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="ads-end">结束日期</Label>
        <Input
          id="ads-end"
          type="date"
          value={search.end_date}
          onChange={(event) => onChange({ end_date: event.target.value })}
        />
      </div>
      <div className="min-w-56 flex-1 space-y-1.5">
        <Label htmlFor="ads-search">搜索广告</Label>
        <Input
          id="ads-search"
          aria-label="搜索广告"
          value={search.query ?? ""}
          onChange={(event) => onChange({ query: event.target.value })}
          placeholder="名称、剧名、版权方或 ID（多关键词 AND）"
        />
      </div>
      <Button type="submit" aria-label="应用筛选">
        应用筛选
      </Button>
      <Button type="button" variant="outline" onClick={onReset}>
        重置
      </Button>
    </form>
  )
}
