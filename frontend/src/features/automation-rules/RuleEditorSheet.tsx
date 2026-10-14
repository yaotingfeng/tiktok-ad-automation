import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Plus, Trash2 } from "lucide-react"
import { type ReactNode, useMemo, useState } from "react"
import {
  AccountsService,
  AutomationRulesService,
  type CatalogPublic,
  type DryRunPublic,
  type Period,
  type RulePublic,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { errorMessage } from "@/features/tenants/shared"
import { ConditionRows } from "./ConditionRows"
import { DryRunPanel } from "./DryRunPanel"
import { ruleKeys, settingsQuery } from "./queries"
import {
  ACTION_LABELS,
  type ActionType,
  AGGREGATION_LABELS,
  type Aggregation,
  actionLevels,
  type Definition,
  emptyDefinition,
  FREQUENCY_LABELS,
  type Frequency,
  LEVEL_LABELS,
  LEVELS,
  type Level,
  minFrequency,
  validateDefinition,
} from "./ruleForm"

const FREQUENCY_ORDER: Frequency[] = ["30M", "1H", "2H", "4H", "DAILY"]
const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

export type EditorTarget =
  | { kind: "create"; name?: string; definition?: Definition }
  | { kind: "edit"; rule: RulePublic }

function defaultName() {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, "0")
  return `规则 ${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}`
}

const lines = (value: string) =>
  value
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean)

