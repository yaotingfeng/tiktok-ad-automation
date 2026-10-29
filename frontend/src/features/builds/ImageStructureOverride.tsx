import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import type { DraftSummary, StructureOverride } from "@/client"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { versionQuery } from "@/features/strategies/queries"
import { RequestError } from "@/features/tenants/shared"
import { change, imageApi, imagePath } from "./imageCompositionApi"
import { RuleChoice } from "./ImageRuleFields"
export function ImageStructureOverride({
  tenantId,
  summary,
  disabled,
  onSaved,
}: {
  tenantId: string
  summary: DraftSummary
  disabled: boolean
  onSaved: () => void
}) {
  const [open, setOpen] = useState(false),
    [patch, setPatch] = useState<StructureOverride | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>()
  const query = useQuery({
    ...versionQuery(tenantId, summary.strategy_version_id),
    enabled: open,
  })
  const saved = summary.structure_override ?? {},
    value = { ...query.data?.config, ...(patch ?? saved) }
  async function save(override: StructureOverride) {
    setBusy(true)
    try {
      await imageApi.updateImageStructure({
        path: imagePath(tenantId, summary.draft_id),
        body: { ...change(summary.revision), structure_override: override },
      })
      setPatch(null)
      onSaved()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const update = (p: StructureOverride) =>
    setPatch({ ...(patch ?? saved), ...p })
  return (
    <div className="flex flex-col gap-3">
      <Button variant="outline" onClick={() => setOpen(!open)}>
        本次广告结构{Object.keys(saved).length ? " · 已覆盖" : " · 继承策略"}
      </Button>
      {open && (
        <>
          <p className="text-sm text-muted-foreground">
            仅影响本批。最终广告数与预算须由搭建预览核验。
          </p>
          {!!(error || query.error) && (
            <RequestError error={error || query.error} />
          )}
          <FieldGroup className="grid gap-4 md:grid-cols-2">
            <RuleChoice
              label="广告组生成"
              value={value.group_generation_mode ?? "FIXED"}
              choices={{ FIXED: "固定组数", BY_MATERIAL: "按创意分组" }}
              disabled={disabled || busy}
              onChange={(v) =>
                update(
                  v === "FIXED"
                    ? {
                        group_generation_mode: "FIXED",
                        group_count: 1,
                        group_material_allocation: "SHARED",
                        max_materials_per_group: null,
                      }
                    : {
                        group_generation_mode: "BY_MATERIAL",
                        group_count: null,
                        group_material_allocation: null,
                        max_materials_per_group: 10,
                      },
                )
              }
            />
            <Field>
              <FieldLabel htmlFor="structure-groups">
                {value.group_generation_mode === "FIXED"
                  ? "组数"
                  : "每组最多创意数"}
              </FieldLabel>
              <Input
                id="structure-groups"
                type="number"
                min={1}
                value={
                  (value.group_generation_mode === "FIXED"
                    ? value.group_count
                    : value.max_materials_per_group) ?? 1
                }
                disabled={disabled || busy}
                onChange={(e) =>
                  update(
                    value.group_generation_mode === "FIXED"
                      ? { group_count: Number(e.target.value) }
                      : { max_materials_per_group: Number(e.target.value) },
                  )
                }
              />
            </Field>
            <RuleChoice
              label="广告生成"
              value={value.ad_generation_mode ?? "BY_MATERIAL"}
              choices={{ FIXED: "固定广告数", BY_MATERIAL: "按创意生成" }}
              disabled={disabled || busy}
              onChange={(v) =>
                update(
                  v === "FIXED"
                    ? {
                        ad_generation_mode: "FIXED",
                        ads_per_group: 1,
                        ad_material_allocation: "SHARED",
                        max_materials_per_ad: null,
                      }
                    : {
                        ad_generation_mode: "BY_MATERIAL",
                        ads_per_group: null,
                        ad_material_allocation: null,
                        max_materials_per_ad: 1,
                      },
                )
              }
            />
            <Field>
              <FieldLabel htmlFor="structure-ads">
                {value.ad_generation_mode === "FIXED"
                  ? "每组广告数"
                  : "每广告最多创意单元 M"}
              </FieldLabel>
              <Input
                id="structure-ads"
                type="number"
                min={1}
                value={
                  (value.ad_generation_mode === "FIXED"
                    ? value.ads_per_group
                    : value.max_materials_per_ad) ?? 1
                }
                disabled={disabled || busy}
                onChange={(e) =>
                  update(
                    value.ad_generation_mode === "FIXED"
                      ? { ads_per_group: Number(e.target.value) }
                      : { max_materials_per_ad: Number(e.target.value) },
                  )
                }
              />
            </Field>
            {value.group_generation_mode === "FIXED" && (
              <RuleChoice
                label="组间创意分配"
                value={value.group_material_allocation ?? "SHARED"}
                choices={{ SHARED: "共享", SEQUENTIAL_AVERAGE: "顺序均分" }}
                disabled={disabled || busy}
                onChange={(v) =>
                  update({
                    group_material_allocation:
                      v as StructureOverride["group_material_allocation"],
                  })
                }
              />
            )}
            {value.ad_generation_mode === "FIXED" && (
              <RuleChoice
                label="广告间创意分配"
                value={value.ad_material_allocation ?? "SHARED"}
                choices={{ SHARED: "共享", SEQUENTIAL_AVERAGE: "顺序均分" }}
                disabled={disabled || busy}
                onChange={(v) =>
                  update({
                    ad_material_allocation:
                      v as StructureOverride["ad_material_allocation"],
                  })
                }
              />
            )}
            <Field>
              <FieldLabel htmlFor="structure-creative-count">
                每基础广告文案版本数 K
              </FieldLabel>
              <Input
                id="structure-creative-count"
                type="number"
                min={1}
                value={value.creative_count ?? 1}
                disabled={disabled || busy}
                onChange={(e) =>
                  update({ creative_count: Number(e.target.value) })
                }
              />
            </Field>
          </FieldGroup>
          <div className="flex gap-2">
            <Button
              disabled={disabled || busy || !patch}
              onClick={() => void save(patch!)}
            >
              保存本次结构
            </Button>
            <Button
              variant="outline"
              disabled={disabled || busy || !Object.keys(saved).length}
              onClick={() => void save({})}
            >
              恢复策略结构
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
