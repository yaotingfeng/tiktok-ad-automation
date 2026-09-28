import { useQuery } from "@tanstack/react-query"
import { useEffect, useId, useRef, useState } from "react"
import {
  type AccountPublic,
  AccountsService,
  type ResolvedLine,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogTrigger } from "@/components/ui/dialog"
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
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
import { Pager, RequestError, useCursorPage } from "@/features/tenants/shared"
import { availabilityLabels, FilterSelect } from "./presentation"

type Props = {
  tenantId: string
  bcId: string
  value: string
  disabled: boolean
  onChange: (value: string) => void
}

export function AccountPicker(props: Props) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open && !props.disabled} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" disabled={props.disabled}>
          选择账户
        </Button>
      </DialogTrigger>
      {open && !props.disabled && (
        <AccountSelection
          key={`${props.tenantId}:${props.bcId}`}
          {...props}
          close={() => setOpen(false)}
        />
      )}
    </Dialog>
  )
}

function AccountSelection({
  tenantId,
  bcId,
  value,
  onChange,
  close,
}: Props & { close: () => void }) {
  const id = useId()
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [statusInput, setStatusInput] = useState("")
  const [status, setStatus] = useState("")
  const [availability, setAvailability] = useState("all")
  const [selected, setSelected] = useState(new Set<string>())
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkError, setBulkError] = useState<unknown>()
  const paging = useCursorPage()
  const controller = useRef(new AbortController())
  useEffect(() => {
    const current = new AbortController()
    controller.current = current
    return () => current.abort()
  }, [])
  // 复用后端名称解析，不凭名称猜 ID；分批遵守解析接口的 500 行上限。
  const existing = useQuery({
    queryKey: ["tenant", tenantId, "account-picker-existing", bcId, value],
    queryFn: async ({ signal }) => {
      const lines = value
        .split("\n")
        .map((raw, line_no) => ({ raw, line_no: line_no + 1 }))
        .filter((line) => line.raw.trim())
      const rows: ResolvedLine[] = []
      for (let start = 0; start < lines.length; start += 500) {
        const result = await AccountsService.postResolve({
          path: { tenant_id: tenantId },
          body: { bc_id: bcId, lines: lines.slice(start, start + 500) },
          signal,
        })
        rows.push(...result.data)
      }
      return rows
    },
  })
  const existingIds = new Set(
    existing.data?.flatMap((row) =>
      row.advertiser_id ? [row.advertiser_id] : [],
    ) ?? [],
  )
  for (const raw of value.split("\n")) existingIds.add(raw.trim())
  const load = async (
    cursor: string | null,
    limit: number,
    signal: AbortSignal,
  ) =>
    (
      await AccountsService.getAccounts({
        path: { tenant_id: tenantId },
        query: {
          bc_id: bcId,
          query: search,
          remote_status: status || undefined,
          availability:
            availability === "all"
              ? undefined
              : (availability as AccountPublic["availability"]),
          cursor,
          limit,
        },
        signal,
      })
    ).data
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "account-picker",
      bcId,
      search,
      status,
      availability,
      paging.cursor,
      paging.limit,
    ],
    queryFn: ({ signal }) => load(paging.cursor, paging.limit, signal),
  })
  const locked =
    bulkBusy ||
    query.isFetching ||
    existing.isFetching ||
    !!query.error ||
    !!existing.error
  const eligible = (item: AccountPublic) =>
    item.can_build && !existingIds.has(item.advertiser_id)
  const available = (query.data?.items ?? []).filter(eligible)
  const pageCount = available.filter((item) =>
    selected.has(item.advertiser_id),
  ).length
  const chosen = [...selected].filter(
    (accountId) => !existingIds.has(accountId),
  )
  function toggle(items: AccountPublic[], checked: boolean) {
    setSelected((old) => {
      const next = new Set(old)
      for (const item of items.filter(eligible)) {
        if (checked) next.add(item.advertiser_id)
        else next.delete(item.advertiser_id)
      }
      return next
    })
  }
  async function selectAll() {
    setBulkBusy(true)
    setBulkError(undefined)
    const signal = controller.current.signal
    try {
      // 取齐筛选结果后再一次性提交勾选；中途失败或取消不留下半批结果。
      const ids = new Set<string>()
      let cursor: string | null = null
      do {
        const page = await load(cursor, 100, signal)
        for (const item of page.items.filter(eligible))
          ids.add(item.advertiser_id)
        cursor = page.next_cursor ?? null
      } while (cursor && !signal.aborted)
      if (!signal.aborted) setSelected((old) => new Set([...old, ...ids]))
    } catch (error) {
      if (!signal.aborted) setBulkError(error)
    } finally {
      if (!signal.aborted) setBulkBusy(false)
    }
  }
  function confirm() {
    // 保留原文及未解析行，仅按已解析的实际账户 ID 去重，追加新选 ID。
    const resolved = new Map(
      existing.data?.map((row) => [row.line_no, row.advertiser_id]),
    )
    const seen = new Set<string>()
    const lines = value.split("\n").filter((raw, index) => {
      if (!raw.trim()) return false
      const key = resolved.get(index + 1) || raw.trim()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    for (const accountId of chosen)
      if (!seen.has(accountId)) {
        lines.push(accountId)
        seen.add(accountId)
      }
    onChange(lines.join("\n"))
    close()
  }
  return (
    <PickerDialogContent
      title="选择账户"
      description="搜索当前 BC 的账户，勾选后将账户 ID 添加到输入框。"
      contained
    >
      <form
        className="shrink-0"
        onSubmit={(event) => {
          event.preventDefault()
          event.stopPropagation()
          if (bulkBusy) return
          setSearch(input.trim())
          setStatus(statusInput.trim())
          paging.reset()
          setBulkError(undefined)
        }}
      >
        <FieldGroup className="gap-3">
          <Field>
            <FieldLabel htmlFor={`${id}-search`}>搜索账户</FieldLabel>
            <div className="flex gap-2">
              <Input
                id={`${id}-search`}
                placeholder="输入名称关键词（如 P1）或完整 ID"
                maxLength={255}
                value={input}
                disabled={bulkBusy}
                onChange={(event) => setInput(event.target.value)}
              />
              <Button type="submit" disabled={bulkBusy}>
                搜索
              </Button>
            </div>
          </Field>
          <div className="flex items-end gap-2">
            <Field>
              <FieldLabel htmlFor={`${id}-status`}>平台状态</FieldLabel>
              <Input
                id={`${id}-status`}
                placeholder="输入平台原始状态"
                maxLength={64}
                value={statusInput}
                disabled={bulkBusy}
                onChange={(event) => setStatusInput(event.target.value)}
              />
            </Field>
            <fieldset disabled={bulkBusy}>
              <FilterSelect
                label="可用性"
                value={availability}
                choices={availabilityLabels}
                onChange={(next) => {
                  if (!bulkBusy) {
                    setAvailability(next)
                    paging.reset()
                    setBulkError(undefined)
                  }
                }}
              />
            </fieldset>
          </div>
        </FieldGroup>
      </form>
      <div className="flex shrink-0 flex-wrap items-center gap-3">
        <Field orientation="horizontal" className="w-auto">
          <Checkbox
            id={`${id}-all`}
            disabled={locked || !available.length}
            checked={
              pageCount > 0 && pageCount === available.length
                ? true
                : pageCount > 0
                  ? "indeterminate"
                  : false
            }
            onCheckedChange={(checked) => toggle(available, checked === true)}
          />
          <FieldLabel htmlFor={`${id}-all`}>全选本页</FieldLabel>
        </Field>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={locked || !query.data?.total}
          onClick={() => void selectAll()}
        >
          {bulkBusy ? "正在选择…" : "全选筛选结果"}
        </Button>
      </div>
      <div className="min-h-0 overflow-y-auto" aria-busy={locked}>
        {(query.isFetching || existing.isFetching) && (
          <p role="status" className="text-sm text-muted-foreground">
            正在读取账户…
          </p>
        )}
        {query.error && (
          <RequestError
            error={query.error}
            retry={() => void query.refetch()}
          />
        )}
        {existing.error && (
          <RequestError
            error={existing.error}
            retry={() => void existing.refetch()}
          />
        )}
        {!!bulkError && (
          <RequestError error={bulkError} retry={() => void selectAll()} />
        )}
        {!query.error && (
          <FieldSet className="gap-1">
            <FieldLegend className="sr-only">账户搜索结果</FieldLegend>
            {query.data?.items.map((item) => (
              <Field
                key={item.advertiser_id}
                orientation="horizontal"
                className="px-3 py-3"
                data-disabled={!eligible(item)}
              >
                <Checkbox
                  id={`${id}-${item.advertiser_id}`}
                  disabled={locked || !eligible(item)}
                  checked={
                    existingIds.has(item.advertiser_id) ||
                    selected.has(item.advertiser_id)
                  }
                  onCheckedChange={(checked) =>
                    toggle([item], checked === true)
                  }
                />
                <FieldLabel
                  htmlFor={`${id}-${item.advertiser_id}`}
                  className="min-w-0 flex-col items-start gap-1 break-all"
                >
                  <span>{item.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {item.advertiser_id}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {existingIds.has(item.advertiser_id) ? "已添加 · " : ""}
                    {availabilityLabels[item.availability]} ·{" "}
                    {item.remote_status}
                    {!item.can_build
                      ? " · 当前不可搭建（请核对账户状态与搭建权限）"
                      : ""}
                  </span>
                </FieldLabel>
              </Field>
            ))}
          </FieldSet>
        )}
        {!query.isPending && !query.error && query.data?.items.length === 0 && (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>没有符合条件的账户</EmptyTitle>
            </EmptyHeader>
          </Empty>
        )}
      </div>
      <div className="shrink-0">
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          total={query.data?.total}
          busy={locked}
        />
      </div>
      <Separator />
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <span role="status" className="text-sm">
          已选 {chosen.length} 个账户
        </span>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="ghost"
            disabled={bulkBusy || !selected.size}
            onClick={() => setSelected(new Set())}
          >
            清空勾选
          </Button>
          <Button type="button" variant="outline" onClick={close}>
            取消
          </Button>
          <Button
            type="button"
            disabled={locked || !chosen.length}
            onClick={confirm}
          >
            确定添加
          </Button>
        </div>
      </div>
    </PickerDialogContent>
  )
}
