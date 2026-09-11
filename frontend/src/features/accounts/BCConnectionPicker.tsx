import { useInfiniteQuery } from "@tanstack/react-query"
import { AccountsService } from "@/client"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { isForbidden, RequestError } from "@/features/tenants/shared"

export function useBCConnections(
  tenantId: string | null | undefined,
  bcId: string | null | undefined,
  activeOnly = false,
) {
  return useInfiniteQuery({
    queryKey: [
      "tenant",
      tenantId,
      "connections",
      "bc-picker",
      bcId,
      activeOnly,
    ],
    initialPageParam: undefined as string | undefined,
    enabled: !!tenantId && !!bcId,
    queryFn: async ({ signal, pageParam }) =>
      (
        await AccountsService.getConnections({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bcId!,
            limit: 50,
            cursor: pageParam,
            ...(activeOnly ? { status: "ACTIVE" } : {}),
          },
          signal,
        })
      ).data,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}

export function BCConnectionPicker({
  query,
  value,
  onChange,
  label,
  id,
  disabled = false,
  allowDefault = false,
}: {
  query: ReturnType<typeof useBCConnections>
  value?: string | null
  onChange: (id: string | null) => void
  label: string
  id: string
  disabled?: boolean
  allowDefault?: boolean
}) {
  const items = query.data?.pages.flatMap((page) => page.items) ?? []
  if (isForbidden(query.error))
    return (
      <RequestError
        error={query.error}
        retry={() => {
          void query.refetch()
        }}
      />
    )
  return (
    <Field className="w-full sm:max-w-xl">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Select
        value={value || (allowDefault ? "bc-default" : "")}
        onValueChange={(choice) =>
          onChange(choice === "bc-default" ? null : choice)
        }
        disabled={disabled || query.isPending || !!query.error}
      >
        <SelectTrigger
          id={id}
          className="w-full h-auto min-h-9 whitespace-normal [&>span]:break-all"
        >
          <SelectValue placeholder="请选择连接" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {allowDefault && (
              <SelectItem value="bc-default">使用 BC 默认连接</SelectItem>
            )}
            {value && !items.some((item) => item.id === value) && (
              <SelectItem value={value}>
                {allowDefault ? "已选连接" : "默认连接"} · {value}
              </SelectItem>
            )}
            {items.map((item) => (
              <SelectItem key={item.id} value={item.id}>
                {item.display_name ||
                  (item.kind === "OFFICIAL_MCP" ? "官方 MCP" : "官方 API")}{" "}
                · {item.id}
                {item.is_default ? " · 默认" : ""}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      {query.hasNextPage && (
        <Button
          variant="link"
          className="self-start"
          disabled={query.isFetchingNextPage || disabled}
          onClick={() => {
            void query.fetchNextPage()
          }}
        >
          加载更多连接
        </Button>
      )}
      {query.error && (
        <RequestError
          error={query.error}
          retry={() => {
            void query.refetch()
          }}
        />
      )}
    </Field>
  )
}
