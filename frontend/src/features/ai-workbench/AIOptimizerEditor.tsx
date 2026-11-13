import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
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
type Account = {
  advertiser_id: string
  bc_id: string
  name: string
  can_build: boolean
  ownership_conflict?: boolean
}
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

/** 这里只维护投手引用和授权；策略、规则正文仍由原模块编辑。 */
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
  const limits = optimizer?.authorization_limits ?? {}
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
  const authorized = (key: string) =>
    limits[key] === true ||
    (typeof limits[key] === "object" &&
      limits[key] !== null &&
      (limits[key] as { allowed?: boolean }).allowed === true)
  const [strategyAllowed, setStrategyAllowed] = useState(authorized("strategy"))
  const [ruleAllowed, setRuleAllowed] = useState(authorized("rule"))
  const [budgetDelta, setBudgetDelta] = useState(
    String(limits.max_budget_delta ?? ""),
  )
  const [spendDelta, setSpendDelta] = useState(
    String(limits.max_daily_spend_delta ?? ""),
  )
  const [materialIds, setMaterialIds] = useState(
    (optimizer?.material_config?.strategy_ids ?? []).join(","),
  )
  const [materialSheet, setMaterialSheet] = useState(
    optimizer?.material_config?.feishu_token ?? "",
  )
  const [strategyFields, setStrategyFields] = useState<string[]>(
    Array.isArray(limits.strategy_allowed_fields)
      ? (limits.strategy_allowed_fields as string[])
      : [],
  )
  const [ruleFields, setRuleFields] = useState<string[]>(
    Array.isArray(limits.rule_allowed_fields)
      ? (limits.rule_allowed_fields as string[])
      : [],
  )
  const [roasDelta, setRoasDelta] = useState(
    String(limits.max_target_roas_delta ?? ""),
  )
  const [bcs, setBcs] = useState<BC[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [rules, setRules] = useState<Rule[]>([])
  const [loading, setLoading] = useState(true)
  const [accountsLoading, setAccountsLoading] = useState(false)
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

  useEffect(() => {
    setAccounts([])
    if (!bcId) return
    const controller = new AbortController()
    setAccountsLoading(true)
    allItems<Account>(
      `/api/tenants/${tenantId}/accounts?bc_id=${encodeURIComponent(bcId)}`,
      controller.signal,
    )
      .then((items) => {
        if (!controller.signal.aborted)
          setAccounts(items.filter((item) => item.bc_id === bcId))
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setAccountsLoading(false)
      })
    return () => controller.abort()
  }, [tenantId, bcId])

  function toggle(ids: string[], id: string, checked: boolean) {
    return checked
      ? [...new Set([...ids, id])]
      : ids.filter((item) => item !== id)
  }
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
      if (
        [budgetDelta, spendDelta, roasDelta].some(
          (value) =>
            value !== "" &&
            (!Number.isFinite(Number(value)) || Number(value) < 0),
        )
      )
        throw new Error("授权调整上限必须为非负数")
      setSaving(true)
      // 仅修改已展示的授权键，保留服务器认可的其他细分边界。
      const updatePermission = (kind: string, allowed: boolean) =>
        typeof limits[kind] === "object" && limits[kind] !== null
          ? { ...(limits[kind] as Record<string, unknown>), allowed }
          : allowed
      const authorization: Record<string, unknown> = {
        ...limits,
        strategy: updatePermission("strategy", strategyAllowed),
        rule: updatePermission("rule", ruleAllowed),
      }
      if (budgetDelta) authorization.max_budget_delta = Number(budgetDelta)
      else delete authorization.max_budget_delta
      if (spendDelta) authorization.max_daily_spend_delta = Number(spendDelta)
      else delete authorization.max_daily_spend_delta
      if (roasDelta) authorization.max_target_roas_delta = Number(roasDelta)
      else delete authorization.max_target_roas_delta
      if (strategyFields.length)
        authorization.strategy_allowed_fields = strategyFields
      else delete authorization.strategy_allowed_fields
      if (ruleFields.length) authorization.rule_allowed_fields = ruleFields
      else delete authorization.rule_allowed_fields
      const materialStrategies = [
        ...new Set(
          materialIds
            .split(/[,，\n]/)
            .map((item) => item.trim())
            .filter(Boolean),
        ),
      ]
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
        authorization_limits: authorization,
        material_config:
          materialStrategies.length || materialSheet.trim()
            ? {
                strategy_ids: materialStrategies,
                feishu_token: materialSheet.trim(),
              }
            : {},
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
          <FieldSet disabled={saving}>
            <FieldLegend>广告账户（仅当前 BC）</FieldLegend>
            <FieldGroup className="max-h-48 overflow-y-auto">
              {accountsLoading ? (
                <p>读取账户中…</p>
              ) : (
                accounts.map((account) => (
                  <Field key={account.advertiser_id} orientation="horizontal">
                    <Checkbox
                      id={`account-${account.advertiser_id}`}
                      checked={accountIds.includes(account.advertiser_id)}
                      disabled={
                        !account.can_build || account.ownership_conflict
                      }
                      onCheckedChange={(checked) =>
                        setAccountIds(
                          toggle(
                            accountIds,
                            account.advertiser_id,
                            checked === true,
                          ),
                        )
                      }
                    />
                    <FieldLabel htmlFor={`account-${account.advertiser_id}`}>
                      {account.name} · {account.advertiser_id}
                      {!account.can_build ? "（不可搭建）" : ""}
                    </FieldLabel>
                  </Field>
                ))
              )}
              {!accountsLoading && !accounts.length && (
                <p className="text-sm text-muted-foreground">
                  请先选择已有授权 BC；该目录为空时在“账户与授权”完成同步。
                </p>
              )}
            </FieldGroup>
          </FieldSet>
          <FieldSet disabled={saving}>
            <FieldLegend>绑定已有自动规则</FieldLegend>
            <FieldGroup className="max-h-48 overflow-y-auto">
              {rules.map((rule) => (
                <Field orientation="horizontal" key={rule.id}>
                  <Checkbox
                    id={`rule-${rule.id}`}
                    checked={ruleIds.includes(rule.id)}
                    onCheckedChange={(checked) =>
                      setRuleIds(toggle(ruleIds, rule.id, checked === true))
                    }
                  />
                  <FieldLabel htmlFor={`rule-${rule.id}`}>
                    {rule.name}
                  </FieldLabel>
                </Field>
              ))}
              {!rules.length && (
                <p className="text-sm text-muted-foreground">
                  暂无规则，可稍后在“自动规则”创建并绑定。
                </p>
              )}
            </FieldGroup>
          </FieldSet>
          <FieldSet disabled={saving}>
            <FieldLegend>素材下单配置</FieldLegend>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="material-strategies">
                  素材制作策略 ID
                </FieldLabel>
                <Input
                  id="material-strategies"
                  value={materialIds}
                  onChange={(event) => setMaterialIds(event.target.value)}
                  placeholder="填写已启用的素材制作策略 ID，多个用逗号分隔"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="material-sheet">素材回写表格</FieldLabel>
                <Input
                  id="material-sheet"
                  value={materialSheet}
                  onChange={(event) => setMaterialSheet(event.target.value)}
                  placeholder="飞书表格链接或 token"
                />
              </Field>
            </FieldGroup>
            <p className="text-xs text-muted-foreground">
              只有剧单勾选下单时使用。必须使用本租户素材工作台已启用的策略；未配置时保留下单意图并显示受阻。
            </p>
          </FieldSet>
          <FieldSet disabled={saving}>
            <FieldLegend>AI 发布授权</FieldLegend>
            <FieldGroup>
              <Field orientation="horizontal">
                <Checkbox
                  id="authorize-strategy"
                  checked={strategyAllowed}
                  onCheckedChange={(checked) =>
                    setStrategyAllowed(checked === true)
                  }
                />
                <FieldLabel htmlFor="authorize-strategy">
                  允许调整绑定策略
                </FieldLabel>
              </Field>
              <Field orientation="horizontal">
                <Checkbox
                  id="authorize-rule"
                  checked={ruleAllowed}
                  onCheckedChange={(checked) =>
                    setRuleAllowed(checked === true)
                  }
                />
                <FieldLabel htmlFor="authorize-rule">
                  允许调整绑定规则
                </FieldLabel>
              </Field>
              <Field>
                <FieldLabel htmlFor="budget-delta">
                  单次预算调整绝对上限
                </FieldLabel>
                <Input
                  id="budget-delta"
                  type="number"
                  min="0"
                  step="any"
                  value={budgetDelta}
                  onChange={(event) => setBudgetDelta(event.target.value)}
                  placeholder="未授权"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="spend-delta">
                  单次日消耗调整绝对上限
                </FieldLabel>
                <Input
                  id="spend-delta"
                  type="number"
                  min="0"
                  step="any"
                  value={spendDelta}
                  onChange={(event) => setSpendDelta(event.target.value)}
                  placeholder="未授权"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="roas-delta">
                  单次 ROAS 出价调整绝对上限
                </FieldLabel>
                <Input
                  id="roas-delta"
                  type="number"
                  min="0"
                  step="any"
                  value={roasDelta}
                  onChange={(event) => setRoasDelta(event.target.value)}
                  placeholder="未授权"
                />
              </Field>
              <Field orientation="horizontal">
                <Checkbox
                  id="allow-structure"
                  checked={strategyFields.includes("group_count")}
                  onCheckedChange={(checked) =>
                    setStrategyFields(
                      checked
                        ? [
                            "group_count",
                            "ads_per_group",
                            "group_generation_mode",
                            "ad_generation_mode",
                            "creative_count",
                          ]
                        : [],
                    )
                  }
                />
                <FieldLabel htmlFor="allow-structure">
                  允许自动调整搭建数量与结构
                </FieldLabel>
              </Field>
              {[
                ["conditions", "允许自动调整规则条件"],
                ["action", "允许自动调整规则动作"],
                ["schedule", "允许自动调整规则执行时间"],
              ].map(([key, label]) => (
                <Field key={key} orientation="horizontal">
                  <Checkbox
                    id={`allow-rule-${key}`}
                    checked={ruleFields.includes(key)}
                    onCheckedChange={(checked) =>
                      setRuleFields(toggle(ruleFields, key, checked === true))
                    }
                  />
                  <FieldLabel htmlFor={`allow-rule-${key}`}>{label}</FieldLabel>
                </Field>
              ))}
            </FieldGroup>
            <p className="text-xs text-muted-foreground">
              授权账户仅限上方所选范围。人工模式每次仍需确认；自动模式的未授权调整会被阻止。
            </p>
          </FieldSet>
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
            <Button
              type="submit"
              disabled={loading || accountsLoading || saving}
            >
              {saving ? "保存中…" : optimizer ? "保存投手" : "创建投手"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
