import { useId, useState } from "react"
import type { AudienceTargeting } from "@/client"
import { Badge } from "@/components/ui/badge"
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
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export type Targeting = Required<AudienceTargeting>
export const defaultTargeting: Targeting = {
  region_mode: "ALL_AVAILABLE",
  region_codes: [],
  languages: [],
  age_groups: [],
  gender: "GENDER_UNLIMITED",
}
export const normalizedTargeting = (
  value?: AudienceTargeting | null,
): Targeting => ({ ...defaultTargeting, ...value })
const regions = new Intl.DisplayNames(["zh"], { type: "region" })
const languages = new Intl.DisplayNames(["zh"], { type: "language" })
export const countryName = (code: string) => regions.of(code) || code
const languageName = (code: string) => languages.of(code) || code
const ageLabels = {
  AGE_18_24: "18–24 岁",
  AGE_25_34: "25–34 岁",
  AGE_35_44: "35–44 岁",
  AGE_45_54: "45–54 岁",
  AGE_55_100: "55 岁以上",
}
const genderLabels = {
  GENDER_UNLIMITED: "不主动限制",
  GENDER_MALE: "男性",
  GENDER_FEMALE: "女性",
}
// 官方 Language Code 枚举（2026-09-28）；国家选择不使用这种静态枚举。
const languageCodes: Targeting["languages"] = [
  "ar",
  "as",
  "bgc",
  "bh",
  "bn",
  "cs",
  "de",
  "el",
  "en",
  "es",
  "fi",
  "fr",
  "gu",
  "he",
  "hi",
  "hu",
  "id",
  "it",
  "ja",
  "kn",
  "ko",
  "ml",
  "mr",
  "ms",
  "nl",
  "or",
  "pa",
  "pl",
  "pt",
  "raj",
  "ro",
  "ru",
  "sv",
  "ta",
  "te",
  "th",
  "tr",
  "uk",
  "vi",
  "zh",
  "zh-Hant",
]
export function targetingError(value: Targeting) {
  return value.region_mode === "SELECTED" && !value.region_codes.length
    ? "请至少选择一个国家。"
    : undefined
}
export function TargetingSummary({
  value,
  countries,
}: {
  value?: AudienceTargeting | null
  countries?: string[]
}) {
  const v = normalizedTargeting(value)
  const selected = v.region_mode === "SELECTED" ? v.region_codes : countries
  return (
    <p className="text-sm text-muted-foreground">
      国家：
      {selected
        ? selected.map(countryName).join("、") || "待核实"
        : "全部共同可投地区"}{" "}
      · 语言：{v.languages.map(languageName).join("、") || "不主动限制"} ·
      年龄：{v.age_groups.map((x) => ageLabels[x]).join("、") || "不主动限制"} ·
      性别：{genderLabels[v.gender]}
    </p>
  )
}
function Choices({
  label,
  values,
  options,
  disabled,
  onChange,
}: {
  label: string
  values: string[]
  options: { code: string; label: string; unavailable?: boolean }[]
  disabled?: boolean
  onChange: (values: string[]) => void
}) {
  const id = useId()
  const [search, setSearch] = useState("")
  const visible = options.filter((o) =>
    `${o.label} ${o.code}`
      .toLocaleLowerCase()
      .includes(search.toLocaleLowerCase()),
  )
  return (
    <FieldSet>
      <FieldLegend>{label}</FieldLegend>
      {options.length > 8 && (
        <Input
          aria-label={`搜索${label}`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          disabled={disabled}
          placeholder={`搜索${label}`}
        />
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !options.some((o) => !o.unavailable)}
          onClick={() =>
            onChange(
              [
                ...new Set([
                  ...values,
                  ...options.filter((o) => !o.unavailable).map((o) => o.code),
                ]),
              ].sort(),
            )
          }
        >
          全选{label}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled || !values.length}
          onClick={() => onChange([])}
        >
          清空{label}
        </Button>
      </div>
      <FieldGroup className="max-h-48 overflow-y-auto sm:grid sm:grid-cols-2">
        {visible.map((o) => (
          <Field
            key={o.code}
            orientation="horizontal"
            data-invalid={o.unavailable && values.includes(o.code)}
          >
            <Checkbox
              id={`${id}-${o.code}`}
              checked={values.includes(o.code)}
              disabled={disabled || (o.unavailable && !values.includes(o.code))}
              onCheckedChange={(checked) =>
                onChange(
                  checked
                    ? [...new Set([...values, o.code])].sort()
                    : values.filter((x) => x !== o.code),
                )
              }
            />
            <FieldLabel htmlFor={`${id}-${o.code}`}>
              {o.label}
              {o.unavailable && (
                <Badge variant="destructive">不可用/待核实</Badge>
              )}
            </FieldLabel>
          </Field>
        ))}
      </FieldGroup>
      {!visible.length && <FieldDescription>没有匹配的选项。</FieldDescription>}
    </FieldSet>
  )
}
export function TargetingForm({
  value,
  onChange,
  countries,
  disabled,
  reference = false,
}: {
  value: Targeting
  onChange: (value: Targeting) => void
  countries: string[]
  disabled?: boolean
  reference?: boolean
}) {
  const id = useId()
  const update = (patch: Partial<Targeting>) => onChange({ ...value, ...patch })
  const options = [...new Set([...countries, ...value.region_codes])]
    .sort()
    .map((code) => ({
      code,
      label: countryName(code),
      unavailable: !countries.includes(code),
    }))
  return (
    <FieldGroup>
      <Field>
        <FieldLabel htmlFor={`${id}-regions`}>国家范围</FieldLabel>
        <Select
          value={value.region_mode}
          disabled={disabled}
          onValueChange={(mode) =>
            update({
              region_mode: mode as Targeting["region_mode"],
              region_codes: [],
            })
          }
        >
          <SelectTrigger id={`${id}-regions`} aria-label="国家范围">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="ALL_AVAILABLE">全部共同可投地区</SelectItem>
              <SelectItem value="SELECTED">指定国家</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
        <FieldDescription>
          {reference
            ? "参考国家来自当前 BC 已核实的场景；实际投放将在搭建时逐账户核验。"
            : "仅可选择所有所选账户与当前小程序共同支持的国家。"}
        </FieldDescription>
      </Field>
      {value.region_mode === "SELECTED" && (
        <Choices
          label="国家"
          values={value.region_codes}
          options={options}
          disabled={disabled}
          onChange={(region_codes) => update({ region_codes })}
        />
      )}
      {targetingError(value) && (
        <p role="alert" className="text-sm text-destructive">
          {targetingError(value)}
        </p>
      )}
      <Choices
        label="语言"
        values={value.languages}
        options={languageCodes.map((code) => ({
          code,
          label: languageName(code),
        }))}
        disabled={disabled}
        onChange={(v) => update({ languages: v as Targeting["languages"] })}
      />
      <Choices
        label="年龄"
        values={value.age_groups}
        options={Object.entries(ageLabels).map(([code, label]) => ({
          code,
          label,
        }))}
        disabled={disabled}
        onChange={(v) => update({ age_groups: v as Targeting["age_groups"] })}
      />
      <FieldDescription>
        语言、年龄不选择表示不主动限制；平台自身限制仍适用。当前小程序投放不支持
        13–17 岁。
      </FieldDescription>
      <Field>
        <FieldLabel htmlFor={`${id}-gender`}>性别</FieldLabel>
        <Select
          value={value.gender}
          disabled={disabled}
          onValueChange={(v) => update({ gender: v as Targeting["gender"] })}
        >
          <SelectTrigger id={`${id}-gender`} aria-label="性别">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {Object.entries(genderLabels).map(([code, label]) => (
                <SelectItem key={code} value={code}>
                  {label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
      <FieldDescription>
        已选条件按严格限制提交，不作为允许平台扩大范围的受众建议。
      </FieldDescription>
    </FieldGroup>
  )
}
