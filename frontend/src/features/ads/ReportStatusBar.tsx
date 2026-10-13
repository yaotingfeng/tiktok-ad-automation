import { CircleAlert, CircleCheck, LoaderCircle } from "lucide-react"
import { useState } from "react"
import type { SyncAccountStatus, SyncStatusPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"

type Tone = "ok" | "pending" | "warn" | "error"

const TONE_CLASS: Record<Tone, string> = {
  ok: "text-muted-foreground",
  pending: "text-muted-foreground",
  warn: "text-amber-600",
  error: "text-destructive",
}

function accountTime(account: SyncAccountStatus) {
  return new Date(account.last_success_at!).toLocaleTimeString("zh-CN", {
    timeZone: account.timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })
}

/** 汇总全 BC 的“数据更新到”时间：账户时区一致时按账户本地时间，否则按浏览器时间。 */
function latestUpdate(accounts: SyncAccountStatus[]): string | null {
  const synced = accounts.filter((account) => account.last_success_at)
  if (!synced.length) return null
  const latest = synced.reduce((a, b) =>
    a.last_success_at! > b.last_success_at! ? a : b,
  )
  const timezones = new Set(synced.map((account) => account.timezone))
  return timezones.size === 1
    ? `数据更新到 ${accountTime(latest)}（账户本地）`
    : `数据更新到 ${new Date(latest.last_success_at!).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })}`
}

/**
 * 报表页唯一的状态区：把同步计划、各账户同步进度和查询覆盖收敛成一行结论。
 * 账户级明细是排查用信息，默认收起到抽屉里，不再逐个账户铺在页面上。
 */
export function ReportStatusBar({
  status,
  coverage,
  notice,
  onLoadLatest,
}: {
  status?: SyncStatusPublic
  coverage?: Record<string, unknown>
  notice: string | null
  /** 后台定时同步发布了新数据时提供；为避免打断勾选与预览，由用户点击后再加载。 */
  onLoadLatest?: () => void
}) {
  const [open, setOpen] = useState(false)
  const accounts = status?.accounts ?? []
  const pending = accounts.filter((account) => !account.last_success_at)
  const incomplete = accounts.filter(
    (account) =>
      (account.incomplete_dates ?? []).length > 0 ||
      (account.failed_dates ?? []).length > 0,
  )
  const coverageStatus = String(
    coverage?.status ?? coverage?.state ?? "",
  ).toUpperCase()

  let tone: Tone = "ok"
  let headline: string
  const reason = status?.plan.disabled_reason
  if (reason === "sync_disabled") {
    tone = "error"
    headline = "报表同步未开启"
  } else if (reason === "api_binding_required") {
    tone = "error"
    headline = "该 BC 未接入 API，无法同步报表"
  } else if (status && accounts.length && pending.length === accounts.length) {
    tone = "pending"
    headline = "首次同步进行中，完成后自动显示数据"
  } else {
    headline = (status && latestUpdate(accounts)) ?? "正在读取同步状态"
  }

  const extras: { text: string; tone: Tone }[] = []
  if (pending.length && pending.length < accounts.length)
    extras.push({ text: `${pending.length} 个账户同步中`, tone: "pending" })
  if (incomplete.length)
    extras.push({ text: `${incomplete.length} 个账户有日期缺失`, tone: "warn" })
  if (status?.plan.enabled && status.plan.slowed)
    extras.push({ text: "数据更新放缓", tone: "warn" })
  if (coverageStatus === "FAILED")
    extras.push({ text: "部分数据读取失败，请刷新重试", tone: "error" })
  else if (
    !incomplete.length &&
    !pending.length &&
    ["INCOMPLETE", "PENDING", "QUEUED", "RUNNING", "WAITING_REMOTE"].includes(
      coverageStatus,
    )
  )
    extras.push({ text: "当前范围部分数据仍在同步", tone: "pending" })
  // 刷新按钮的回执与主结论相同时不重复显示。
  if (notice && notice !== headline) extras.push({ text: notice, tone: "ok" })

  if (tone === "ok" && extras.some((item) => item.tone === "warn"))
    tone = "warn"
  const Icon =
    tone === "pending"
      ? LoaderCircle
      : tone === "ok"
        ? CircleCheck
        : CircleAlert

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
    >
      <span className={`inline-flex items-center gap-1.5 ${TONE_CLASS[tone]}`}>
        <Icon
          className={`size-4 ${tone === "pending" ? "animate-spin" : ""}`}
          aria-hidden="true"
        />
        {headline}
      </span>
      {extras.map((item) => (
        <span key={item.text} className={`text-xs ${TONE_CLASS[item.tone]}`}>
          {item.text}
        </span>
      ))}
      {onLoadLatest && (
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={onLoadLatest}
        >
          有新数据，点击加载
        </Button>
      )}
      {accounts.length > 0 && (
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={() => setOpen(true)}
        >
          同步明细
        </Button>
      )}
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-md">
          <SheetHeader>
            <SheetTitle>账户同步明细</SheetTitle>
            <SheetDescription>
              共 {accounts.length} 个账户，时间为账户本地时间。
            </SheetDescription>
          </SheetHeader>
          <ul className="divide-y px-4 text-xs">
            {accounts.map((account) => {
              const missing = [
                ...(account.incomplete_dates ?? []),
                ...(account.failed_dates ?? []),
              ]
              return (
                <li key={account.advertiser_id} className="space-y-0.5 py-2">
                  <div className="flex justify-between gap-2">
                    <span className="font-mono">{account.advertiser_id}</span>
                    <span className="text-muted-foreground">
                      {account.last_success_at
                        ? `数据更新到 ${accountTime(account)}（账户本地）`
                        : "同步中"}
                    </span>
                  </div>
                  {missing.length > 0 && (
                    <p className="text-amber-600">
                      以下日期数据不完整：{missing.join("、")}
                    </p>
                  )}
                </li>
              )
            })}
          </ul>
        </SheetContent>
      </Sheet>
    </div>
  )
}