/** 输入框保留原始文本，避免受控回写时吞掉正在输入的逗号。 */
function MonthDaysInput({
  label,
  days,
  onChange,
}: {
  label: string
  days: number[]
  onChange: (days: number[]) => void
}) {
  const [text, setText] = useState(days.join(","))
  return (
    <Input
      aria-label={label}
      placeholder="日期，如 1,15"
      className="w-36"
      value={text}
      onChange={(e) => {
        setText(e.target.value)
        onChange(
          lines(e.target.value)
            .map(Number)
            .filter((day) => Number.isInteger(day)),
        )
      }}
    />
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 border-b py-4 last:border-b-0">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  )
}

function Choice({
  value,
  label,
  disabled,
  hint,
}: {
  value: string
  label: string
  disabled?: boolean
  hint?: string
}) {
  const id = `choice-${label}`
  return (
    <div className="flex items-center gap-2">
      <RadioGroupItem
        id={id}
        value={value}
        aria-label={label}
        disabled={disabled}
      />
      <Label
        htmlFor={id}
        className={disabled ? "text-muted-foreground" : undefined}
      >
        {label}
        {hint && (
          <span className="text-xs text-muted-foreground">（{hint}）</span>
        )}
      </Label>
    </div>
  )
}

/** 去掉空值并把表单字段整理成接口需要的规则定义。 */
function toPayload(def: Definition): Definition {
  const apply = def.apply_to
  const adjusts = def.action.type === "BUDGET" || def.action.type === "ROAS"
  return {
    ...def,
    accounts: {
      mode: def.accounts.mode,
      bc_ids: def.accounts.mode === "BC" ? def.accounts.bc_ids : [],
      advertiser_ids:
        def.accounts.mode === "ACCOUNTS" ? def.accounts.advertiser_ids : [],
    },
    apply_to: {
      ...apply,
      object_ids: apply.status === "SELECTED" ? apply.object_ids : [],
      name_contains: apply.name_contains?.trim() || null,
      name_excludes: apply.name_excludes?.trim() || null,
    },
    conditions: def.conditions.map((condition) => ({
      ...condition,
      values: condition.values.map((value) => value.trim()),
    })),
    action: adjusts
      ? def.action
      : { type: def.action.type, level: def.action.level },
    schedule: {
      ...def.schedule,
      daily_at:
        def.schedule.frequency === "DAILY" ? def.schedule.daily_at : null,
      periods: def.schedule.window === "CUSTOM" ? def.schedule.periods : [],
      once_at: def.schedule.window === "ONCE" ? def.schedule.once_at : null,
    },
  }
}

export function RuleEditorSheet({
  tenantId,
  catalog,
  target,
  admin,
  onClose,
}: {
  tenantId: string
  catalog: CatalogPublic
  target: EditorTarget
  admin: boolean
  onClose: () => void
}) {
  const client = useQueryClient()
  const initial = useMemo(
    () =>
      target.kind === "edit"
        ? {
            name: target.rule.name,
            definition: target.rule.definition as Definition,
          }
        : {
            name: target.name ?? defaultName(),
            definition: target.definition ?? emptyDefinition(),
          },
    [target],
  )
  const [name, setName] = useState(initial.name)
  const [def, setDef] = useState<Definition>(initial.definition)
  const [objectText, setObjectText] = useState(
    (initial.definition.apply_to.object_ids ?? []).join("\n"),
  )
  const [accountText, setAccountText] = useState(
    (initial.definition.accounts.advertiser_ids ?? []).join("\n"),
  )
  const [dramaText, setDramaText] = useState(
    (initial.definition.apply_to.drama_names ?? []).join("\n"),
  )
  const [dryRun, setDryRun] = useState<DryRunPublic | null>(null)
  const [error, setError] = useState<string | null>(null)
  const dirty =
    name !== initial.name ||
    JSON.stringify(def) !== JSON.stringify(initial.definition)
  const bcs = useQuery({
    queryKey: ["tenant", tenantId, "bcs", "automation"],
    queryFn: async ({ signal }) =>
      (await AccountsService.getBcs({ path: { tenant_id: tenantId }, signal }))
        .data!,
  })
  const settings = useQuery(settingsQuery(tenantId))
  const update = (patch: (draft: Definition) => Definition) => {
    setDryRun(null)
    setDef((current) => patch(structuredClone(current)))
  }
  const payload = () =>
    toPayload({
      ...def,
      accounts: { ...def.accounts, advertiser_ids: lines(accountText) },
      apply_to: {
        ...def.apply_to,
        object_ids: lines(objectText),
        // 短剧标题常含逗号，剧名只按行分隔。
        drama_names: dramaText
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
      },
    })
  const levels = actionLevels(
    def.aggregation,
    def.apply_to.level,
    def.action.type,
  )
  const minimum = FREQUENCY_ORDER.indexOf(
    minFrequency(def.apply_to.level, def.action.level),
  )
  const adjusts = def.action.type === "BUDGET" || def.action.type === "ROAS"
  // 校验失败时在顶部列出错误，不发请求。
  const valid = () => {
    const problems = validateDefinition(payload(), catalog)
    setError(problems.length ? problems.join("；") : null)
    return problems.length === 0
  }
  const save = useMutation({
    mutationFn: async () => {
      const body = { name: name.trim(), definition: payload(), enabled: true }
      if (target.kind === "edit")
        return AutomationRulesService.updateRule({
          path: { tenant_id: tenantId, rule_id: target.rule.id },
          body: {
            ...body,
            enabled: target.rule.enabled,
            expected_version: target.rule.version,
          },
        })
      return AutomationRulesService.createRule({
        path: { tenant_id: tenantId },
        body,
      })
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ruleKeys(tenantId) })
      onClose()
    },
    onError: (err) =>
      setError(
        (err as { response?: { status?: number } }).response?.status === 409 &&
          target.kind === "edit"
          ? "规则已被他人修改或名称重复，请刷新后重试"
          : errorMessage(err),
      ),
  })
  const runDry = useMutation({
    mutationFn: async () =>
      (
        await AutomationRulesService.dryRun({
          path: { tenant_id: tenantId },
          body: { definition: payload() },
        })
      ).data!,
    onSuccess: (data) => {
      setError(null)
      setDryRun(data)
    },
    onError: (err) => setError(errorMessage(err)),
  })
  const setPeriod = (index: number, patch: Partial<Period>) =>
    update((draft) => {
      const periods = [...(draft.schedule.periods ?? [])]
      periods[index] = { ...periods[index], ...patch }
      draft.schedule.periods = periods
      return draft
    })
  return (
    <ManagementSheet
      title={target.kind === "edit" ? "编辑规则" : "新建规则"}
      description="条件全部满足时触发；按执行模式仅通知、待人确认后执行或自动执行。"
      dirty={dirty}
      pending={save.isPending}
      wide
      onClose={onClose}
      actions={
        <>
          <Button
            variant="outline"
            onClick={() => valid() && runDry.mutate()}
            disabled={runDry.isPending}
          >
            试运行
          </Button>
          <Button
            onClick={() => valid() && save.mutate()}
            disabled={save.isPending || !name.trim()}
          >
            保存
          </Button>
        </>
      }
    >
      {error && (
        <Alert variant="destructive" className="my-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <Section title="适用账户">
        <RadioGroup
          aria-label="适用账户"
          className="flex flex-wrap gap-4"
          value={def.accounts.mode}
          onValueChange={(value) =>
            update((draft) => {
              draft.accounts.mode = value as Definition["accounts"]["mode"]
              return draft
            })
          }
        >
          <Choice value="ALL" label="全部账户" />
          <Choice value="BC" label="按 BC" />
          <Choice value="ACCOUNTS" label="指定账户" />
        </RadioGroup>
        {def.accounts.mode === "BC" && (
          <div className="flex flex-wrap gap-4">
            {(bcs.data?.items ?? []).map((bc) => (
              <div key={bc.bc_id} className="flex items-center gap-2">
                <Checkbox
                  id={`bc-${bc.bc_id}`}
                  checked={def.accounts.bc_ids?.includes(bc.bc_id)}
                  onCheckedChange={(checked) =>
                    update((draft) => {
                      const ids = new Set(draft.accounts.bc_ids ?? [])
                      if (checked) ids.add(bc.bc_id)
                      else ids.delete(bc.bc_id)
                      draft.accounts.bc_ids = [...ids]
                      return draft
                    })
                  }
                />
                <Label htmlFor={`bc-${bc.bc_id}`}>{bc.name || bc.bc_id}</Label>
              </div>
            ))}
          </div>
        )}
        {def.accounts.mode === "ACCOUNTS" && (
          <Textarea
            aria-label="账户 ID"
            placeholder="每行或用逗号分隔一个账户 ID"
            value={accountText}
            onChange={(e) => {
              setDryRun(null)
              setAccountText(e.target.value)
            }}
          />
        )}
        <p className="text-xs text-muted-foreground">
          「今天」「昨天」按每个账户自己的时区计算；选择全部账户或按 BC
          时，新接入的账户自动纳入。
        </p>
      </Section>
      <Section title="应用至">
        <div className="flex flex-wrap items-center gap-3">
          <Select
            value={def.apply_to.level}
            onValueChange={(value) =>
              update((draft) => {
                const level = value as Level
                draft.apply_to.level = level
                if (level === "material") draft.aggregation = "EACH"
                const allowed = actionLevels(
                  draft.aggregation,
                  level,
                  draft.action.type,
                )
                if (!allowed.includes(draft.action.level))
                  draft.action.level = allowed[0] ?? level
                return draft
              })
            }
          >
            <SelectTrigger aria-label="对象层级" className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LEVELS.map((level) => (
                <SelectItem key={level} value={level}>
                  {LEVEL_LABELS[level]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <RadioGroup
            aria-label="状态范围"
            className="flex flex-wrap gap-4"
            value={def.apply_to.status}
            onValueChange={(value) =>
              update((draft) => {
                draft.apply_to.status =
                  value as Definition["apply_to"]["status"]
                return draft
              })
            }
          >
            <Choice value="ACTIVE" label="投放中" />
            <Choice value="PAUSED" label="已暂停" />
            <Choice value="SELECTED" label="指定对象" />
          </RadioGroup>
        </div>
        {def.apply_to.status === "SELECTED" && (
          <Textarea
            aria-label="指定对象"
            placeholder="每行一个：账户ID:对象ID（素材为 账户ID:主素材ID）"
            value={objectText}
            onChange={(e) => {
              setDryRun(null)
              setObjectText(e.target.value)
            }}
          />
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <Textarea
            rows={2}
            aria-label="剧名"
            placeholder="剧名（版权方-剧名），每行一个"
            value={dramaText}
            onChange={(e) => {
              setDryRun(null)
              setDramaText(e.target.value)
            }}
          />
          <Input
            aria-label="名称包含"
            placeholder="名称包含"
            maxLength={200}
            value={def.apply_to.name_contains ?? ""}
            onChange={(e) =>
              update((draft) => {
                draft.apply_to.name_contains = e.target.value
                return draft
              })
            }
          />
          <Input
            aria-label="名称不包含"
            placeholder="名称不包含"
            maxLength={200}
            value={def.apply_to.name_excludes ?? ""}
            onChange={(e) =>
              update((draft) => {
                draft.apply_to.name_excludes = e.target.value
                return draft
              })
            }
          />
        </div>
      </Section>
      <Section title="判断对象">
        <RadioGroup
          aria-label="判断对象"
          className="flex flex-wrap gap-4"
          value={def.aggregation}
          onValueChange={(value) =>
            update((draft) => {
              draft.aggregation = value as Aggregation
              // 合计口径不能使用对象属性条件，换成第一个可用的数据指标。
              if (value !== "EACH")
                draft.conditions = draft.conditions.map((condition) =>
                  catalog.metrics.find((m) => m.key === condition.metric)
                    ?.kind === "attribute"
                    ? {
                        metric: "spend",
                        range: "TODAY",
                        operator: "GT",
                        values: [""],
                      }
                    : condition,
                )
              return draft
            })
          }
        >
          {(Object.keys(AGGREGATION_LABELS) as Aggregation[]).map((key) => (
            <Choice
              key={key}
              value={key}
              label={AGGREGATION_LABELS[key]}
              disabled={key !== "EACH" && def.apply_to.level === "material"}
            />
          ))}
        </RadioGroup>
      </Section>
      <Section title="条件">
        <ConditionRows
          catalog={catalog}
          level={def.apply_to.level}
          aggregation={def.aggregation}
          conditions={def.conditions}
          onChange={(conditions) =>
            update((draft) => {
              draft.conditions = conditions
              return draft
            })
          }
        />
      </Section>
      <Section title="执行操作">
        <div className="flex flex-wrap items-center gap-3">
          <Select
            value={def.action.type}
            onValueChange={(value) =>
              update((draft) => {
                const type = value as ActionType
                const allowed = actionLevels(
                  draft.aggregation,
                  draft.apply_to.level,
                  type,
                )
                draft.action = {
                  type,
                  level: allowed.includes(draft.action.level)
                    ? draft.action.level
                    : (allowed[0] ?? draft.action.level),
                  ...(type === "BUDGET" || type === "ROAS"
                    ? {
                        adjustment: {
                          mode: "INCREASE",
                          value_type: "PERCENT",
                          value: "",
                          limit: "",
                        },
                        frequency: { type: "EVERY_24H" },
                      }
                    : {}),
                }
                return draft
              })
            }
          >
            <SelectTrigger aria-label="操作" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(ACTION_LABELS) as ActionType[]).map((type) => (
                <SelectItem key={type} value={type}>
                  {ACTION_LABELS[type]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {levels.length > 0 ? (
            <Select
              value={def.action.level}
              onValueChange={(value) =>
                update((draft) => {
                  draft.action.level = value as Level
                  return draft
                })
              }
            >
              <SelectTrigger aria-label="执行层级" className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {levels.map((level) => (
                  <SelectItem key={level} value={level}>
                    {LEVEL_LABELS[level]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <span className="text-sm text-destructive">
              当前判断对象不支持该操作
            </span>
          )}
        </div>
        {adjusts && def.action.adjustment && (
          <div className="flex flex-wrap items-center gap-3">
            <Select
              value={def.action.adjustment.mode}
              onValueChange={(value) =>
                update((draft) => {
                  const adjustment = draft.action.adjustment!
                  adjustment.mode = value as "INCREASE" | "DECREASE" | "SET"
                  if (value === "SET") {
                    adjustment.value_type = "AMOUNT"
                    adjustment.limit = null
                  }
                  return draft
                })
              }
            >
              <SelectTrigger aria-label="调整方式" className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="INCREASE">提高</SelectItem>
                <SelectItem value="DECREASE">降低</SelectItem>
                <SelectItem value="SET">调整至</SelectItem>
              </SelectContent>
            </Select>
            {def.action.adjustment.mode !== "SET" && (
              <Select
                value={def.action.adjustment.value_type}
                onValueChange={(value) =>
                  update((draft) => {
                    draft.action.adjustment!.value_type = value as
                      | "AMOUNT"
                      | "PERCENT"
                    return draft
                  })
                }
              >
                <SelectTrigger aria-label="调整单位" className="w-24">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="PERCENT">百分比</SelectItem>
                  <SelectItem value="AMOUNT">金额</SelectItem>
                </SelectContent>
              </Select>
            )}
            <Input
              type="number"
              min={0}
              step="any"
              className="w-28"
              aria-label="调整数值"
              value={String(def.action.adjustment.value ?? "")}
              onChange={(e) =>
                update((draft) => {
                  draft.action.adjustment!.value = e.target.value
                  return draft
                })
              }
            />
            {def.action.adjustment.mode !== "SET" && (
              <Input
                type="number"
                min={0}
                step="any"
                className="w-32"
                aria-label={
                  def.action.adjustment.mode === "INCREASE" ? "上限" : "下限"
                }
                placeholder={
                  def.action.adjustment.mode === "INCREASE" ? "上限" : "下限"
                }
                value={String(def.action.adjustment.limit ?? "")}
                onChange={(e) =>
                  update((draft) => {
                    draft.action.adjustment!.limit = e.target.value
                    return draft
                  })
                }
              />
            )}
            <Select
              value={def.action.frequency?.type ?? "EVERY_24H"}
              onValueChange={(value) =>
                update((draft) => {
                  draft.action.frequency =
                    value === "CUSTOM"
                      ? { type: "CUSTOM", window: 24, unit: "HOUR", count: 1 }
                      : {
                          type: value as
                            | "ONCE"
                            | "EVERY_24H"
                            | "EVERY_48H"
                            | "EVERY_WEEK",
                        }
                  return draft
                })
              }
            >
              <SelectTrigger aria-label="调整频控" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ONCE">仅一次</SelectItem>
                <SelectItem value="EVERY_24H">每 24 小时一次</SelectItem>
                <SelectItem value="EVERY_48H">每 48 小时一次</SelectItem>
                <SelectItem value="EVERY_WEEK">每周一次</SelectItem>
                <SelectItem value="CUSTOM">自定义</SelectItem>
              </SelectContent>
            </Select>
            {def.action.frequency?.type === "CUSTOM" && (
              <div className="flex items-center gap-2 text-sm">
                <Input
                  type="number"
                  min={1}
                  className="w-20"
                  aria-label="频控时间"
                  value={def.action.frequency.window ?? ""}
                  onChange={(e) =>
                    update((draft) => {
                      draft.action.frequency!.window = Number(e.target.value)
                      return draft
                    })
                  }
                />
                <Select
                  value={def.action.frequency.unit ?? "HOUR"}
                  onValueChange={(value) =>
                    update((draft) => {
                      draft.action.frequency!.unit = value as
                        | "MINUTE"
                        | "HOUR"
                        | "DAY"
                      return draft
                    })
                  }
                >
                  <SelectTrigger aria-label="频控单位" className="w-24">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="MINUTE">分钟</SelectItem>
                    <SelectItem value="HOUR">小时</SelectItem>
                    <SelectItem value="DAY">天</SelectItem>
                  </SelectContent>
                </Select>
                内最多
                <Input
                  type="number"
                  min={1}
                  className="w-20"
                  aria-label="频控次数"
                  value={def.action.frequency.count ?? ""}
                  onChange={(e) =>
                    update((draft) => {
                      draft.action.frequency!.count = Number(e.target.value)
                      return draft
                    })
                  }
                />
                次
              </div>
            )}
          </div>
        )}
        <RadioGroup
          aria-label="执行模式"
          className="flex flex-wrap gap-4"
          value={def.mode}
          onValueChange={(value) =>
            update((draft) => {
              draft.mode = value as Definition["mode"]
              return draft
            })
          }
        >
          <Choice
            value="AUTO"
            label="自动执行"
            disabled={!admin}
            hint={admin ? undefined : "需要租户管理员"}
          />
          <Choice value="CONFIRM" label="确认后执行" />
          <Choice value="NOTIFY" label="仅通知" />
        </RadioGroup>
        <div className="flex items-center gap-2 text-sm">
          <Label htmlFor="max-targets">单次执行对象上限</Label>
          <Input
            id="max-targets"
            type="number"
            min={1}
            max={5000}
            className="w-28"
            value={def.max_targets ?? 200}
            onChange={(e) =>
              update((draft) => {
                draft.max_targets = Number(e.target.value)
                return draft
              })
            }
          />
        </div>
      </Section>
      <Section title="运行排期">
        <div className="flex flex-wrap items-center gap-3">
          <Select
            value={def.schedule.frequency}
            onValueChange={(value) =>
              update((draft) => {
                draft.schedule.frequency = value as Frequency
                draft.schedule.daily_at = value === "DAILY" ? "09:00" : null
                return draft
              })
            }
          >
            <SelectTrigger aria-label="运行频率" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {FREQUENCY_ORDER.map((frequency, index) => (
                <SelectItem
                  key={frequency}
                  value={frequency}
                  disabled={index < minimum}
                >
                  {FREQUENCY_LABELS[frequency]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {def.schedule.frequency === "DAILY" && (
            <Input
              type="time"
              aria-label="每日运行时刻"
              className="w-32"
              value={def.schedule.daily_at ?? "09:00"}
              onChange={(e) =>
                update((draft) => {
                  draft.schedule.daily_at = e.target.value
                  return draft
                })
              }
            />
          )}
        </div>
        <RadioGroup
          aria-label="生效时段"
          className="flex flex-wrap gap-4"
          value={def.schedule.window}
          onValueChange={(value) =>
            update((draft) => {
              draft.schedule.window = value as Definition["schedule"]["window"]
              if (value === "CUSTOM" && !draft.schedule.periods?.length)
                draft.schedule.periods = [
                  { date_type: "DAY", days: [], start: "09:00", end: "21:00" },
                ]
              return draft
            })
          }
        >
          <Choice value="CONTINUOUS" label="持续" />
          <Choice value="CUSTOM" label="自定义时段" />
          <Choice value="ONCE" label="仅一次" />
        </RadioGroup>
        {def.schedule.window === "CUSTOM" && (
          <div className="flex flex-col gap-2">
            {(def.schedule.periods ?? []).map((period, index) => (
              <div
                key={index}
                className="flex flex-wrap items-center gap-2 rounded-md border p-2"
              >
                <Select
                  value={period.date_type}
                  onValueChange={(value) =>
                    setPeriod(index, {
                      date_type: value as Period["date_type"],
                      days: [],
                    })
                  }
                >
                  <SelectTrigger
                    aria-label={`时间段 ${index + 1} 频次`}
                    className="w-24"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="DAY">按天</SelectItem>
                    <SelectItem value="WEEK">按周</SelectItem>
                    <SelectItem value="MONTH">按月</SelectItem>
                  </SelectContent>
                </Select>
                {period.date_type === "WEEK" &&
                  WEEKDAYS.map((label, day) => (
                    <div key={label} className="flex items-center gap-1">
                      <Checkbox
                        id={`period-${index}-w${day}`}
                        checked={period.days?.includes(day + 1)}
                        onCheckedChange={(checked) =>
                          setPeriod(index, {
                            days: checked
                              ? [...(period.days ?? []), day + 1].sort()
                              : (period.days ?? []).filter(
                                  (d) => d !== day + 1,
                                ),
                          })
                        }
                      />
                      <Label htmlFor={`period-${index}-w${day}`}>
                        周{label}
                      </Label>
                    </div>
                  ))}
                {period.date_type === "MONTH" && (
                  <MonthDaysInput
                    label={`时间段 ${index + 1} 日期`}
                    days={period.days ?? []}
                    onChange={(days) => setPeriod(index, { days })}
                  />
                )}
                <Input
                  type="time"
                  aria-label={`时间段 ${index + 1} 首次运行时间`}
                  className="w-28"
                  value={period.start}
                  onChange={(e) => setPeriod(index, { start: e.target.value })}
                />
                <span className="text-sm">至</span>
                <Input
                  type="time"
                  aria-label={`时间段 ${index + 1} 末次运行时间`}
                  className="w-28"
                  value={period.end}
                  onChange={(e) => setPeriod(index, { end: e.target.value })}
                />
                {(def.schedule.periods ?? []).length > 1 && (
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`删除时间段 ${index + 1}`}
                    onClick={() =>
                      update((draft) => {
                        draft.schedule.periods = (
                          draft.schedule.periods ?? []
                        ).filter((_, i) => i !== index)
                        return draft
                      })
                    }
                  >
                    <Trash2 />
                  </Button>
                )}
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="self-start"
              disabled={(def.schedule.periods ?? []).length >= 7}
              onClick={() =>
                update((draft) => {
                  draft.schedule.periods = [
                    ...(draft.schedule.periods ?? []),
                    {
                      date_type: "DAY",
                      days: [],
                      start: "09:00",
                      end: "21:00",
                    },
                  ]
                  return draft
                })
              }
            >
              <Plus data-icon="inline-start" />
              添加时间段 ({(def.schedule.periods ?? []).length}/7)
            </Button>
            <p className="text-xs text-muted-foreground">
              时间段按通知设置中的运行时区解释。
            </p>
          </div>
        )}
        {def.schedule.window === "ONCE" && (
          <Input
            type="datetime-local"
            aria-label="运行时间"
            className="w-56"
            value={
              def.schedule.once_at
                ? new Date(
                    new Date(def.schedule.once_at).getTime() -
                      new Date().getTimezoneOffset() * 60000,
                  )
                    .toISOString()
                    .slice(0, 16)
                : ""
            }
            onChange={(e) =>
              update((draft) => {
                draft.schedule.once_at = e.target.value
                  ? new Date(e.target.value).toISOString()
                  : null
                return draft
              })
            }
          />
        )}
      </Section>
      <Section title="飞书通知">
        <div className="flex flex-wrap gap-6">
          <div className="flex items-center gap-2">
            <Checkbox
              id="notify-enabled"
              checked={def.notification?.enabled ?? true}
              onCheckedChange={(checked) =>
                update((draft) => {
                  draft.notification = {
                    ...draft.notification,
                    enabled: !!checked,
                  }
                  return draft
                })
              }
            />
            <Label htmlFor="notify-enabled">命中时发送飞书卡片</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="notify-empty"
              checked={def.notification?.notify_without_hits ?? false}
              onCheckedChange={(checked) =>
                update((draft) => {
                  draft.notification = {
                    ...draft.notification,
                    notify_without_hits: !!checked,
                  }
                  return draft
                })
              }
            />
            <Label htmlFor="notify-empty">没有命中时也发送</Label>
          </div>
        </div>
        {settings.data && !settings.data.feishu_enabled && (
          <p className="text-sm text-muted-foreground">
            租户尚未启用飞书通知，
            <Link
              to="/tenants/$tenantId/notifications"
              params={{ tenantId }}
              className="underline"
            >
              去设置
            </Link>
          </p>
        )}
      </Section>
      <Section title="规则名称">
        <Input
          aria-label="规则名称"
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </Section>
      {dryRun && (
        <div className="border-t py-4">
          <DryRunPanel result={dryRun} catalog={catalog} />
        </div>
      )}
    </ManagementSheet>
  )
}
