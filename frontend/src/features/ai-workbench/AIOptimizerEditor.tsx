import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { AccountPicker } from "@/features/accounts/AccountPicker"
import { RuleSelectionDialog } from "./RuleSelectionDialog"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { AIOptimizer } from "./AIOptimizerList"
import { aiRequest, allItems, createRequestIdentity, errorMessage } from "./api"

type BC = { bc_id: string; name: string; ownership_conflict?: boolean }
type Strategy = {
  id: string
  version_id: string
  name: string
  latest_version: number
  active: boolean
}
type Rule = { id: string; name: string }

function Choice({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string
  value: string
  options: Array<[string, string]>
  onChange: (value: string) => void
  disabled?: boolean
}) {
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Select value={value} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger aria-label={label} className="w-full">
          <SelectValue placeholder="请选择" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {options.map(([id, title]) => (
              <SelectItem value={id} key={id}>
                {title}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </Field>
  )
}

/** 投手只维护范围与运行模式；策略和规则由领域模块及 AI 能力目录统一校验。 */
export function AIOptimizerEditor({
  tenantId,
  optimizer,
  onSaved,
  onClose,
}: {
  tenantId: string
  optimizer: AIOptimizer | null
  onSaved: (optimizer: AIOptimizer) => void
  onClose: () => void
}) {
  const lifecycle = useRef(new AbortController())
  useEffect(() => {
    const controller = new AbortController()
    lifecycle.current = controller
    return () => controller.abort()
  }, [])
  const requestIdentity = useRef(createRequestIdentity())
  const [name, setName] = useState(optimizer?.name ?? "")
  const [bcId, setBcId] = useState(optimizer?.bc_id ?? "")
  const [accountIds, setAccountIds] = useState(optimizer?.advertiser_ids ?? [])
  const [strategyId, setStrategyId] = useState(
    optimizer?.strategy_version_id ?? "",
  )
  const [ruleIds, setRuleIds] = useState(optimizer?.rule_ids ?? [])
  const [spend, setSpend] = useState(String(optimizer?.daily_spend ?? ""))
  const [roas, setRoas] = useState(String(optimizer?.target_roas ?? ""))
  const [priority, setPriority] = useState(optimizer?.priority ?? "BALANCED")
  const [mode, setMode] = useState(optimizer?.optimization_mode ?? "MANUAL")
  const [timezone, setTimezone] = useState(
    optimizer?.timezone ?? "Asia/Shanghai",
  )
  const [reviewTime, setReviewTime] = useState(
    (optimizer?.review_time ?? "13:00").slice(0, 5),
  )
  const [rulePickerOpen, setRulePickerOpen] = useState(false)
  const [bcs, setBcs] = useState<BC[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [rules, setRules] = useState<Rule[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const prefix = `/api/tenants/${tenantId}`
    Promise.all([
      allItems<BC>(`${prefix}/bcs`, controller.signal),
      allItems<Strategy>(`${prefix}/strategies`, controller.signal),
      allItems<Rule>(`${prefix}/automation-rules`, controller.signal),
    ])
      .then(([nextBcs, nextStrategies, nextRules]) => {
        if (controller.signal.aborted) return
        setBcs(nextBcs.filter((bc) => !bc.ownership_conflict))
        setStrategies(nextStrategies)
        setRules(nextRules)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [tenantId])

  async function save() {
    setError(null)
    try {
      if (
        !name.trim() ||
        !bcId ||
        !strategyId ||
        !accountIds.length ||
        !(Number(spend) > 0)
      )
        throw new Error("请填写名称、BC、账户、唯一策略和大于零的日消耗目标")
      if (priority === "ROAS" && !(Number(roas) > 0))
        throw new Error("ROAS 优先时必须填写目标 ROAS")
      if (roas && !(Number(roas) > 0)) throw new Error("目标 ROAS 必须大于零")
      new Intl.DateTimeFormat("en", { timeZone: timezone }).format()
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(reviewTime))
        throw new Error("请填写有效复盘时间")
      setSaving(true)
      const body = {
        name: name.trim(),
        bc_id: bcId,
        advertiser_ids: accountIds,
        strategy_version_id: strategyId,
        rule_ids: ruleIds,
        daily_spend: spend,
        target_roas: roas || null,
        priority,
        optimization_mode: mode,
        timezone,
        review_time: reviewTime,
        ...(optimizer ? {} : { active: true }),
      }
      const result = await aiRequest<AIOptimizer>(
        `/api/tenants/${tenantId}/ai-optimizers${optimizer ? `/${optimizer.id}` : ""}`,
        {
          method: optimizer ? "PATCH" : "POST",
          signal: lifecycle.current.signal,
          body: { ...body, request_id: requestIdentity.current(body) },
        },
      )
      if (!lifecycle.current.signal.aborted) onSaved(result)
    } catch (reason) {
      if (!lifecycle.current.signal.aborted) setError(errorMessage(reason))
    } finally {
      if (!lifecycle.current.signal.aborted) setSaving(false)
    }
  }

  const strategyOptions: Array<[string, string]> = strategies
    .filter((item) => item.active || item.version_id === strategyId)
    .map((item) => [item.version_id, `${item.name} · v${item.latest_version}`])
  if (strategyId && !strategyOptions.some(([id]) => id === strategyId))
    strategyOptions.unshift([strategyId, `当前绑定版本 ${strategyId}`])
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !saving) onClose()
      }}
    >
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{optimizer ? "编辑投手" : "创建投手"}</DialogTitle>
          <DialogDescription>
            绑定已有策略与规则。每日剧单继承此处的 BC
            和账户；暂停后停止新的自动任务。
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void save()
          }}
          className="flex flex-col gap-5"
        >
          <fieldset disabled={saving || loading} className="min-w-0">
            <FieldGroup className="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="optimizer-name">投手名称</FieldLabel>
                <Input
                  id="optimizer-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  required
                  maxLength={120}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="optimizer-spend">日消耗目标</FieldLabel>
                <Input
                  id="optimizer-spend"
                  type="number"
                  step="any"
                  min="0.01"
                  value={spend}
                  onChange={(event) => setSpend(event.target.value)}
                  required
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="optimizer-roas">目标 ROAS</FieldLabel>
                <Input
                  id="optimizer-roas"
                  type="number"
                  step="any"
                  min="0.01"
                  value={roas}
                  onChange={(event) => setRoas(event.target.value)}
                />
              </Field>
              <Choice
                label="目标优先级"
                value={priority}
                options={[
                  ["BALANCED", "平衡"],
                  ["SPEND", "消耗优先"],
                  ["ROAS", "ROAS 优先"],
                ]}
                onChange={(value) => setPriority(value as typeof priority)}
              />
              <Choice
                label="优化模式"
                value={mode}
                options={[
                  ["MANUAL", "人工确认"],
                  ["AUTO", "自动优化"],
                ]}
                onChange={(value) => setMode(value as typeof mode)}
              />
              <p className="text-xs text-muted-foreground sm:col-span-2">
                人工确认会先生成提案；自动优化会在当前账户范围和领域能力内执行。素材下单使用平台内置
                CL6 v4、CL8 v5、GM-精剪 v4，无需填写策略 ID 或表格。
              </p>
              <Choice
                label="绑定策略版本"
                value={strategyId}
                options={strategyOptions}
                onChange={setStrategyId}
              />
              <Field>
                <FieldLabel htmlFor="optimizer-timezone">
                  运营时区（IANA）
                </FieldLabel>
                <Input
                  id="optimizer-timezone"
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                  required
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="optimizer-review-time">
                  每日复盘时间
                </FieldLabel>
                <Input
                  id="optimizer-review-time"
                  type="time"
                  value={reviewTime}
                  onChange={(event) => setReviewTime(event.target.value)}
                  required
                />
              </Field>
            </FieldGroup>
          </fieldset>
          <FieldSet
            disabled={saving || loading}
            className="rounded-lg border p-4"
          >
            <FieldLegend>管理范围</FieldLegend>
            <FieldGroup className="grid gap-4 sm:grid-cols-2">
              <Choice
                label="BC"
                value={bcId}
                options={bcs.map((bc) => [
                  bc.bc_id,
                  `${bc.name} · ${bc.bc_id}`,
                ])}
                onChange={(value) => {
                  setBcId(value)
                  setAccountIds([])
                }}
              />
              <Field>
                <FieldLabel>广告账户</FieldLabel>
                <AccountPicker
                  key={bcId}
                  tenantId={tenantId}
                  bcId={bcId}
                  value={accountIds.join("\n")}
                  disabled={!bcId || saving || loading}
                  selectionMode="replace"
                  triggerLabel={
                    accountIds.length
                      ? `已选择 ${accountIds.length} 个账户`
                      : "选择管理账户"
                  }
                  onChange={(value) =>
                    setAccountIds(value.split("\n").filter(Boolean))
                  }
                />
              </Field>
            </FieldGroup>
            <p className="text-xs text-muted-foreground">
              账户仅限所选 BC；切换 BC 会清空账户选择。AI
              新建规则也受此管理范围约束。
            </p>
          </FieldSet>
          <FieldSet disabled={saving}>
            <FieldLegend>绑定已有自动规则</FieldLegend>
            <Button
              type="button"
              variant="outline"
              className="justify-between"
              onClick={() => setRulePickerOpen(true)}
            >
              <span>
                {ruleIds.length
                  ? `已绑定 ${ruleIds.length} 条规则`
                  : "选择自动规则"}
              </span>
              <span className="text-xs text-muted-foreground">打开选择器</span>
            </Button>
            <p className="text-xs text-muted-foreground">
              规则由自动规则引擎执行，作用范围仍受投手的 BC 和账户边界约束。
            </p>
          </FieldSet>

          <RuleSelectionDialog
            key={rulePickerOpen ? "open" : "closed"}
            open={rulePickerOpen}
            onOpenChange={setRulePickerOpen}
            rules={rules}
            value={ruleIds}
            onChange={setRuleIds}
          />

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={saving}
              onClick={onClose}
            >
              取消
            </Button>
            <Button type="submit" disabled={loading || saving}>
              {saving ? "保存中…" : optimizer ? "保存投手" : "创建投手"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
