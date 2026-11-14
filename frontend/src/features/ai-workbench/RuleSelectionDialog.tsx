import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog } from "@/components/ui/dialog"
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { PickerDialogContent } from "@/features/tenants/PickerDialogContent"

const PAGE_SIZE = 50

/** 规则已从租户目录取齐，筛选与跨页选择仅修改草稿，确认才写回投手表单。 */
export function RuleSelectionDialog({
  open,
  onOpenChange,
  rules,
  value,
  onChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  rules: Array<{ id: string; name: string }>
  value: string[]
  onChange: (value: string[]) => void
}) {
  const [query, setQuery] = useState("")
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState(() => new Set(value))
  const keywords = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  const filtered = rules.filter((rule) =>
    keywords.every((word) =>
      `${rule.name} ${rule.id}`.toLocaleLowerCase().includes(word),
    ),
  )
  const shown = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const count = shown.filter((rule) => selected.has(rule.id)).length
  function toggle(ids: string[], checked: boolean) {
    setSelected((old) => {
      const next = new Set(old)
      for (const id of ids) {
        if (checked) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <PickerDialogContent
        title="选择绑定规则"
        description="搜索已有自动规则；AI 新建规则仍按投手的管理账户范围校验。"
        contained
      >
        <Field>
          <FieldLabel htmlFor="ai-rule-search">搜索规则</FieldLabel>
          <Input
            id="ai-rule-search"
            placeholder="按规则名称或 ID 搜索"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setPage(0)
            }}
          />
        </Field>
        <div className="flex shrink-0 items-center justify-between gap-2 px-3">
          <Field orientation="horizontal" className="w-auto">
            <Checkbox
              id="ai-rules-page"
              disabled={!shown.length}
              checked={
                count > 0 && count === shown.length
                  ? true
                  : count > 0
                    ? "indeterminate"
                    : false
              }
              onCheckedChange={(checked) =>
                toggle(
                  shown.map((rule) => rule.id),
                  checked === true,
                )
              }
            />
            <FieldLabel htmlFor="ai-rules-page">全选本页</FieldLabel>
          </Field>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!filtered.length}
            onClick={() =>
              toggle(
                filtered.map((rule) => rule.id),
                true,
              )
            }
          >
            全选筛选结果
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto rounded-md border">
          <FieldSet className="gap-0">
            <FieldLegend className="sr-only">自动规则列表</FieldLegend>
            <FieldGroup className="gap-0 divide-y">
              {shown.map((rule) => (
                <Field
                  key={rule.id}
                  orientation="horizontal"
                  className="px-3 py-3"
                >
                  <Checkbox
                    id={`ai-rule-${rule.id}`}
                    checked={selected.has(rule.id)}
                    onCheckedChange={(checked) =>
                      toggle([rule.id], checked === true)
                    }
                  />
                  <FieldLabel
                    htmlFor={`ai-rule-${rule.id}`}
                    className="min-w-0 break-all"
                  >
                    {rule.name}
                  </FieldLabel>
                </Field>
              ))}
            </FieldGroup>
          </FieldSet>
          {!shown.length && (
            <p className="p-4 text-sm text-muted-foreground">
              没有符合条件的规则
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center justify-between gap-2 text-sm">
          <span>
            共 {filtered.length} 条 · 第 {page + 1} 页
          </span>
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={page === 0}
              onClick={() => setPage(page - 1)}
            >
              上一页
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={(page + 1) * PAGE_SIZE >= filtered.length}
              onClick={() => setPage(page + 1)}
            >
              下一页
            </Button>
          </div>
        </div>
        <Separator />
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <span role="status" className="text-sm">
            已选 {selected.size} 条规则
          </span>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={!selected.size}
              onClick={() => setSelected(new Set())}
            >
              清空勾选
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              onClick={() => {
                onChange([...selected])
                onOpenChange(false)
              }}
            >
              确定选择
            </Button>
          </div>
        </div>
      </PickerDialogContent>
    </Dialog>
  )
}
