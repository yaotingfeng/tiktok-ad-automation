import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { type ProviderApplicationPublic, ProvidersService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Pager, RequestError, useCursorPage } from "@/features/tenants/shared"
export function ApplicationPicker({
  tenantId,
  connectionId,
  value,
  disabled,
  onSelect,
}: {
  tenantId: string
  connectionId: string
  value: string
  disabled: boolean
  onSelect: (item: ProviderApplicationPublic) => void
}) {
  const [open, setOpen] = useState(false),
    paging = useCursorPage(),
    query = useQuery({
      queryKey: [
        "tenant",
        tenantId,
        "builds",
        "application-picker",
        connectionId,
        paging.cursor,
        paging.limit,
      ],
      enabled: open && !!connectionId,
      queryFn: async ({ signal }) =>
        (
          await ProvidersService.listApplications({
            path: { tenant_id: tenantId, connection_id: connectionId },
            query: { cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
    })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-label="推广应用"
          aria-expanded={open}
          disabled={disabled}
          className="h-auto min-h-9 justify-start whitespace-normal break-all"
        >
          {value || "选择推广应用"}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>选择推广应用</DialogTitle>
          <DialogDescription>
            应用目录来自所选版权方连接。不可用应用不能选为推广目标。
          </DialogDescription>
        </DialogHeader>
        {query.error && <RequestError error={query.error} />}{" "}
        {query.isPending && <p role="status">正在读取应用…</p>}
        <div
          role="listbox"
          aria-label="推广应用列表"
          className="max-h-80 space-y-2 overflow-y-auto"
        >
          {query.data?.items.map((item) => (
            <Button
              key={item.external_id}
              role="option"
              aria-selected={false}
              disabled={!item.available || query.isFetching}
              variant="outline"
              className="h-auto w-full justify-start whitespace-normal text-left"
              onClick={() => {
                onSelect(item)
                setOpen(false)
              }}
            >
              <span>
                {item.name}
                {!item.available ? " · 不可用于推广" : ""}
                <span className="block break-all font-mono text-xs">
                  {item.external_id}
                </span>
              </span>
            </Button>
          ))}
        </div>
        {query.data?.items.length === 0 && (
          <p role="status">此连接暂无可用应用，请联系管理员核实。</p>
        )}
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          busy={query.isFetching}
        />
      </DialogContent>
    </Dialog>
  )
}
