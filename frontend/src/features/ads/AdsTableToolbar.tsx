import { ChevronDown, ChevronUp } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import type { ManagementAction } from "@/features/ad-management/ManagementPreviewSheet"
import { type ColumnPrefs, DEFAULT_HIDDEN } from "./columnPrefs"
import type { ColumnDef } from "./metricCatalog"
import type { AdsDimension } from "./search"

export type ToolbarActionKey = "enable" | "disable" | "roas" | "budget"

// 各维度工具栏提供的动作（规格 5.1）：账户与剧没有可直接修改的目标 ROAS，
// 广告的预算归属广告组或系列，素材只有独立启停，其余在预览中按层级展开。
const DIMENSION_ACTIONS: Record<AdsDimension, ToolbarActionKey[]> = {
  account: ["enable", "disable", "budget"],
  campaign: ["enable", "disable", "roas", "budget"],
  adgroup: ["enable", "disable", "roas", "budget"],
  ad: ["enable", "disable", "roas"],
  material: ["enable", "disable"],
  drama: ["enable", "disable", "budget"],
}

export function actionsFor(dimension: AdsDimension): ToolbarActionKey[] {
  return DIMENSION_ACTIONS[dimension]
}

const ACTIONS: Record<
  ToolbarActionKey,
  { label: string; action: ManagementAction }
> = {
  enable: { label: "启用", action: { field: "status", value: "ENABLE" } },
  disable: { label: "暂停", action: { field: "status", value: "DISABLE" } },
  roas: { label: "修改目标 ROAS", action: { field: "roas" } },
  budget: { label: "修改预算", action: { field: "budget" } },
}

/** 工具栏动作键对应的管理动作；详情面板的单对象操作复用同一映射。 */
export function toolbarAction(key: ToolbarActionKey): ManagementAction {
  return ACTIONS[key].action
}

/** 工具栏动作文案；详情面板页头沿用同一文案，两处保持一致。 */
export function toolbarActionLabel(key: ToolbarActionKey): string {
  return ACTIONS[key].label
}

export type ToolbarSelection = {
  selectedCount: number
  allMatching: boolean
  total: number
  dimension: AdsDimension
  readonly: boolean
  busy: boolean
  onAction: (action: ManagementAction) => void
  onSelectAllMatching: () => void
  onClear: () => void
}

function SelectionToolbar({
  selectedCount,
  allMatching,
  total,
  dimension,
  readonly,
  busy,
  onAction,
  onSelectAllMatching,
  onClear,
}: ToolbarSelection) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/40 px-3 py-2 text-sm">
      <span>
        {allMatching
          ? `已选全部 ${total} 条匹配结果`
          : `已选 ${selectedCount} 条`}
      </span>
      <div className="flex flex-wrap items-center gap-2">
        {/* 只读成员只能看到计数与取消选择，不显示任何写入动作和跨页全选。 */}
        {!readonly &&
          actionsFor(dimension).map((key) => (
            <Button
              key={key}
              type="button"
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => onAction(toolbarAction(key))}
            >
              {toolbarActionLabel(key)}
            </Button>
          ))}
        {!readonly && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={allMatching}
            onClick={onSelectAllMatching}
          >
            选择全部匹配结果
          </Button>
        )}
        <Button type="button" variant="ghost" size="sm" onClick={onClear}>
          取消选择
        </Button>
      </div>
    </div>
  )
}

export function AdsTableToolbar({
  columns,
  prefs,
  onPrefsChange,
  selection,
}: {
  columns: ColumnDef[]
  prefs: ColumnPrefs | null
  onPrefsChange: (prefs: ColumnPrefs) => void
  selection: ToolbarSelection
}) {
  // 勾选任意一行（或进入全部匹配模式）后，工具栏整体切换为批量操作态。
  if (selection.selectedCount > 0 || selection.allMatching)
    return <SelectionToolbar {...selection} />
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
