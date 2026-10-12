import { ChevronDown, ChevronUp } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { type ColumnPrefs, DEFAULT_HIDDEN } from "./columnPrefs"
import type { ColumnDef } from "./metricCatalog"

export function AdsTableToolbar({
  columns,
  prefs,
  onPrefsChange,
}: {
  columns: ColumnDef[]
  prefs: ColumnPrefs | null
  onPrefsChange: (prefs: ColumnPrefs) => void
}) {
  const hidden = prefs?.hidden ?? DEFAULT_HIDDEN
  const order = prefs?.order ?? columns.map((c) => c.key)

  const toggleColumn = (key: string) => {
    const newHidden = hidden.includes(key)
      ? hidden.filter((h) => h !== key)
      : [...hidden, key]
    onPrefsChange({ hidden: newHidden, order })
  }

  const moveUp = (key: string) => {
    const index = order.indexOf(key)
    if (index <= 0) return
    const newOrder = [...order]
    ;[newOrder[index - 1], newOrder[index]] = [
      newOrder[index],
      newOrder[index - 1],
    ]
    onPrefsChange({ hidden, order: newOrder })
  }

  const moveDown = (key: string) => {
    const index = order.indexOf(key)
    if (index === -1 || index >= order.length - 1) return
    const newOrder = [...order]
    ;[newOrder[index], newOrder[index + 1]] = [
      newOrder[index + 1],
      newOrder[index],
    ]
    onPrefsChange({ hidden, order: newOrder })
  }

  const restoreDefault = () => {
    onPrefsChange({
      hidden: DEFAULT_HIDDEN,
      order: columns.map((c) => c.key),
    })
  }

  // 按当前顺序排列列
  const sortedColumns = [...columns].sort((a, b) => {
    const aIndex = order.indexOf(a.key)
    const bIndex = order.indexOf(b.key)
    const aRank = aIndex === -1 ? 1e6 + columns.indexOf(a) : aIndex
    const bRank = bIndex === -1 ? 1e6 + columns.indexOf(b) : bIndex
    return aRank - bRank
  })

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          列设置
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <div className="max-h-96 overflow-y-auto">
          {sortedColumns.map((column, index) => (
            <div
              key={column.key}
              className="flex items-center gap-2 px-2 py-1 hover:bg-muted/50"
            >
              <DropdownMenuCheckboxItem
                checked={!hidden.includes(column.key)}
                onCheckedChange={() => toggleColumn(column.key)}
                onSelect={(e) => e.preventDefault()}
                className="flex-1"
              >
                {column.label}
              </DropdownMenuCheckboxItem>
              <div className="flex gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 w-6 p-0"
                  disabled={index === 0}
                  onClick={(e) => {
                    e.stopPropagation()
                    moveUp(column.key)
                  }}
                  aria-label={`上移 ${column.label}`}
                >
                  <ChevronUp className="h-3 w-3" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 w-6 p-0"
                  disabled={index === sortedColumns.length - 1}
                  onClick={(e) => {
                    e.stopPropagation()
                    moveDown(column.key)
                  }}
                  aria-label={`下移 ${column.label}`}
                >
                  <ChevronDown className="h-3 w-3" />
                </Button>
              </div>
            </div>
          ))}
        </div>
        <DropdownMenuSeparator />
        <div className="p-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-full"
            onClick={restoreDefault}
          >
            恢复默认
          </Button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
