import type { LinkConfigSchema } from "@/client"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

type LinkConfigValue = string | number | boolean | null

export function ProviderLinkConfigFields({
  schema,
  value,
  disabled,
  onChange,
}: {
  schema: LinkConfigSchema
  value: Record<string, LinkConfigValue>
  disabled: boolean
  onChange: (patch: Record<string, LinkConfigValue>) => void
}) {
  if (!schema.fields.length) return null
  return (
    <FieldGroup className="grid min-w-0 gap-4 md:grid-cols-2">
      {schema.fields.map((field) => {
        const current = value[field.name] ?? schema.defaults?.[field.name] ?? ""
        const required = field.required || Object.entries(schema.required_when || {}).some(
          ([fieldName, rules]) =>
            fieldName === field.name &&
            Object.entries(rules).some(([dependency, allowed]) =>
              allowed.includes(String(value[dependency] ?? "")),
            ),
        )
        const label = `${field.label}${required ? "（必填）" : "（选填）"}`
        const update = (raw: string | boolean) => {
          let next: LinkConfigValue = raw
          if (field.type === "integer") next = raw === "" ? null : Number(raw)
          if (field.type === "boolean") next = Boolean(raw)
          onChange({ [field.name]: next })
        }
        return (
          <Field key={field.name}>
            <FieldLabel htmlFor={`link-config-${field.name}`}>{label}</FieldLabel>
            {field.type === "select" && (field.options?.length || 0) > 0 ? (
              <Select
                value={String(current)}
                disabled={disabled}
                onValueChange={update}
              >
                <SelectTrigger id={`link-config-${field.name}`}>
                  <SelectValue placeholder="请选择" />
                </SelectTrigger>
                <SelectContent>
                  {field.options?.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={`link-config-${field.name}`}
                type={field.type === "integer" ? "number" : field.type === "boolean" ? "checkbox" : "text"}
                value={field.type === "boolean" ? undefined : String(current ?? "")}
                checked={field.type === "boolean" ? Boolean(current) : undefined}
                required={required}
                disabled={disabled}
                onChange={(event) =>
                  update(
                    field.type === "boolean"
                      ? event.currentTarget.checked
                      : event.currentTarget.value,
                  )
                }
              />
            )}
            {field.name === "payment_template_id" && (
              <FieldDescription>付费或混合剧目必须选择版权方支付模板。</FieldDescription>
            )}
          </Field>
        )
      })}
    </FieldGroup>
  )
}
