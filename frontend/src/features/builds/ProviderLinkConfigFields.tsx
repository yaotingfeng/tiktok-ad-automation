import type { LinkConfigSchema } from "@/client"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export type LinkConfigValues = Record<string, number>

// defaults：推广应用默认值，只显示可设默认的字段；overrides：本次单独调整，留空即沿用下层值。
export function ProviderLinkConfigFields({
  schema,
  value,
  mode,
  disabled,
  onChange,
}: {
  schema: LinkConfigSchema
  value: LinkConfigValues
  mode: "defaults" | "overrides"
  disabled: boolean
  onChange: (next: LinkConfigValues) => void
}) {
  const fields = schema.fields.filter(
    (field) => mode === "overrides" || field.source === "application_default",
  )
  if (!fields.length) return null
  const update = (name: string, raw: string) => {
    const next = { ...value }
    // 清空即删除该键，不提交 null，后端按“未调整”处理。
    if (raw === "") delete next[name]
    else next[name] = Number(raw)
    onChange(next)
  }
  return (
    <FieldGroup className="grid min-w-0 gap-4 md:grid-cols-2">
      {fields.map((field) => {
        const current = value[field.name]
        const placeholder =
          field.source === "per_drama"
            ? (field.rule ?? "")
            : String(
                schema.application_defaults?.[field.name] ??
                  schema.defaults?.[field.name] ??
                  "",
              )
        return (
          <Field key={field.name}>
            <FieldLabel htmlFor={`link-config-${field.name}`}>
              {field.label}
            </FieldLabel>
            {field.type === "select" ? (
              <Select
                value={current === undefined ? "" : String(current)}
                disabled={disabled || !field.options?.length}
                onValueChange={(raw) => update(field.name, raw)}
              >
                <SelectTrigger
                  id={`link-config-${field.name}`}
                  aria-label={field.label}
                >
                  <SelectValue
                    placeholder={
                      field.options?.length
                        ? "请选择"
                        : "暂无可选项，请重新验证连接"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {field.options?.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={`link-config-${field.name}`}
                type="number"
                min={field.minimum ?? 1}
                value={current === undefined ? "" : String(current)}
                placeholder={placeholder}
                disabled={disabled}
                onChange={(event) =>
                  update(field.name, event.currentTarget.value)
                }
              />
            )}
            {field.rule && mode === "overrides" && (
              <FieldDescription>留空则{field.rule}</FieldDescription>
            )}
          </Field>
        )
      })}
    </FieldGroup>
  )
}
