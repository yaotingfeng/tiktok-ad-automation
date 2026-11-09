import { useQuery, useQueryClient } from "@tanstack/react-query"
import { isAxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import {
  BusinessService,
  type RateInput,
  type RatePeriod,
  type RatePreviewPublic,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { businessKey, percentageRate, rateKey, shiftedDay } from "./queries"
import { RateHistory } from "./RateHistory"
import { RatePeriodTable, ratePercentage, rateText } from "./RatePeriodTable"

export function RateSettings({
  tenantId,
  kind,
  scopeId,
  name,
  onClose,
}: {
  tenantId: string
  kind: "provider" | "rebate"
  scopeId: string
  name: string
  onClose: () => void
}) {
  const query = useQuery({
    queryKey: rateKey(tenantId, kind, scopeId),
    queryFn: async ({ signal }) =>
      (
        await BusinessService.rates({
          path: { tenant_id: tenantId },
          query: { kind, scope_id: scopeId },
          signal,
        })
      ).data,
  })
  const today = new Date().toISOString().slice(0, 10)
  const [referenceDay, setReferenceDay] = useState(today)
  const [action, setAction] = useState<"set" | "edit" | "end">("set")
  const [target, setTarget] = useState<RatePeriod | null>(null)
  const [day, setDay] = useState(shiftedDay(today, 1))
  const [end, setEnd] = useState("")
  const [forever, setForever] = useState(true)
  const [rate, setRate] = useState("0")
  const [note, setNote] = useState("")
  const [successor, setSuccessor] = useState(false)
  const [successorRate, setSuccessorRate] = useState("0")
  const [successorEnd, setSuccessorEnd] = useState("")
  const [successorForever, setSuccessorForever] = useState(true)
  const [dirty, setDirty] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [preview, setPreview] = useState<{
    result: RatePreviewPublic
    body: RateInput
  } | null>(null)
  const client = useQueryClient()
  const contentRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // 切换预览时从顶部展示完整影响范围，避免沿用表单底部的滚动位置。
    if (preview) contentRef.current?.parentElement?.scrollTo({ top: 0 })
  }, [preview])
  const periods = query.data?.periods ?? []
  const label =
    kind === "provider" ? "版权方抽成比例（%）" : "其他返点比例（%）"
  const current = periods.find(
    (p) =>
      p.start_date <= referenceDay &&
      (!p.end_date || p.end_date >= referenceDay),
  )
  const next = periods.find((p) => p.start_date > referenceDay)
  useEffect(() => {
    if (!dirty && query.data) {
      const p = query.data.periods.find(
        (p) => p.start_date <= day && (!p.end_date || day <= p.end_date),
      )
      setRate(ratePercentage(p?.rate))
    }
  }, [query.data, day, dirty])

  const choose = (p: RatePeriod, mode: "set" | "edit" | "end") => {
    setAction(mode)
    setTarget(mode === "set" ? null : p)
    setForever(!p.end_date)
    setDay(p.start_date)
    setEnd(
      mode === "end"
        ? p.end_date || (today < p.start_date ? p.start_date : today)
        : p.end_date || "",
    )
    setRate(ratePercentage(p.rate))
    const following = periods.find(
      (other) => other.start_date > p.start_date && other.rate != null,
    )
    setSuccessorEnd(following ? shiftedDay(following.start_date, -1) : "")
    setSuccessorForever(!following)
    setSuccessor(false)
    setError("")
    setDirty(true)
  }
  const fail = async (caught: unknown) => {
    setPreview(null)
    if (isAxiosError(caught) && caught.response?.status === 409) {
      setError(
        "比例配置已被修改，已刷新时间段。你的输入已保留，请核对后重新预览。",
      )
      await query.refetch()
    } else {
      setError("操作失败，请检查日期、比例、原时间段及当前管理权限后重试。")
    }
  }
  const submit = async () => {
    if (pending || !query.data) return
    setPending(true)
    setError("")
    try {
      if (preview) {
        // 使用预览时冻结的原操作及修订；刷新不能让确认绕过并发检查。
        await BusinessService.setRate({
          path: { tenant_id: tenantId },
          body: preview.body,
        })
        await client.invalidateQueries({ queryKey: businessKey(tenantId) })
        onClose()
      } else {
        const body: RateInput = {
          kind,
          scope_id: scopeId,
          expected_revision: query.data.revision,
          action,
          start_date: day,
          end_date: action === "end" ? end : forever ? null : end,
          rate: action === "end" ? null : percentageRate(rate),
          target_start_date: target?.start_date ?? null,
          note,
          successor_rate:
            action === "end" && successor
              ? percentageRate(successorRate)
              : null,
          successor_end_date:
            action === "end" && successor && !successorForever
              ? successorEnd
              : null,
        }
        const result = (
          await BusinessService.previewRate({
            path: { tenant_id: tenantId },
            body,
          })
        ).data
        setPreview({ result, body })
      }
    } catch (caught) {
      await fail(caught)
    } finally {
      setPending(false)
    }
  }
  return (
    <ManagementSheet
      title={kind === "provider" ? "版权方抽成设置" : "经营设置"}
      description={`${name} · 按广告账户当地业务日期适用；截止日期包含当天。`}
      dirty={dirty}
      pending={pending}
      onClose={onClose}
      wide
      actions={
        <>
          {preview && (
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => setPreview(null)}
            >
              返回修改
            </Button>
          )}
          <Button
            type="submit"
            form="business-rate-form"
            disabled={pending || !query.data || query.isError}
          >
            {pending ? "处理中…" : preview ? "确认保存" : "预览调整"}
          </Button>
        </>
      }
    >
      <div ref={contentRef} className="flex flex-col gap-5 py-2">
        {error && (
          <Alert variant="destructive" role="alert">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {query.isError && (
          <Alert variant="destructive">
            <AlertDescription>
              比例时间段读取失败。
              <Button variant="link" onClick={() => void query.refetch()}>
                重试
              </Button>
            </AlertDescription>
          </Alert>
        )}
        {query.isPending && <p>正在读取比例时间段…</p>}
        {preview ? (
          <>
            <Alert>
              <AlertTitle>请核对调整结果</AlertTitle>
              <AlertDescription>
                填写范围内的原比例将被替代，范围外保留。涉及历史日期时，后台将重新核算，完成前看板继续显示上次完整结果。
              </AlertDescription>
            </Alert>
            <section aria-label="调整前">
              <h3>调整前的时间段</h3>
              <RatePeriodTable periods={preview.result.before} />
            </section>
            <section aria-label="调整后">
              <h3>保存后的时间段</h3>
              <RatePeriodTable periods={preview.result.periods} />
            </section>
            {preview.result.periods.some((p) => p.rate == null) && (
              <Alert>
                <AlertTitle>存在待配置时间段</AlertTitle>
                <AlertDescription>
                  待配置日期的相关经营金额暂不核算，不会按 0%
                  计入。保存后可以继续补齐比例。
                </AlertDescription>
              </Alert>
            )}
            {preview.body.note && <p>调整说明：{preview.body.note}</p>}
          </>
        ) : (
          <>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="rate-reference">查看日期</FieldLabel>
                <Input
                  id="rate-reference"
                  type="date"
                  required
                  value={referenceDay}
                  onChange={(e) => setReferenceDay(e.target.value)}
                />
                <FieldDescription>
                  {referenceDay} 适用比例：
                  {current ? rateText(current.rate) : "默认 0%，未设置"}。
                  {next
                    ? `下一次变化：${next.start_date}，${rateText(next.rate)}。`
                    : ""}
                </FieldDescription>
              </Field>
            </FieldGroup>
            <section aria-label="当前时间段">
              <h3>比例时间段</h3>
              <RatePeriodTable
                periods={periods}
                referenceDay={referenceDay}
                onEdit={(p) => choose(p, "edit")}
                onEnd={(p) => choose(p, "end")}
                onFill={(p) => choose(p, "set")}
              />
              {periods.length > 0 && (
                <p className="text-muted-foreground">
                  {periods[0].start_date} 之前默认
                  0%。待配置表示该日期的比例尚未确定。
                </p>
              )}
            </section>
            <Separator />
          </>
        )}
        <form
          id="business-rate-form"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
          onChange={() => setDirty(true)}
        >
          {!preview && (
            <FieldSet disabled={pending || !query.data}>
              <FieldLegend>
                {action === "end"
                  ? "结束当前比例"
                  : action === "edit"
                    ? "修改时间段"
                    : "新增 / 从某天调整比例"}
              </FieldLegend>
              {action !== "set" && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setAction("set")
                    setTarget(null)
                    setSuccessor(false)
                    setDay(shiftedDay(today, 1))
                    setEnd("")
                    setForever(true)
                    setDirty(true)
                  }}
                >
                  改为新增比例
                </Button>
              )}
              {/* 表单模式变化时重新挂载布局，避免浏览器复用切换前的flex排版。 */}
              <FieldGroup
                key={`${action}:${forever}:${successor}:${successorForever}`}
              >
                <Field>
                  <FieldLabel htmlFor="business-rate-date">开始日期</FieldLabel>
                  <Input
                    id="business-rate-date"
                    type="date"
                    required
                    value={day}
                    disabled={action === "end"}
                    onChange={(e) => setDay(e.target.value)}
                  />
                </Field>
                {action !== "end" ? (
                  <>
                    <Field orientation="horizontal">
                      <Checkbox
                        id="rate-forever"
                        checked={forever}
                        onCheckedChange={(checked) => {
                          setForever(checked === true)
                          if (!end) setEnd(day)
                          setDirty(true)
                        }}
                      />
                      <FieldLabel htmlFor="rate-forever">持续有效</FieldLabel>
                    </Field>
                    {!forever && (
                      <Field>
                        <FieldLabel htmlFor="rate-end">结束日期</FieldLabel>
                        <Input
                          id="rate-end"
                          type="date"
                          required
                          min={day}
                          value={end}
                          onChange={(e) => setEnd(e.target.value)}
                        />
                      </Field>
                    )}
                    <Field>
                      <FieldLabel htmlFor="business-rate">{label}</FieldLabel>
                      <Input
                        id="business-rate"
                        type="number"
                        min="0"
                        max="100"
                        step="0.000001"
                        required
                        value={rate}
                        onChange={(e) => setRate(e.target.value)}
                      />
                      <FieldDescription>
                        {kind === "provider"
                          ? "按平台收入计算抽成，剩余部分计入分成后收入。"
                          : "按平台消耗计算，填写代理、主体等返点的合计比例。"}
                      </FieldDescription>
                    </Field>
                  </>
                ) : (
                  <>
                    <Field>
                      <FieldLabel htmlFor="rate-cutoff">截止日期</FieldLabel>
                      <Input
                        id="rate-cutoff"
                        type="date"
                        required
                        min={day}
                        max={target?.end_date || "9999-12-30"}
                        value={end}
                        onChange={(e) => setEnd(e.target.value)}
                      />
                      <FieldDescription>
                        当前比例为 {rateText(target?.rate)}
                        。截止后的空档将标记为待配置。
                      </FieldDescription>
                    </Field>
                    <Field orientation="horizontal">
                      <Checkbox
                        id="rate-successor"
                        checked={successor}
                        onCheckedChange={(checked) => {
                          setSuccessor(checked === true)
                          setDirty(true)
                        }}
                      />
                      <FieldLabel htmlFor="rate-successor">
                        同时设置接续比例
                      </FieldLabel>
                    </Field>
                    {successor && (
                      <FieldSet>
                        <FieldLegend variant="label">
                          {end ? shiftedDay(end, 1) : "截止日次日"} 起接续
                        </FieldLegend>
                        <Field>
                          <FieldLabel htmlFor="successor-rate">
                            接续比例（%）
                          </FieldLabel>
                          <Input
                            id="successor-rate"
                            type="number"
                            min="0"
                            max="100"
                            step="0.000001"
                            required
                            value={successorRate}
                            onChange={(e) => setSuccessorRate(e.target.value)}
                          />
                        </Field>
                        <Field orientation="horizontal">
                          <Checkbox
                            id="successor-forever"
                            checked={successorForever}
                            onCheckedChange={(checked) => {
                              setSuccessorForever(checked === true)
                              if (!successorEnd)
                                setSuccessorEnd(end ? shiftedDay(end, 1) : day)
                              setDirty(true)
                            }}
                          />
                          <FieldLabel htmlFor="successor-forever">
                            接续比例持续有效
                          </FieldLabel>
                        </Field>
                        {!successorForever && (
                          <Field>
                            <FieldLabel htmlFor="successor-end">
                              接续结束日期
                            </FieldLabel>
                            <Input
                              id="successor-end"
                              type="date"
                              required
                              min={end ? shiftedDay(end, 1) : day}
                              value={successorEnd}
                              onChange={(e) => setSuccessorEnd(e.target.value)}
                            />
                          </Field>
                        )}
                        <FieldDescription>
                          默认保留下一条已安排配置；选择持续有效将替代后续全部安排，请核对预览。
                        </FieldDescription>
                      </FieldSet>
                    )}
                  </>
                )}
                <Field>
                  <FieldLabel htmlFor="rate-note">调整说明（选填）</FieldLabel>
                  <Textarea
                    id="rate-note"
                    maxLength={500}
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                  />
                </Field>
                <FieldDescription>
                  影响范围：{name}
                  。保存前会展示所有调整后的时间段，历史修改记录保留供追溯。
                </FieldDescription>
              </FieldGroup>
            </FieldSet>
          )}
        </form>
        {!preview && (
          <RateHistory tenantId={tenantId} kind={kind} scopeId={scopeId} />
        )}
      </div>
    </ManagementSheet>
  )
}
