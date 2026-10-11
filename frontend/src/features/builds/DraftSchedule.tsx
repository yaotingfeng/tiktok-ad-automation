import type { DeliveryConfig, DraftSummary } from "@/client"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"

type Mode = "IMMEDIATE" | "START_AT" | "START_END"
export type ScheduleValue = { mode: Mode; start: string; end: string }

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

export function scheduleFromSummary(summary: DraftSummary): ScheduleValue {
  return {
    mode: summary.delivery_config?.schedule_mode || "IMMEDIATE",
    start: localInputFromUtc(summary.delivery_config?.schedule_start_time),
    end: localInputFromUtc(summary.delivery_config?.schedule_end_time),
  }
}

export function scheduleError(value: ScheduleValue) {
  if (value.mode !== "IMMEDIATE" && !value.start) return "请选择投放开始时间。"
  if (value.mode === "START_END" && !value.end) return "请选择投放结束时间。"
  if (
    value.mode === "START_END" &&
    new Date(value.end) <= new Date(value.start)
  )
    return "结束时间必须晚于开始时间。"
  return ""
}

export function deliveryConfig(value: ScheduleValue): DeliveryConfig {
  return {
    schedule_mode: value.mode,
    schedule_start_time:
      value.mode === "IMMEDIATE" ? null : utcFromLocalInput(value.start),
    schedule_end_time:
      value.mode === "START_END" ? utcFromLocalInput(value.end) : null,
  }
}

// 投放排期属于本次搭建，在第二步投放身份之后填写；
// 不单独保存，点击“生成搭建预览”时由准备页先保存排期再生成预览。
export function DraftSchedule({
  value,
  onChange,
  disabled,
  error,
}: {
  value: ScheduleValue
  onChange: (value: ScheduleValue) => void
  disabled: boolean
  error: string
}) {
  const change = (patch: Partial<ScheduleValue>) =>
    onChange({ ...value, ...patch })
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>投放排期</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">
          在本次广告搭建中设置，生成搭建预览时一并保存。日期时间使用选择器填写，提交前会按广告账户时区转换为
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
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
