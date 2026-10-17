import { Plus, Trash2 } from "lucide-react"
import type { CatalogPublic, Condition } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  type Aggregation,
  type Level,
  metricOptions,
  OPERATOR_LABELS,
  type Operator,
  RANGE_LABELS,
  type RangeType,
} from "./ruleForm"

const GROUP_LABELS: Record<string, string> = {
  delivery: "投放数据",
  revenue: "收益数据",
  attribute: "对象属性",
}
const UNIT_SUFFIX: Record<string, string> = {
  money: "账户币种",
  ratio: "%",
  multiple: "倍",
  day: "天",
}
const MAX_CONDITIONS = 5

export function ConditionRows({
  catalog,
  level,
  aggregation,
  conditions,
  disabled,
  onChange,
}: {
  catalog: CatalogPublic
  level: Level
  aggregation: Aggregation
  conditions: Condition[]
  disabled?: boolean
  onChange: (conditions: Condition[]) => void
}) {
  const options = metricOptions(catalog, level, aggregation)
  const groups = Object.keys(GROUP_LABELS).filter((group) =>
    options.some((metric) => metric.group === group),
  )
  const replace = (index: number, next: Condition) =>
    onChange(conditions.map((item, i) => (i === index ? next : item)))
  // 切换指标时重置为该指标第一个可用的运算符与时间周期，避免组合非法。
  const pickMetric = (index: number, key: string) => {
    const metric = catalog.metrics.find((item) => item.key === key)
    if (!metric) return
    replace(index, {
      metric: key,
      range: metric.ranged ? "TODAY" : null,
      operator: metric.operators[0] as Operator,
      values: [metric.options?.[0]?.value ?? ""],
    })
  }
  return (
    <div className="flex flex-col gap-3">
      {conditions.map((condition, index) => {
        const n = index + 1
        const metric = catalog.metrics.find(
          (item) => item.key === condition.metric,
        )
        const textual = metric?.unit === "text"
        return (
          <div
            key={index}
            className="flex flex-wrap items-center gap-2 rounded-md border p-2"
          >
            <Select
              value={condition.metric}
              onValueChange={(value) => pickMetric(index, value)}
              disabled={disabled}
            >
              <SelectTrigger aria-label={`条件 ${n} 指标`} className="w-48">
                <SelectValue placeholder="选择指标" />
              </SelectTrigger>
              <SelectContent>
                {groups.map((group) => (
                  <SelectGroup key={group}>
                    <SelectLabel>{GROUP_LABELS[group]}</SelectLabel>
                    {options
                      .filter((item) => item.group === group)
                      .map((item) => (
                        <SelectItem key={item.key} value={item.key}>
                          {item.name_zh}
                        </SelectItem>
                      ))}
                  </SelectGroup>
                ))}
              </SelectContent>
            </Select>
            {metric?.ranged && (
              <Select
                value={condition.range ?? "TODAY"}
                onValueChange={(value) =>
                  replace(index, { ...condition, range: value as RangeType })
                }
                disabled={disabled}
              >
                <SelectTrigger
                  aria-label={`条件 ${n} 时间周期`}
                  className="w-32"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(RANGE_LABELS) as RangeType[]).map((range) => (
                    <SelectItem key={range} value={range}>
                      {RANGE_LABELS[range]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <Select
              value={condition.operator}
              onValueChange={(value) =>
                replace(index, {
                  ...condition,
                  operator: value as Operator,
                  values:
                    value === "BETWEEN"
                      ? ["", ""]
                      : [condition.values[0] ?? ""],
                })
              }
              disabled={disabled}
            >
              <SelectTrigger aria-label={`条件 ${n} 运算符`} className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(metric?.operators ?? []).map((operator) => (
                  <SelectItem key={operator} value={operator}>
                    {metric?.options?.length && operator === "EQ"
                      ? "是"
                      : OPERATOR_LABELS[operator as Operator]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {metric?.options?.length ? (
              <Select
                value={condition.values[0] ?? ""}
                onValueChange={(value) =>
                  replace(index, { ...condition, values: [value] })
                }
                disabled={disabled}
              >
                <SelectTrigger aria-label={`条件 ${n} 状态`} className="w-32">
                  <SelectValue placeholder="选择状态" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {metric.options.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            ) : condition.operator === "BETWEEN" ? (
              <>
                <Input
                  type="number"
                  min={0}
                  step="any"
                  className="w-28"
                  aria-label={`条件 ${n} 下限`}
                  value={condition.values[0] ?? ""}
                  disabled={disabled}
                  onChange={(e) =>
                    replace(index, {
                      ...condition,
                      values: [e.target.value, condition.values[1] ?? ""],
                    })
                  }
                />
                <span className="text-sm text-muted-foreground">~</span>
                <Input
                  type="number"
                  min={0}
                  step="any"
                  className="w-28"
                  aria-label={`条件 ${n} 上限`}
                  value={condition.values[1] ?? ""}
                  disabled={disabled}
                  onChange={(e) =>
                    replace(index, {
                      ...condition,
                      values: [condition.values[0] ?? "", e.target.value],
                    })
                  }
                />
              </>
            ) : (
              <Input
                type={textual ? "text" : "number"}
                min={textual ? undefined : 0}
                step={textual ? undefined : "any"}
                maxLength={textual ? 512 : undefined}
                className={textual ? "w-56" : "w-32"}
                aria-label={`条件 ${n} 数值`}
                value={condition.values[0] ?? ""}
                disabled={disabled}
                onChange={(e) =>
                  replace(index, { ...condition, values: [e.target.value] })
                }
              />
            )}
            {metric && UNIT_SUFFIX[metric.unit] && (
              <span className="text-sm text-muted-foreground">
                {UNIT_SUFFIX[metric.unit]}
              </span>
            )}
            {conditions.length > 1 && !disabled && (
              <Button
                variant="ghost"
                size="icon"
                aria-label={`删除条件 ${n}`}
                onClick={() =>
                  onChange(conditions.filter((_, i) => i !== index))
                }
              >
                <Trash2 />
              </Button>
            )}
          </div>
        )
      })}
      <div className="flex items-center gap-3">
        <Button
          variant="outline"
          size="sm"
          aria-label="添加条件"
          disabled={disabled || conditions.length >= MAX_CONDITIONS}
          onClick={() =>
            onChange([
              ...conditions,
              { metric: "spend", range: "TODAY", operator: "GT", values: [""] },
            ])
          }
        >
          <Plus data-icon="inline-start" />
          添加条件 ({conditions.length}/{MAX_CONDITIONS})
        </Button>
        <span className="text-sm text-muted-foreground">全部满足才触发</span>
      </div>
    </div>
  )
}
