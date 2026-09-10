import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table"
import { AxiosError } from "axios"
import { useEffect, useState } from "react"
import type { TenantSummary } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export const roleLabels: Record<TenantSummary["role"], string> = {
  platform_admin: "平台管理员",
  tenant_admin: "租户管理员",
  operator: "投手",
  viewer: "只读成员",
}
export const memberRoles = ["tenant_admin", "operator", "viewer"] as const
export const canManage = (role?: TenantSummary["role"]) =>
  role === "platform_admin" || role === "tenant_admin"
export const isForbidden = (error: unknown) =>
  error instanceof AxiosError && error.response?.status === 403
export function errorMessage(error: unknown) {
  if (error instanceof AxiosError) {
    const body = error.response?.data
    if (body?.code === "last_tenant_admin")
      return "请先设置其他租户管理员，再调整这位管理员。"
    if (isForbidden(error))
      return "当前角色无权执行此操作，请联系管理员检查租户权限。"
    if (typeof body?.message === "string") return body.message
    if (typeof body?.detail === "string") return body.detail
    if (Array.isArray(body?.detail))
      return "提交内容不符合要求，请检查各字段后重试。"
  }
  return "请求未完成，请稍后重试。"
}
export function RequestError({
  error,
  retry,
}: {
  error: unknown
  retry?: () => void
}) {
  return (
    <Alert variant="destructive">
      <AlertTitle>
        {isForbidden(error) ? "无权访问此页面" : "请求未完成"}
      </AlertTitle>
      <AlertDescription>
        <p>{errorMessage(error)}</p>
        {retry && !isForbidden(error) && (
          <Button variant="outline" size="sm" onClick={retry}>
            重试
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}
export function useCursorPage() {
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const [limit, setPageLimit] = useState(50)
  const reset = () => setCursors([null])
  return {
    cursor: cursors[cursors.length - 1] ?? null,
    page: cursors.length,
    limit,
    reset,
    setLimit: (value: number) => {
      setPageLimit(value)
      reset()
    },
    previous: () => setCursors((values) => values.slice(0, -1)),
    next: (cursor: string) => setCursors((values) => [...values, cursor]),
  }
}
export function Pager({
  paging,
  nextCursor,
  busy,
}: {
  paging: ReturnType<typeof useCursorPage>
  nextCursor?: string | null
  busy: boolean
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span>每页</span>
        <Select
          value={String(paging.limit)}
          onValueChange={(value) => paging.setLimit(Number(value))}
          disabled={busy}
        >
          <SelectTrigger aria-label="每页条数">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="50">50 条</SelectItem>
              <SelectItem value="100">100 条</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-sm text-muted-foreground">
          第 {paging.page} 页
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || paging.page === 1}
          onClick={paging.previous}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || !nextCursor}
          onClick={() => nextCursor && paging.next(nextCursor)}
        >
          下一页
        </Button>
      </div>
    </div>
  )
}
export function StatusSelect({
  value,
  onChange,
}: {
  value: string
  onChange: (value: string) => void
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label="状态筛选">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectItem value="all">全部状态</SelectItem>
          <SelectItem value="active">正常</SelectItem>
          <SelectItem value="inactive">停用</SelectItem>
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
export function ServerTable<T>({
  rows,
  columns,
  loading,
  fetching,
  error,
  retry,
  filtered,
  emptyTitle,
  fixedLayout,
}: {
  rows: T[]
  columns: ColumnDef<T>[]
  loading: boolean
  fetching: boolean
  error: unknown
  retry: () => void
  filtered: boolean
  emptyTitle: string
  fixedLayout?: { fillColumn: string }
}) {
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualPagination: true,
    manualFiltering: true,
  })
  return (
    <div
      aria-busy={fetching}
      className="min-w-0 overflow-hidden rounded-lg border [&>[data-slot=table-container]]:max-h-[60svh] [&>[data-slot=table-container]]:overflow-y-auto"
    >
      {!!error && (
        <div className="p-4">
          <RequestError error={error} retry={retry} />
        </div>
      )}
      {fetching && !loading && (
        <p role="status" className="px-4 py-2 text-sm text-muted-foreground">
          正在更新列表…
        </p>
      )}
      <Table
        style={
          fixedLayout
            ? { tableLayout: "fixed", minWidth: table.getTotalSize() }
            : undefined
        }
      >
        {fixedLayout && (
          <colgroup>
            {table.getVisibleLeafColumns().map((column) => (
              <col
                key={column.id}
                // TanStack merges a default size into every resolved column.
                // Leave the designated column unsized to absorb spare space.
                style={
                  column.id === fixedLayout.fillColumn
                    ? undefined
                    : { width: column.getSize() }
                }
              />
            ))}
          </colgroup>
        )}
        <TableHeader className="sticky top-0 z-10 bg-muted">
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id}>
              {group.headers.map((header) => (
                <TableHead key={header.id}>
                  {flexRender(
                    header.column.columnDef.header,
                    header.getContext(),
                  )}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {loading
            ? Array.from({ length: 6 }, (_, i) => (
                <TableRow key={i}>
                  {columns.map((_, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-5 w-full min-w-16" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            : table.getRowModel().rows.map((row) => (
                <TableRow key={row.id}>
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
        </TableBody>
      </Table>
      {!loading && !error && rows.length === 0 && (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>
              {filtered ? "没有符合条件的记录" : emptyTitle}
            </EmptyTitle>
            <EmptyDescription>
              {filtered
                ? "请调整搜索条件或清除筛选。"
                : "添加后会在此处显示实际记录。"}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </div>
  )
}

// Retain the last response during filtering/refetch failures within this mounted
// page. Tenant outlets are keyed by scope; a permission error never shows rows.
export function useRetainedData<T>(data: T | undefined, error: unknown) {
  const [previous, setPrevious] = useState<T | undefined>(undefined)
  useEffect(() => {
    if (data !== undefined) setPrevious(data)
  }, [data])
  return isForbidden(error) ? undefined : (data ?? previous)
}
