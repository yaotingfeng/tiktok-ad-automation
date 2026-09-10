import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useState } from "react"
import { type MaterialPublic, MaterialsService } from "@/client"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { displayTime, FilterSelect } from "@/features/accounts/presentation"
import {
  isForbidden,
  Pager,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { bytes, CopyValue, materialKey, Stage, stages } from "./presentation"
export function MaterialTable({
  tenantId,
  bcId,
  onDetails,
  onForbidden,
  enabled,
}: {
  tenantId: string
  bcId: string
  onDetails: (id: string) => void
  onForbidden?: () => void
  enabled: boolean
}) {
  const paging = useCursorPage(),
    [input, setInput] = useState(""),
    [search, setSearch] = useState(""),
    [status, setStatus] = useState("all"),
    [from, setFrom] = useState(""),
    [to, setTo] = useState(""),
    [dates, setDates] = useState({ from: "", to: "" })
  const query = useQuery({
    enabled,
    queryKey: [
      ...materialKey(tenantId, bcId),
      "library",
      search,
      status,
      dates,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await MaterialsService.getMaterials({
          path: { tenant_id: tenantId },
          query: {
            bc_id: bcId,
            query: search,
            status: status === "all" ? undefined : status,
            created_from: dates.from
              ? new Date(`${dates.from}T00:00:00`).toISOString()
              : undefined,
            created_to: dates.to
              ? new Date(`${dates.to}T23:59:59.999`).toISOString()
              : undefined,
            cursor: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
  })
  const data = useRetainedData(query.data, query.error)
  useEffect(() => {
    if (isForbidden(query.error)) onForbidden?.()
  }, [query.error, onForbidden])
  const columns: ColumnDef<MaterialPublic>[] = [
    {
      header: "素材文件",
      cell: ({ row: { original: r } }) => (
        <div className="min-w-52 max-w-72">
          <Button
            className="h-auto max-w-full justify-start p-0"
            variant="link"
            onClick={() => onDetails(r.material_id)}
          >
            <span className="truncate" title={r.file_name}>
              {r.file_name}
            </span>
          </Button>
          <p className="text-xs text-muted-foreground">{r.mime_type}</p>
        </div>
      ),
    },
    {
      header: "文件信息",
      cell: ({ row: { original: r } }) => (
        <div>
          {bytes(r.byte_size)}
          <p className="text-xs text-muted-foreground">
            {r.duration != null && r.width != null && r.height != null
              ? `${r.duration} 秒 · ${r.width} × ${r.height}`
              : "待识别文件信息"}
          </p>
        </div>
      ),
    },
    {
      header: "平台入库状态",
      cell: ({ row: { original: r } }) => <Stage status={r.status} />,
    },
    {
      header: "上传账户（最近一次）",
      cell: ({ row: { original: r } }) =>
        r.latest_advertiser_id ? (
          <CopyValue value={r.latest_advertiser_id} />
        ) : (
          <span className="text-xs text-muted-foreground">
            尚未开始平台上传
          </span>
        ),
    },
    {
      header: "可用账户数",
      cell: ({ row: { original: r } }) => (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onDetails(r.material_id)}
        >
          {r.available_account_count} 个账户
        </Button>
      ),
    },
    {
      header: "文件登记时间",
      cell: ({ row: { original: r } }) => displayTime(r.created_at),
    },
    {
      header: "操作",
      cell: ({ row: { original: r } }) => (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onDetails(r.material_id)}
        >
          查看文件与账户记录
        </Button>
      ),
    },
  ]
  const reset = () => {
    setInput("")
    setSearch("")
    setStatus("all")
    setFrom("")
    setTo("")
    setDates({ from: "", to: "" })
    paging.reset()
  }
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (from && to && from > to) return
          setSearch(input)
          setDates({ from, to })
          paging.reset()
        }}
      >
        <Field className="w-full sm:w-80">
          <FieldLabel htmlFor="material-search">素材文件名</FieldLabel>
          <Input
            id="material-search"
            placeholder="搜索完整或部分文件名"
            maxLength={1000}
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
        </Field>
        <FilterSelect
          label="素材状态"
          value={status}
          choices={Object.fromEntries(
            Object.entries(stages).filter(([key]) =>
              [
                "receiving",
                "stored",
                "uploading",
                "verifying",
                "available",
                "blocked",
                "result_unknown",
              ].includes(key),
            ),
          )}
          onChange={(v) => {
            setStatus(v)
            paging.reset()
          }}
        />
        <Field className="w-40">
          <FieldLabel htmlFor="material-from">开始日期</FieldLabel>
          <Input
            id="material-from"
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </Field>
        <Field className="w-40">
          <FieldLabel htmlFor="material-to">结束日期</FieldLabel>
          <Input
            id="material-to"
            type="date"
            min={from || undefined}
            value={to}
            onChange={(e) => setTo(e.target.value)}
          />
        </Field>
        <Button variant="outline" type="submit">
          搜索
        </Button>
        <Button variant="ghost" type="button" onClick={reset}>
          清空筛选
        </Button>
      </form>
      <ServerTable
        rows={data?.items || []}
        columns={columns}
        loading={query.isPending && !data}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={!!search || status !== "all" || !!dates.from || !!dates.to}
        emptyTitle="还没有上传素材"
      />
      <Pager
        paging={paging}
        nextCursor={data?.next_cursor}
        busy={query.isFetching}
      />
    </div>
  )
}
