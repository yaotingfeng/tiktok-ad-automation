import {
  keepPreviousData,
  type QueryKey,
  useQuery,
} from "@tanstack/react-query"
import { ChevronsUpDown } from "lucide-react"
import { type ReactNode, useId, useState } from "react"
import { Button } from "@/components/ui/button"
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
import { Skeleton } from "@/components/ui/skeleton"
import { Pager, RequestError, useCursorPage } from "./shared"

export function DirectoryPicker<T extends { id: string }>({
  label,
  valueLabel,
  queryKey,
  load,
  renderItem,
  onSelect,
  requiredSearch = false,
  disabled = false,
  invalid = false,
  describedBy,
}: {
  label: string
  valueLabel?: string
  queryKey: QueryKey
  load: (
    query: string,
    cursor: string | null,
    limit: number,
    signal: AbortSignal,
  ) => Promise<{ items: T[]; next_cursor?: string | null }>
  renderItem: (item: T) => ReactNode
  onSelect: (item: T) => void
  requiredSearch?: boolean
  disabled?: boolean
  invalid?: boolean
  describedBy?: string
}) {
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const paging = useCursorPage()
  const id = useId()
  const enabled = open && (!requiredSearch || !!search)
  const query = useQuery({
    queryKey: [...queryKey, search, paging.cursor, paging.limit],
    queryFn: ({ signal }) => load(search, paging.cursor, paging.limit, signal),
    enabled,
    placeholderData: keepPreviousData,
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-label={label}
          aria-expanded={open}
          aria-controls={id}
          aria-haspopup="dialog"
          aria-invalid={invalid}
          aria-describedby={describedBy}
          disabled={disabled}
          className="h-auto min-h-9 max-w-full justify-between whitespace-normal text-left"
        >
          <span className="break-all">{valueLabel || `选择${label}`}</span>
          <ChevronsUpDown data-icon="inline-end" />
        </Button>
      </DialogTrigger>
      <DialogContent id={id} className="max-h-[90svh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>选择{label}</DialogTitle>
          <DialogDescription>
            搜索已有记录并选择。名称和完整 ID 用于核对。
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            // Portal events still bubble through the enclosing management form.
            event.preventDefault()
            event.stopPropagation()
            paging.reset()
            setSearch(input.trim())
          }}
          className="flex items-end gap-2"
        >
          <Field>
            <FieldLabel htmlFor={`${id}-search`}>搜索{label}</FieldLabel>
            <Input
              id={`${id}-search`}
              maxLength={255}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="输入名称、账号或完整 ID"
            />
          </Field>
          <Button type="submit">搜索</Button>
        </form>
        {!enabled && (
          <p className="text-sm text-muted-foreground">
            请输入关键词后搜索已有用户。
          </p>
        )}
        {enabled && query.isPending && <Skeleton className="h-24 w-full" />}
        {enabled && query.error && (
          <RequestError
            error={query.error}
            retry={() => {
              void query.refetch()
            }}
          />
        )}
        {enabled && query.isFetching && !query.isPending && (
          <p role="status" className="text-sm text-muted-foreground">
            正在更新搜索结果…
          </p>
        )}
        {enabled && (
          <div
            role="listbox"
            aria-label={`${label}搜索结果`}
            className="flex flex-col gap-1"
            onKeyDown={(event) => {
              if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key))
                return
              const options = Array.from(
                event.currentTarget.querySelectorAll<HTMLButtonElement>(
                  '[role="option"]:not(:disabled)',
                ),
              )
              const current = options.indexOf(
                document.activeElement as HTMLButtonElement,
              )
              const next =
                event.key === "Home"
                  ? 0
                  : event.key === "End"
                    ? options.length - 1
                    : (current +
                        (event.key === "ArrowDown" ? 1 : -1) +
                        options.length) %
                      options.length
              event.preventDefault()
              options[next]?.focus()
            }}
          >
            {query.data?.items.map((item) => (
              <Button
                key={item.id}
                type="button"
                variant="ghost"
                role="option"
                aria-selected={false}
                disabled={query.isFetching}
                className="h-auto justify-start whitespace-normal px-3 py-3 text-left"
                onClick={() => {
                  setOpen(false)
                  onSelect(item)
                }}
              >
                <span className="flex min-w-0 flex-col gap-1 break-all">
                  {renderItem(item)}
                  <span className="font-mono text-xs text-muted-foreground">
                    {item.id}
                  </span>
                </span>
              </Button>
            ))}
          </div>
        )}
        {enabled &&
          !query.isPending &&
          !query.error &&
          query.data?.items.length === 0 && (
            <p role="status" className="text-sm text-muted-foreground">
              没有符合条件的记录，请调整关键词。
            </p>
          )}
        {enabled && (
          <Pager
            paging={paging}
            nextCursor={query.data?.next_cursor}
            busy={query.isFetching}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}
