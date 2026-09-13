import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { BuildsService, type DraftSummary } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { buildKey, mutationKey, readPendingMutation } from "./api"
import { BuildError, reportError, unknownOutcome } from "./presentation"

export function MiniTargetPicker({
  tenantId,
  bcId,
  summary,
  write,
  onPrepare,
  onRefresh,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  write: boolean
  onPrepare: () => Promise<void>
  onRefresh: () => void
}) {
  const [open, setOpen] = useState(false)
  const [page, setPage] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const query = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      summary.revision,
      summary.status,
      "minis",
      page,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.minisOptions({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: { page },
          signal,
        })
      ).data,
    enabled: summary.account_count > 0 && summary.drama_count > 0,
    refetchInterval: summary.status === "PREPARING" ? 2000 : false,
  })
  const data = query.data
  if (!summary.account_count || !summary.drama_count) return null
  const pendingKey = mutationKey(tenantId, bcId, summary.draft_id)
  const selectable =
    write && summary.status === "READY" && !readPendingMutation(pendingKey)
  async function choose(minisId: string) {
    if (!selectable || busy || !data?.catalog_job_id) return
    const requestId = crypto.randomUUID()
    setBusy(true)
    setError(undefined)
    sessionStorage.setItem(
      pendingKey,
      JSON.stringify({ requestId, kind: "minis", prepare: true }),
    )
    try {
      await BuildsService.selectMini({
        path: { tenant_id: tenantId, draft_id: summary.draft_id },
        body: {
          request_id: requestId,
          expected_revision: summary.revision,
          catalog_job_id: data.catalog_job_id,
          minis_id: minisId,
        },
      })
      sessionStorage.removeItem(pendingKey)
      setOpen(false)
      onRefresh()
      await onPrepare()
    } catch (e) {
      if (!unknownOutcome(e)) sessionStorage.removeItem(pendingKey)
      setError(e)
      reportError(e)
      onRefresh()
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardContent className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-medium">推广小程序</p>
            <p className="text-sm text-muted-foreground">
              {data?.selected?.name ||
                (summary.status === "PREPARING" || query.isPending
                  ? "正在读取可用小程序…"
                  : data?.state === "conflict"
                    ? "剧目已有不同推广目标，请核对后选择"
                    : data?.state === "unavailable"
                      ? "参考账户暂无可用短剧小程序"
                      : "请按名称选择本批次剧目对应的小程序")}
            </p>
          </div>
          {write && (
            <Button
              variant="outline"
              disabled={!selectable || busy || !data?.catalog_job_id}
              onClick={() => {
                setPage(1)
                setOpen(true)
              }}
            >
              {data?.selected ? "更换小程序" : "选择小程序"}
            </Button>
          )}
        </div>
        {query.error ? <BuildError error={query.error} /> : null}
        {data?.state === "pending" && summary.status === "READY" && write && (
          <Button
            className="self-start"
            variant="outline"
            onClick={() => void onPrepare()}
          >
            更新可用小程序
          </Button>
        )}
        <p className="text-xs text-muted-foreground">
          选择会用于本批次全部剧目，系统会检查各目标账户是否可用，并记住已确认链接的对应关系。
        </p>
        <Dialog
          open={open}
          onOpenChange={(value) => {
            if (!busy) setOpen(value)
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>选择推广小程序</DialogTitle>
              <DialogDescription>
                选择这些剧目的实际播放小程序。无需查找或填写 ID。
              </DialogDescription>
            </DialogHeader>
            {error ? <BuildError error={error} /> : null}
            <div className="max-h-80 space-y-2 overflow-y-auto">
              {data?.items?.map((item) => (
                <Button
                  key={item.minis_id}
                  className="h-auto w-full justify-between gap-4 whitespace-normal p-3 text-left"
                  variant="outline"
                  disabled={busy || !selectable || query.isFetching}
                  onClick={() => void choose(item.minis_id)}
                >
                  <span>
                    <strong>{item.name}</strong>
                    <span className="block text-xs font-normal text-muted-foreground">
                      {item.minis_id}
                    </span>
                  </span>
                  <span>{busy ? "保存中…" : "选择并继续"}</span>
                </Button>
              ))}
              {!query.isFetching && !data?.items?.length && (
                <p className="text-sm text-muted-foreground">
                  本页没有可选的短剧小程序。
                </p>
              )}
            </div>
            <div className="flex items-center justify-end gap-2">
              <Button
                variant="outline"
                disabled={busy || page === 1 || query.isFetching}
                onClick={() => setPage(page - 1)}
              >
                上一页
              </Button>
              <span className="text-sm">第 {page} 页</span>
              <Button
                variant="outline"
                disabled={busy || !data?.next_page || query.isFetching}
                onClick={() => setPage(data!.next_page!)}
              >
                下一页
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
