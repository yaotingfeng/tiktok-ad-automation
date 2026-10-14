import { useQuery } from "@tanstack/react-query"
import { X } from "lucide-react"
import { useEffect, useState } from "react"
import { AutomationRulesService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ruleKeys } from "./queries"
import type { Level } from "./ruleForm"

type Option = { value: string; label: string; detail: string }

function useDebounced(value: string, delay = 250) {
  const [current, setCurrent] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setCurrent(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return current
}

/** 选中值以文本保存（每行一个），与批量粘贴共用同一份数据。 */
function splitValues(text: string) {
  return text
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function SearchPicker({
  label,
  pasteLabel,
  textLabel,
  placeholder,
  text,
  onTextChange,
  options,
  query,
  onQueryChange,
  loading,
}: {
  label: string
  pasteLabel: string
  textLabel: string
  placeholder: string
  text: string
  onTextChange: (text: string) => void
  options: Option[]
  query: string
  onQueryChange: (query: string) => void
  loading: boolean
}) {
  const [pasting, setPasting] = useState(false)
  const values = splitValues(text)
  const add = (value: string) => {
    if (!values.includes(value)) onTextChange([...values, value].join("\n"))
    onQueryChange("")
  }
  const remove = (value: string) =>
    onTextChange(values.filter((item) => item !== value).join("\n"))
  return (
    <div className="flex flex-col gap-2">
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {values.map((value) => (
            <Badge key={value} variant="secondary" className="gap-1">
              {value}
              <button
                type="button"
                aria-label={`移除 ${value}`}
                onClick={() => remove(value)}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      <div className="relative">
        <Input
          type="search"
          aria-label={label}
          placeholder={placeholder}
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
        />
        {query.trim() && (
          <div
            role="listbox"
            aria-label={`${label}结果`}
            className="absolute z-10 mt-1 max-h-64 w-full overflow-y-auto rounded-md border bg-popover p-1 shadow-md"
          >
            {loading && (
              <p className="px-2 py-1 text-sm text-muted-foreground">搜索中…</p>
            )}
            {!loading && options.length === 0 && (
              <p className="px-2 py-1 text-sm text-muted-foreground">
                没有匹配的结果
              </p>
            )}
            {options.map((option) => (
              <div
                key={option.value}
                role="option"
                aria-selected={values.includes(option.value)}
                tabIndex={0}
                className="flex cursor-pointer flex-col rounded-sm px-2 py-1 text-sm hover:bg-muted focus:bg-muted focus:outline-none"
                onClick={() => add(option.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault()
                    add(option.value)
                  }
                }}
              >
                <span>{option.label}</span>
                <span className="text-xs text-muted-foreground">
                  {option.detail}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setPasting((value) => !value)}
        >
          {pasteLabel}
        </Button>
      </div>
      {pasting && (
        <Textarea
          aria-label={textLabel}
          placeholder="每行或用逗号分隔一个"
          value={text}
          onChange={(e) => onTextChange(e.target.value)}
        />
      )}
    </div>
  )
}

export function AccountPicker({
  tenantId,
  text,
  onTextChange,
}: {
  tenantId: string
  text: string
  onTextChange: (text: string) => void
}) {
  const [query, setQuery] = useState("")
  const q = useDebounced(query.trim())
  const results = useQuery({
    queryKey: [...ruleKeys(tenantId), "picker", "accounts", q],
    enabled: !!q,
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.pickAccounts({
          path: { tenant_id: tenantId },
          query: { q, limit: 20 },
          signal,
        })
      ).data!,
  })
  return (
    <SearchPicker
      label="搜索账户"
      pasteLabel="批量粘贴账户 ID"
      textLabel="账户 ID"
      placeholder="按账户名称或 ID 搜索"
      text={text}
      onTextChange={onTextChange}
      query={query}
      onQueryChange={setQuery}
      loading={results.isFetching}
      options={(results.data ?? []).map((item) => ({
        value: item.advertiser_id,
        label: item.name || item.advertiser_id,
        detail: `${item.advertiser_id} · ${item.currency} · ${item.timezone}`,
      }))}
    />
  )
}

export function ObjectPicker({
  tenantId,
  level,
  advertiserIds,
  text,
  onTextChange,
}: {
  tenantId: string
  level: Level
  advertiserIds: string[]
  text: string
  onTextChange: (text: string) => void
}) {
  const [query, setQuery] = useState("")
  const q = useDebounced(query.trim())
  const results = useQuery({
    queryKey: [
      ...ruleKeys(tenantId),
      "picker",
      "objects",
      level,
      q,
      advertiserIds,
    ],
    enabled: !!q,
    queryFn: async ({ signal }) =>
      (
        await AutomationRulesService.pickObjects({
          path: { tenant_id: tenantId },
          query: { level, q, advertiser_ids: advertiserIds, limit: 20 },
          signal,
        })
      ).data!,
  })
  return (
    <SearchPicker
      label="搜索对象"
      pasteLabel="批量粘贴对象"
      textLabel="指定对象"
      placeholder="按名称或 ID 搜索当前层级的对象"
      text={text}
      onTextChange={onTextChange}
      query={query}
      onQueryChange={setQuery}
      loading={results.isFetching}
      options={(results.data ?? []).map((item) => ({
        value: item.value,
        label: item.label,
        detail: `账户 ${item.advertiser_id} · ${item.value}`,
      }))}
    />
  )
}
