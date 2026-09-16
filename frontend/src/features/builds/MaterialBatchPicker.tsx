import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useId, useState } from "react"
import { type MaterialPublic, MaterialsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Pager, RequestError, useCursorPage } from "@/features/tenants/shared"
import { cn } from "@/lib/utils"

export function MaterialBatchPicker({
  tenantId,
  existingIds,
  existingContentKeys,
  disabled,
  onAdd,
}: {
  tenantId: string
  existingIds: string[]
  existingContentKeys: string[]
  disabled: boolean
  onAdd: (items: MaterialPublic[]) => void
}) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  // 保存完整选项，翻页或更换搜索条件时不丢失已勾选的素材。
  const [selected, setSelected] = useState(new Map<string, MaterialPublic>())
  const paging = useCursorPage()
  const existing = new Set(existingIds)
  const existingContent = new Set(existingContentKeys)
  const selectedContent = new Set(
    [...selected.values()].map((item) => item.content_key),
  )
  const alreadyAdded = (item: MaterialPublic) =>
    existing.has(item.material_id) || existingContent.has(item.content_key)
  const chosen = [...selected.values()].filter((item) => !alreadyAdded(item))
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "builds",
      "material-picker",
      search,
      paging.cursor,
      paging.limit,
    ],
    enabled: open && !disabled,
    placeholderData: keepPreviousData,
    queryFn: async ({ signal }) =>
      (
        await MaterialsService.getMaterials({
          path: { tenant_id: tenantId },
          query: {
            query: search,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  const available = (query.data?.items || []).filter(
    (item) => !alreadyAdded(item),
  )
  const selectedOnPage = available.filter((item) =>
    selectedContent.has(item.content_key),
  ).length
  const locked = disabled || query.isFetching || !!query.error
  function changeOpen(value: boolean) {
    setOpen(value)
    // 关闭即取消本次勾选，只有确认添加才修改外层分组。
    setSelected(new Map())
    setInput("")
    setSearch("")
    paging.reset()
  }
  function toggle(rows: MaterialPublic[], checked: boolean) {
    setSelected((old) => {
      const next = new Map(old)
      for (const item of rows) {
        if (alreadyAdded(item)) continue
        if (checked) {
          // 全选本页保留已选项；不同历史文件名代表同一内容时只加入一次。
          if (
            ![...next.values()].some(
              (value) => value.content_key === item.content_key,
            )
          )
            next.set(item.material_id, item)
        } else {
          for (const [key, value] of next)
            if (value.content_key === item.content_key) next.delete(key)
        }
      }
      return next
    })
  }
  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" disabled={disabled}>
          <Plus data-icon="inline-start" />
          添加素材
        </Button>
      </DialogTrigger>
      <DialogContent className="flex max-h-[90svh] flex-col overflow-hidden sm:max-w-2xl">
        <DialogHeader className="shrink-0 text-left">
          <DialogTitle>添加素材</DialogTitle>
          <DialogDescription>
            勾选多条素材后一起添加。翻页、搜索会保留已选素材。
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex shrink-0 items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            event.stopPropagation()
            paging.reset()
            setSearch(input.trim())
          }}
        >
          <Field>
            <FieldLabel htmlFor={`${id}-search`}>搜索素材</FieldLabel>
            <Input
              id={`${id}-search`}
              placeholder="输入素材文件名"
              maxLength={255}
              value={input}
              onChange={(event) => setInput(event.target.value)}
            />
          </Field>
          <Button type="submit" variant="outline" disabled={disabled}>
            搜索
          </Button>
        </form>
        <Field orientation="horizontal" className="shrink-0 gap-3">
          <Checkbox
            id={`${id}-all`}
            disabled={locked || !available.length}
            checked={
              selectedOnPage && selectedOnPage === available.length
                ? true
                : selectedOnPage
                  ? "indeterminate"
                  : false
            }
            onCheckedChange={(checked) => toggle(available, checked === true)}
          />
          <FieldLabel htmlFor={`${id}-all`}>全选本页</FieldLabel>
          <span className="text-xs text-muted-foreground">
            本页可选 {available.length} 条
          </span>
        </Field>
        <div
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
          aria-busy={query.isFetching}
        >
          {query.isFetching && (
            <p role="status" className="py-3 text-sm text-muted-foreground">
              正在加载素材…
            </p>
          )}
          {query.error && (
            <RequestError
              error={query.error}
              retry={() => void query.refetch()}
            />
          )}
          {!query.isPending && !query.error && !query.data?.items.length && (
            <p
              role="status"
              className="py-6 text-center text-sm text-muted-foreground"
            >
              没有找到素材，请调整搜索关键词。
            </p>
          )}
          <ul aria-label="可添加的素材" className="flex flex-col gap-2">
            {query.data?.items.map((item) => {
              const added = alreadyAdded(item)
              const checked = added || selectedContent.has(item.content_key)
              return (
                <li key={item.material_id}>
                  <FieldLabel
                    htmlFor={`${id}-${item.material_id}`}
                    className={cn(
                      "flex items-start gap-3 rounded-lg border p-4 font-normal",
                      added
                        ? "bg-muted/40 text-muted-foreground"
                        : "cursor-pointer hover:bg-accent",
                      !added && checked && "border-primary bg-accent",
                    )}
                  >
                    <Checkbox
                      id={`${id}-${item.material_id}`}
                      aria-label={item.file_name}
                      className="mt-0.5"
                      checked={checked}
                      disabled={locked || added}
                      onCheckedChange={(value) =>
                        toggle([item], value === true)
                      }
                    />
                    <span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">
                      {item.file_name}
                      <span className="block text-xs text-muted-foreground">
                        上传来源 BC：{item.bc_id}
                      </span>
                    </span>
                    {added && (
                      <Badge variant="secondary" className="shrink-0">
                        已添加
                      </Badge>
                    )}
                  </FieldLabel>
                </li>
              )
            })}
          </ul>
        </div>
        <div className="flex shrink-0 flex-col gap-4 border-t pt-4">
          <Pager
            paging={paging}
            nextCursor={query.data?.next_cursor}
            total={query.data?.total}
            busy={locked}
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span role="status" className="text-sm">
                已选 {chosen.length} 条
              </span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={!chosen.length}
                onClick={() => setSelected(new Map())}
              >
                清空已选
              </Button>
            </div>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => changeOpen(false)}
              >
                取消
              </Button>
              <Button
                type="button"
                disabled={disabled || !chosen.length}
                onClick={() => {
                  onAdd(chosen)
                  changeOpen(false)
                }}
              >
                添加{chosen.length ? ` ${chosen.length} 条素材` : "所选素材"}
              </Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            添加到最后一组，可继续调整组号，保存素材分组后生效。
          </p>
        </div>
      </DialogContent>
    </Dialog>
  )
}
