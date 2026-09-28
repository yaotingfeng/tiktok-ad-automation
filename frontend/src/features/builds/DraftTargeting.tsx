import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { BuildsService, type DraftSummary } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  countryName,
  normalizedTargeting,
  TargetingForm,
  TargetingSummary,
  targetingError,
} from "@/features/targeting/TargetingForm"
import { useTargetingDirectory } from "@/features/targeting/useTargetingDirectory"
import { buildKey, mutationKey, readPendingMutation } from "./api"
import { BuildError, reportError, unknownOutcome } from "./presentation"

export function DraftTargeting({
  tenantId,
  bcId,
  summary,
  write,
  onRefresh,
  onPrepare,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  write: boolean
  onRefresh: () => void
  onPrepare: () => Promise<void>
}) {
  const directory = useTargetingDirectory(tenantId, bcId, summary)
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [differences, setDifferences] = useState(false)
  const [cursor, setCursor] = useState("")
  const [value, setValue] = useState(() =>
    normalizedTargeting(summary.targeting),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const key = mutationKey(tenantId, bcId, summary.draft_id)
  const disabled =
    !write || busy || summary.status !== "READY" || !!readPendingMutation(key)
  const available = directory.data?.region_codes || []
  const invalid =
    value.region_mode === "SELECTED" &&
    value.region_codes.some((code) => !available.includes(code))
  const accounts = useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary.draft_id,
      "targeting-accounts",
      summary.revision,
      cursor,
    ],
    enabled: differences,
    queryFn: async ({ signal }) =>
      (
        await BuildsService.targetingAccounts({
          path: { tenant_id: tenantId, draft_id: summary.draft_id },
          query: { cursor, limit: 50 },
          signal,
        })
      ).data,
  })
  async function save(reset = false) {
    if (
      disabled ||
      (!reset &&
        (targetingError(value) || invalid || directory.data?.state !== "READY"))
    )
      return
    const requestId = crypto.randomUUID()
    setBusy(true)
    setError(undefined)
    sessionStorage.setItem(
      key,
      JSON.stringify({ requestId, kind: "targeting", prepare: false }),
    )
    try {
      await BuildsService.updateTargeting({
        path: { tenant_id: tenantId, draft_id: summary.draft_id },
        body: {
          request_id: requestId,
          expected_revision: summary.revision,
          targeting_override: reset ? null : value,
        },
      })
      sessionStorage.removeItem(key)
      setOpen(false)
      await client.invalidateQueries({
        queryKey: [...buildKey(tenantId, bcId), summary.draft_id],
      })
      onRefresh()
    } catch (e) {
      if (!unknownOutcome(e)) sessionStorage.removeItem(key)
      setError(e)
      reportError(e)
      onRefresh()
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          受众定向{" "}
          <Badge variant="secondary">
            {summary.targeting_override ? "本次已修改" : "策略默认"}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <TargetingSummary
          value={summary.targeting}
          countries={directory.data?.state === "READY" ? available : undefined}
        />
        <p className="text-sm text-muted-foreground">
          {directory.data?.state === "READY"
            ? `已核实 ${directory.data.verified_account_count} 个账户，共同可投 ${available.length} 个国家/地区。`
            : directory.data?.state === "UNAVAILABLE"
              ? "没有共同可投国家，请调整账户或小程序。"
              : "共同可投国家待核实，请先完成账户和小程序准备。"}
        </p>
        {!!directory.data?.unavailable_region_codes?.length && (
          <Alert variant="destructive">
            <AlertDescription>
              已选国家不可用或待核实：
              {directory.data.unavailable_region_codes
                .map(countryName)
                .join("、")}
              。请调整本次定向。
            </AlertDescription>
          </Alert>
        )}
        {!!directory.error && <BuildError error={directory.error} />}
        {!!error && <BuildError error={error} />}
        <div className="flex flex-wrap gap-2">
          {write && (
            <Button
              variant="outline"
              disabled={disabled}
              onClick={() => {
                setValue(normalizedTargeting(summary.targeting))
                setError(undefined)
                setOpen(true)
                void directory.refetch()
              }}
            >
              修改本次定向
            </Button>
          )}
          {write && summary.targeting_override && (
            <Button
              variant="ghost"
              disabled={disabled}
              onClick={() => void save(true)}
            >
              恢复策略默认
            </Button>
          )}
          <Button
            variant="ghost"
            disabled={!summary.account_count || !summary.drama_count}
            onClick={() => {
              setCursor("")
              setDifferences(true)
            }}
          >
            查看账户地区差异
          </Button>
          {write && directory.data?.state !== "READY" && (
            <Button
              variant="outline"
              disabled={disabled}
              onClick={() => void onPrepare()}
            >
              重新核实可投地区
            </Button>
          )}
        </div>
        <Dialog open={open} onOpenChange={(v) => !busy && setOpen(v)}>
          <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>修改本次受众定向</DialogTitle>
              <DialogDescription>
                只影响当前批次，保存后重新核验并使旧预览失效。
              </DialogDescription>
            </DialogHeader>
            <TargetingForm
              value={value}
              onChange={setValue}
              countries={available}
              disabled={disabled}
            />
            {invalid && (
              <p role="alert" className="text-sm text-destructive">
                已选国家不在当前共同可投范围，请移除或重新核实。
              </p>
            )}
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setOpen(false)}
              >
                取消
              </Button>
              <Button
                disabled={
                  disabled ||
                  directory.isFetching ||
                  directory.data?.state !== "READY" ||
                  invalid ||
                  !!targetingError(value)
                }
                onClick={() => void save()}
              >
                保存本次定向
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <Dialog open={differences} onOpenChange={setDifferences}>
          <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>账户地区差异</DialogTitle>
              <DialogDescription>
                各账户与当前小程序匹配的地区；空列表表示不可用或尚未核实。
              </DialogDescription>
            </DialogHeader>
            {accounts.isPending && <p>正在读取…</p>}
            {!!accounts.error && <BuildError error={accounts.error} />}
            {accounts.data?.items.map((row) => (
              <div key={row.advertiser_id} className="flex flex-col gap-1">
                <p>{row.advertiser_id}</p>
                <p className="text-sm text-muted-foreground">
                  {row.region_codes.map(countryName).join("、") ||
                    "不可用/待核实"}
                </p>
              </div>
            ))}
            <DialogFooter>
              {cursor && (
                <Button variant="outline" onClick={() => setCursor("")}>
                  返回首页
                </Button>
              )}
              <Button
                variant="outline"
                disabled={!accounts.data?.next_cursor || accounts.isFetching}
                onClick={() => setCursor(accounts.data?.next_cursor || "")}
              >
                下一页
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
