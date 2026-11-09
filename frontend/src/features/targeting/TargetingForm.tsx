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
// 策略仍保留历史年龄集合；页面仅提供与Minis最低年龄完整等价的选择。
const over25: Targeting["age_groups"] = [
  "AGE_25_34",
  "AGE_35_44",
  "AGE_45_54",
  "AGE_55_100",
]
export function minimumAge(value: Targeting): "18" | "25" | "LEGACY" {
  const ages = new Set(value.age_groups)
  if (
    !ages.size ||
    (ages.size === 5 &&
      Object.keys(ageLabels).every((a) =>
        ages.has(a as Targeting["age_groups"][number]),
      ))
  )
    return "18"
  if (ages.size === 4 && over25.every((a) => ages.has(a))) return "25"
  return "LEGACY"
}
// 官方 Language Code 枚举（2026-09-28）。
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
  if (value.region_mode === "SELECTED" && !value.region_codes.length)
    return "请至少选择一个国家。"
  if (minimumAge(value) === "LEGACY")
    return "旧年龄条件不适用于小程序自动定向，请重新选择最低年龄。"
  if (value.gender !== "GENDER_UNLIMITED")
    return "小程序自动定向不支持严格性别限制，请改为不限性别。"
  return undefined
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
      最低年龄：
      {minimumAge(v) === "LEGACY"
        ? `旧年龄条件（${v.age_groups.map((x) => ageLabels[x]).join("、")}，需重新选择）`
        : `${minimumAge(v)} 岁以上`}{" "}
      · 性别：{genderLabels[v.gender]}
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
            ? "可提前选择 TikTok Ads 国家/地区，无需先搭建；实际可投范围将在搭建时按账户和小程序核验。"
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
      <Field data-invalid={minimumAge(value) === "LEGACY"}>
        <FieldLabel htmlFor={`${id}-minimum-age`}>最低年龄</FieldLabel>
        <Select
          value={minimumAge(value)}
          disabled={disabled}
          onValueChange={(v) =>
            update({ age_groups: v === "25" ? [...over25] : [] })
          }
        >
          <SelectTrigger
            id={`${id}-minimum-age`}
            aria-label="最低年龄"
            aria-invalid={minimumAge(value) === "LEGACY"}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {minimumAge(value) === "LEGACY" && (
                <SelectItem value="LEGACY" disabled>
                  旧年龄条件，需重新选择
                </SelectItem>
              )}
              <SelectItem value="18">18 岁以上</SelectItem>
              <SelectItem value="25">25 岁以上</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
        <FieldDescription>
          小程序使用自动定向，支持最低年龄控制；语言不选择表示不限。
        </FieldDescription>
      </Field>
      {value.gender !== "GENDER_UNLIMITED" ? (
        <Field>
          <FieldLabel>旧性别条件：{genderLabels[value.gender]}</FieldLabel>
          <FieldDescription>
            当前模式不支持严格性别限制，需明确改为不限性别后保存。
          </FieldDescription>
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            onClick={() => update({ gender: "GENDER_UNLIMITED" })}
          >
            改为不限性别
          </Button>
        </Field>
      ) : (
        <FieldDescription>
          性别不限；国家、语言和最低年龄按自动定向受众控制提交。
        </FieldDescription>
      )}
    </FieldGroup>
  )
}
