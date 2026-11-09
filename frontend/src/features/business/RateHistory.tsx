import { useInfiniteQuery } from "@tanstack/react-query"
import { useState } from "react"
import { BusinessService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { rateKey } from "./queries"
import { RatePeriodTable, rateText } from "./RatePeriodTable"

export function RateHistory({
  tenantId,
  kind,
  scopeId,
}: {
  tenantId: string
  kind: "provider" | "rebate"
  scopeId: string
}) {
  const [open, setOpen] = useState(false)
  const history = useInfiniteQuery({
    queryKey: [...rateKey(tenantId, kind, scopeId), "history"],
    enabled: open,
    initialPageParam: 0,
    queryFn: async ({ pageParam, signal }) =>
      (
        await BusinessService.rateHistory({
          path: { tenant_id: tenantId },
          query: { kind, scope_id: scopeId, offset: pageParam, limit: 20 },
          signal,
        })
      ).data,
    getNextPageParam: (last, pages) =>
      last.has_more
        ? pages.reduce((count, p) => count + p.items.length, 0)
        : undefined,
  })
  return (
    <div className="flex flex-col gap-3">
      <Button
        variant="ghost"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        {open ? "收起修改记录" : "查看修改记录"}
      </Button>
      {open && (
        <>
          {history.isPending && <p>正在读取修改记录…</p>}
          {history.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                修改记录读取失败。
                <Button variant="link" onClick={() => void history.refetch()}>
                  重试
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {history.data?.pages
            .flatMap((p) => p.items)
            .map((item) => (
              <details key={item.id}>
                <summary>
                  修订 {item.revision} ·{" "}
                  {new Date(item.created_at).toLocaleString()} ·{" "}
                  {item.legacy ? "旧版记录" : "比例调整"}
                </summary>
                <div className="flex flex-col gap-2 py-2">
                  <p>操作人：{item.actor_name || item.actor_id}</p>
                  <p>{item.note || "未填写调整说明"}</p>
                  {item.legacy ? (
                    <p>
                      生效日期：{item.periods[0]?.start_date}；比例：
                      {rateText(item.periods[0]?.rate)}
                      。当前适用范围以时间段列表为准。
                    </p>
                  ) : (
                    <>
                      <p>调整前</p>
                      <RatePeriodTable periods={item.previous_periods} />
                      <p>调整后</p>
                      <RatePeriodTable periods={item.periods} />
                    </>
                  )}
                </div>
              </details>
            ))}
          {history.data?.pages[0]?.items.length === 0 && <p>尚无修改记录。</p>}
          {history.hasNextPage && (
            <Button
              variant="outline"
              disabled={history.isFetchingNextPage}
              onClick={() => void history.fetchNextPage()}
            >
              加载更多记录
            </Button>
          )}
        </>
      )}
    </div>
  )
}
