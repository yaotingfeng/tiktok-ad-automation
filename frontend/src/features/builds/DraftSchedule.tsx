import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { BuildsService, type DraftSummary } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { buildKey, mutationKey, readPendingMutation } from "./api"
import { BuildError, reportError, unknownOutcome } from "./presentation"

type Mode = "IMMEDIATE" | "START_AT" | "START_END"

function localInputFromUtc(value?: string | null) {
  if (!value) return ""
  const date = new Date(`${value.replace(" ", "T")}Z`)
  if (Number.isNaN(date.getTime())) return ""
  const pad = (part: number) => String(part).padStart(2, "0")
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function utcFromLocalInput(value: string) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toISOString().slice(0, 19).replace("T", " ")
}

function initial(summary: DraftSummary) {
  return {
    mode: (summary.delivery_config?.schedule_mode || "IMMEDIATE") as Mode,
    start: localInputFromUtc(summary.delivery_config?.schedule_start_time),
    end: localInputFromUtc(summary.delivery_config?.schedule_end_time),
  }
}

// 投放排期属于本次搭建，在第二步选完投放身份后单独保存；
// 后端沿用已准备的账户、链接与素材，只重核场景并使旧预览失效。
export function DraftSchedule({
  tenantId,
  bcId,
  summary,
  write,
  onRefresh,
}: {
  tenantId: string
  bcId: string
  summary: DraftSummary
  write: boolean
  onRefresh: () => void
}) {
  const client = useQueryClient()
  const [value, setValue] = useState(() => initial(summary))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const [inputError, setInputError] = useState("")
  const key = mutationKey(tenantId, bcId, summary.draft_id)
  const disabled =
    !write || busy || summary.status !== "READY" || !!readPendingMutation(key)
  const saved = initial(summary)
  const dirty = JSON.stringify(value) !== JSON.stringify(saved)
  const change = (patch: Partial<typeof value>) => {
    setInputError("")
    setValue((v) => ({ ...v, ...patch }))
  }
  async function save() {
    if (disabled) return
    if (value.mode !== "IMMEDIATE" && !value.start)
      return setInputError("请选择投放开始时间。")
    if (value.mode === "START_END" && !value.end)
      return setInputError("请选择投放结束时间。")
    if (
      value.mode === "START_END" &&
      new Date(value.end) <= new Date(value.start)
    )
      return setInputError("结束时间必须晚于开始时间。")
    const requestId = crypto.randomUUID()
    setBusy(true)
    setError(undefined)
    sessionStorage.setItem(
      key,
      JSON.stringify({ requestId, kind: "delivery", prepare: false }),
    )
    try {
      await BuildsService.updateDelivery({
        path: { tenant_id: tenantId, draft_id: summary.draft_id },
        body: {
          request_id: requestId,
          expected_revision: summary.revision,
          delivery_config: {
            schedule_mode: value.mode,
            schedule_start_time:
              value.mode === "IMMEDIATE"
                ? null
                : utcFromLocalInput(value.start),
            schedule_end_time:
              value.mode === "START_END" ? utcFromLocalInput(value.end) : null,
          },
        },
      })
      sessionStorage.removeItem(key)
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
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>投放排期</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">
          在本次广告搭建中设置。日期时间使用选择器填写，提交前会按广告账户时区转换为
          TikTok API 所需的 UTC。
        </p>
        <RadioGroup
          value={value.mode}
          disabled={disabled}
          onValueChange={(mode) => change({ mode: mode as Mode })}
          className="grid gap-3 md:grid-cols-3"
        >
          {[
            ["IMMEDIATE", "立即开始"],
            ["START_AT", "指定时间开启"],
            ["START_END", "指定开始和结束时间"],
          ].map(([mode, label]) => (
            <label
              key={mode}
              htmlFor={`schedule-mode-${mode}`}
              className="flex items-center gap-2 rounded-md border p-3 text-sm"
            >
              <RadioGroupItem id={`schedule-mode-${mode}`} value={mode} />
              {label}
            </label>
          ))}
        </RadioGroup>
        {value.mode !== "IMMEDIATE" && (
          <div className="grid gap-4 md:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="schedule-start">开始时间</FieldLabel>
              <Input
                id="schedule-start"
                type="datetime-local"
                value={value.start}
                disabled={disabled}
                onChange={(event) => change({ start: event.target.value })}
              />
            </Field>
            {value.mode === "START_END" && (
              <Field>
                <FieldLabel htmlFor="schedule-end">结束时间</FieldLabel>
                <Input
                  id="schedule-end"
                  type="datetime-local"
                  value={value.end}
                  disabled={disabled}
                  onChange={(event) => change({ end: event.target.value })}
                />
              </Field>
            )}
          </div>
        )}
        {inputError && (
          <p role="alert" className="text-sm text-destructive">
            {inputError}
          </p>
        )}
        {!!error && <BuildError error={error} />}
        {write && (
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={disabled || !dirty}
              onClick={() => void save()}
            >
              {busy ? "正在保存…" : "保存排期"}
            </Button>
            {dirty && (
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setInputError("")
                  setValue(saved)
                }}
              >
                撤销修改
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
