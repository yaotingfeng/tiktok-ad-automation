import { useState, useId } from "react"
import type { ImageRule } from "@/client"
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldDescription,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { MusicPicker } from "./MusicPicker"
export function RuleChoice({
  label,
  value,
  choices,
  onChange,
  disabled,
}: {
  label: string
  value: string
  choices: Record<string, string>
  onChange: (v: string) => void
  disabled?: boolean
}) {
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <ToggleGroup
        type="single"
        variant="outline"
        aria-label={label}
        value={value}
        disabled={disabled}
        onValueChange={(v) => {
          if (v) onChange(v)
        }}
      >
        {Object.entries(choices).map(([key, text]) => (
          <ToggleGroupItem key={key} value={key}>
            {text}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    </Field>
  )
}
export function ImageRuleFields({
  tenantId,
  draftId,
  value,
  onChange,
  disabled = false,
}: {
  tenantId: string
  draftId: string
  value: ImageRule
  onChange: (v: ImageRule) => void
  disabled?: boolean
}) {
  const countId = useId()
  const [pick, setPick] = useState<"fixed_music_id" | "single_music_id" | null>(
      null,
    ),
    [names, setNames] = useState<Record<string, string>>({})
  return (
    <>
      <FieldGroup className="grid gap-4 md:grid-cols-2">
        <RuleChoice
          label="图片组合"
          value={value.mode!}
          disabled={disabled}
          choices={{ SINGLE: "单图", SEQUENTIAL: "顺序轮播" }}
          onChange={(v) =>
            onChange({
              ...value,
              mode: v as ImageRule["mode"],
              images_per_unit: v === "SINGLE" ? 1 : 3,
            })
          }
        />
        {value.mode !== "SINGLE" && (
          <Field>
            <FieldLabel htmlFor={countId}>每轮播张数</FieldLabel>
            <Input
              id={countId}
              type="number"
              min={2}
              max={35}
              value={value.images_per_unit}
              disabled={disabled}
              onChange={(e) =>
                onChange({ ...value, images_per_unit: Number(e.target.value) })
              }
            />
          </Field>
        )}
        <RuleChoice
          label="图片排序"
          value={value.order!}
          disabled={disabled}
          choices={{ NATURAL_NAME: "自然文件名", UPLOAD_SEQUENCE: "上传顺序" }}
          onChange={(v) =>
            onChange({ ...value, order: v as ImageRule["order"] })
          }
        />
        <RuleChoice
          label="尾部不足张数"
          value={value.remainder!}
          disabled={disabled}
          choices={{ KEEP: "保留", EXCLUDE: "本次不使用" }}
          onChange={(v) =>
            onChange({ ...value, remainder: v as ImageRule["remainder"] })
          }
        />
        <RuleChoice
          label="配乐方式"
          value={value.music_mode!}
          disabled={disabled}
          choices={{ RECOMMEND: "按轮播推荐", FIXED: "整批指定一首" }}
          onChange={(v) =>
            onChange({ ...value, music_mode: v as ImageRule["music_mode"] })
          }
        />
        <Field>
          <FieldLabel>
            {value.music_mode === "FIXED" ? "整批用曲" : "单图用曲"}
          </FieldLabel>
          <Button
            variant="outline"
            disabled={disabled}
            onClick={() =>
              setPick(
                value.music_mode === "FIXED"
                  ? "fixed_music_id"
                  : "single_music_id",
              )
            }
          >
            {names[
              value.music_mode === "FIXED"
                ? "fixed_music_id"
                : "single_music_id"
            ] ||
              (value[
                value.music_mode === "FIXED"
                  ? "fixed_music_id"
                  : "single_music_id"
              ]
                ? "已指定音乐 · 更换"
                : "选择音乐")}
          </Button>
          <FieldDescription>
            单图不能自动推荐；实际有单图时需要选曲。
          </FieldDescription>
        </Field>
      </FieldGroup>
      {pick && (
        <MusicPicker
          tenantId={tenantId}
          draftId={draftId}
          onClose={() => setPick(null)}
          onSelect={(song) => {
            onChange({ ...value, [pick]: song.music_id })
            setNames({ ...names, [pick]: song.display_name })
          }}
        />
      )}
    </>
  )
}
