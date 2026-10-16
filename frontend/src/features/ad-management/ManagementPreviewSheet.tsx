import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { Info, TriangleAlert } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import {
  AdManagementService,
  type EntityRef,
  type FrozenSelection,
  type ManagementItemPublic,
  type ManagementPreviewPublic,
  type ManagementTaskPublic,
  type MutationSpec,
  type RenameRowProblem,
  type RenameRule,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import type { AdsDimension } from "@/features/ads/search"
import { renameReasonLabel } from "./presentation"
import { RenameFields } from "./RenameFields"
import {
  emptyRenameDraft,
  parsePastedRows,
  type RenameDraft,
  renameRuleFrom,
} from "./renameRules"

// 工具栏选定的批量动作：启停直接带状态值，数值类动作在面板里再填调整方式与数值。
// 改名有两个入口：勾选推广系列后批量改名（selection），或不勾选直接按表格粘贴（paste）。
export type ManagementAction =
  | { field: "status"; value: "ENABLE" | "DISABLE" }
  | { field: "roas" | "budget" }
  | { field: "name"; entry: "selection" | "paste" }

type Props = {
  tenantId: string
  bcId: string
  selection: FrozenSelection | null
  action: ManagementAction | null
  // 发起动作时所在的报表维度，用来说明“选中的是什么、实际改的是哪一层”。
  dimension: AdsDimension
  open: boolean
  onOpenChange: (open: boolean) => void
  onSubmitted: (task: ManagementTaskPublic) => void
}

type MutationMode = MutationSpec["mode"]

const MODE_OPTIONS: Array<{ value: MutationMode; label: string }> = [
  { value: "set", label: "设为" },
  { value: "add", label: "增加" },
  { value: "subtract", label: "减少" },
  { value: "increase_percent", label: "增加百分比" },
  { value: "decrease_percent", label: "减少百分比" },
]

const KIND_LABELS: Record<EntityRef["kind"], string> = {
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  creative: "素材",
}

const DIMENSION_LABELS: Record<AdsDimension, string> = {
  account: "账户",
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  material: "素材",
  drama: "剧",
}

// 数值输入停顿后才请求预览，避免每敲一个字符都让服务器计算一次。
const VALUE_DEBOUNCE_MS = 400

/** 动作的稳定键：调用方用它（连同冻结选择 ID）作为面板 key，换动作即重新挂载。 */
export function managementActionKey(action: ManagementAction | null) {
  if (!action) return ""
  if (action.field === "status") return `status:${action.value}`
  if (action.field === "name") return `name:${action.entry}`
  return action.field
}

export function actionTitle(action: ManagementAction | null) {
  if (!action) return "管理预览"
  if (action.field === "status")
    return action.value === "ENABLE" ? "批量启用" : "批量暂停"
  if (action.field === "name") return "批量改名"
  return action.field === "roas" ? "修改目标 ROAS" : "修改预算"
}

/**
 * 按动作与维度说明修改落在哪一层。后端规则见 ad_management/expansion.py：
 * 启停只改所选层级本身；目标 ROAS 只存在于广告组；预算改在实际持有预算的层级。
 */
export function actionScope(
  action: ManagementAction | null,
  dimension: AdsDimension,
): string[] {
  // 改名的说明放在各规则页签里，这里不再重复。
  if (!action || action.field === "name") return []
  if (action.field === "status") {
    const verb = action.value === "ENABLE" ? "启用" : "暂停"
    if (dimension === "account") return [`将${verb}所选账户下的全部广告系列。`]
    if (dimension === "drama") return [`将${verb}所选剧对应的全部广告系列。`]
    if (dimension === "material")
      return [
        `只${verb}该素材在对应广告里的使用，广告本身和其它素材不受影响。`,
        "仅 Smart+ 广告支持单独开关素材。",
      ]
    const lines = [
      `只${verb}所选的${DIMENSION_LABELS[dimension]}本身，不会改动它的上级或下级。`,
    ]
    lines.push(
      action.value === "ENABLE"
        ? "如果上级处于暂停状态，下级启用后仍不会投放；预览会列出这些上级，可勾选一起启用。"
        : "暂停系列或广告组后，其下的全部内容都会停止投放。",
    )
    return lines
  }
  if (action.field === "roas") {
    const target: Partial<Record<AdsDimension, string>> = {
      campaign: "将修改所选系列下的全部广告组。",
      adgroup:
        "将修改所选广告组；Smart+ 系列会连同同系列的其它广告组一起修改。",
      ad: "将修改广告所属的广告组；Smart+ 系列会连同同系列的其它广告组一起修改。",
    }
    return [
      "目标 ROAS 只设置在广告组上，系列和广告没有单独的目标 ROAS。",
      target[dimension] ?? "",
      "TikTok 要求同一系列下所有广告组的目标 ROAS 相同。",
    ].filter(Boolean)
  }
  const target: Partial<Record<AdsDimension, string>> = {
    account: "将修改所选账户下全部系列的预算。",
    drama: "将修改所选剧对应的全部系列的预算。",
    campaign: "所选系列如果使用组预算，会改为修改它下面的每个广告组。",
    adgroup:
      "所选广告组如果所属系列使用系列预算，会改为修改该系列的预算（同系列只改一次）。",
  }
  return [
    "预算改在实际持有预算的那一层：系列开启系列预算（CBO）时改系列日预算，使用组预算时改各广告组日预算。",
    target[dimension] ?? "",
    "按加减或百分比调整时，每个对象按自己的原值分别计算。",
  ].filter(Boolean)
}

// 只接受大于 0 的普通十进制数；"0x10"、"1e3" 之类后端 Decimal 不认的写法一律拒绝。
const PLAIN_DECIMAL = /^\d+(\.\d+)?$/
function validNumber(value: string) {
  const trimmed = value.trim()
  return PLAIN_DECIMAL.test(trimmed) && Number(trimmed) > 0
}

function remaining(expiresAt: string | undefined, now: number) {
  if (!expiresAt) return ""
  const seconds = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000))
  if (seconds <= 0) return "预览已过期，请关闭后重新发起"
  return `预览五分钟内有效 · 剩余 ${Math.floor(seconds / 60)}分${seconds % 60}秒`
}

// 服务器只返回应用自有的中文提示（code/message），这里直接展示给用户。
function previewErrorMessage(error: unknown) {
  if (error instanceof AxiosError) {
    const message = (error.response?.data as { message?: unknown } | undefined)
      ?.message
    if (typeof message === "string" && message) return message
  }
  return "预览生成失败，请稍后重试。"
}

type RowGroup = "change" | "info" | "blocked"

// 被用户主动跳过的项与其它“无法修改”分开：它们不再阻止提交。
const SKIPPED_REASONS = new Set(["excluded_ref", "excluded_material_use"])

function rowGroup(item: ManagementItemPublic): RowGroup {
  if (item.execution_result === "PENDING") return "change"
  if (
    item.execution_result === "UNSUPPORTED" ||
    item.execution_result === "CONFLICT"
  )
    return "blocked"
  return "info"
}

function isBlocking(item: ManagementItemPublic) {
  return rowGroup(item) === "blocked" && !SKIPPED_REASONS.has(item.reason ?? "")
}

const STATUS_TEXT: Record<string, string> = {
  ENABLE: "启用",
  DISABLE: "暂停",
}

function displayValue(value: string | null | undefined) {
  if (value === null || value === undefined) return null
  return STATUS_TEXT[value] ?? value
}

/** 把后端原因代码翻译成用户能看懂的说明。 */
export function reasonText(
  item: ManagementItemPublic,
  action: ManagementAction | null,
): string {
  const reason = item.reason
  if (action?.field === "name" && reason && renameReasonLabel[reason])
    return renameReasonLabel[reason]
  if (item.execution_result === "PENDING") {
    if (action?.field === "status")
      return action.value === "ENABLE" ? "将启用" : "将暂停"
    return "将修改"
  }
  switch (reason) {
    case "linked_impact":
      if (action?.field === "roas")
        return "你选中的对象；目标 ROAS 实际改在它所属的广告组上（见上方）"
      if (action?.field === "budget")
        return "你选中的对象；预算实际改在持有预算的那一层（见上方）"
      return "你选中的对象；由上方对象代为修改"
    case "parent_disabled":
      return "上级处于暂停状态；未勾选“同时启用”时不会改动"
    case "no_change":
      return "当前已是目标值，无需修改"
    case "already_disabled":
      return "已是暂停状态，无需修改"
    case "configuration_missing":
      return action?.field === "roas"
        ? "平台未返回目标 ROAS（该广告组可能没有使用目标 ROAS 出价）"
        : "平台未返回当前值，无法在此基础上修改"
    case "configuration_invalid":
      return action?.field === "budget"
        ? "当前预算为 0 或无效，无法在此基础上修改"
        : "当前值为 0 或无效，无法在此基础上修改"
    case "final_value_invalid":
      return "调整后的数值不大于 0，请减小减少的幅度"
    case "budget_owner_missing":
      return "找不到持有预算的系列或广告组，请先刷新报表"
    case "adgroup_missing":
      return "找不到广告所属的广告组，请先刷新报表"
    case "management_capability_unverified":
      return "该账户的这项操作权限尚未验证，请到账户管理检查授权"
    case "grouping_revision_missing":
      return "所属系列信息尚未同步，请先刷新报表"
    case "material_status_unsupported":
      return "只有 Smart+ 广告支持单独开关素材"
    case "material_reference_missing":
      return "素材使用记录尚未同步，请先刷新报表"
    case "ad_not_found":
    case "directory_missing":
    case "parent_not_found":
      return "目录中找不到该对象，可能已删除，请先刷新报表"
    case "excluded_ref":
    case "excluded_material_use":
      return "已跳过，不会提交"
    case "configuration_conflict":
      return "当前值已发生变化，请重新发起"
    default:
      return reason ?? "无需修改"
  }
}

function objectLabel(item: ManagementItemPublic) {
  if (item.material_use)
    return {
      kind: "素材",
      name: item.material_use.platform_material_id,
      detail: `所在广告 ${item.name ?? item.ref.remote_id}`,
    }
  return {
    kind: KIND_LABELS[item.ref.kind] ?? item.ref.kind,
    name: item.name ?? item.ref.remote_id,
    detail: item.name ? `ID ${item.ref.remote_id}` : null,
  }
}

function refKey(ref: EntityRef) {
  return `${ref.advertiser_id}:${ref.kind}:${ref.remote_id}`
}

/** 按层级统计将要修改的对象，例如“广告组 3 个、系列 1 个”。 */
function kindSummary(items: ManagementItemPublic[]) {
  const counts = new Map<string, number>()
  for (const item of items) {
    const label = objectLabel(item).kind
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  return [...counts.entries()]
    .map(([label, count]) => `${label} ${count} 个`)
    .join("、")
}

const GROUP_TITLES: Record<RowGroup, string> = {
  change: "将修改",
  info: "不修改（仅供参考）",
  blocked: "无法修改",
}

/** 改名后的剧名：剧名或版权方变化时醒目提示报表归类会随之改变。 */
function DramaCell({ item }: { item: ManagementItemPublic }) {
  if (item.drama_changed)
    return (
      <Badge
        variant="outline"
        className="whitespace-normal border-amber-300 bg-amber-50 text-amber-800"
      >
        {item.drama_name
          ? `报表中将归到 ${item.drama_name}`
          : "改名后无法识别剧名，将归入未识别"}
      </Badge>
    )
  return <span className="text-muted-foreground">{item.drama_name ?? "—"}</span>
}

function ObjectCell({
  kind,
  name,
  detail,
}: {
  kind: string
  name: string
  detail?: string | null
}) {
  return (
    <div className="flex items-start gap-2">
      <Badge variant="outline">{kind}</Badge>
      <div className="min-w-0">
        <div className="max-w-56 truncate" title={name}>
          {name}
        </div>
        {detail && (
          <div className="max-w-56 truncate text-xs text-muted-foreground">
            {detail}
          </div>
        )}
      </div>
    </div>
  )
}

function PreviewRows({
  items,
  action,
  problems,
}: {
  items: ManagementItemPublic[]
  action: ManagementAction | null
  // 表格粘贴时解析接口找不到或格式不对的行；它们不在冻结选择里，单独列在最上面。
  problems: RenameRowProblem[]
}) {
  const groups: RowGroup[] = ["change", "blocked", "info"]
  // 改名逐行展示原名称、新名称和改名后解析出的剧名，对象列只放推广系列 ID 与账户。
  const rename = action?.field === "name"
  const columns = rename ? 5 : 3
  return (
    <div className="max-h-96 overflow-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-background">
          <tr className="border-b text-left text-muted-foreground">
            <th className="p-2 font-medium">{rename ? "推广系列" : "对象"}</th>
            {rename ? (
              <>
                <th className="p-2 font-medium">原名称</th>
                <th className="p-2 font-medium">新名称</th>
                <th className="p-2 font-medium">剧名</th>
              </>
            ) : (
              <th className="whitespace-nowrap p-2 font-medium">原值 → 新值</th>
            )}
            <th className="p-2 font-medium">说明</th>
          </tr>
        </thead>
        {problems.length > 0 && (
          <tbody>
            <tr className="border-b bg-muted/40">
              <td colSpan={columns} className="px-2 py-1 text-xs font-medium">
                表格中无法识别（{problems.length}）
              </td>
            </tr>
            {problems.map((problem, index) => (
              <tr
                key={`${index}:${problem.campaign_id}:${problem.reason}`}
                className="border-b align-top"
              >
                <td className="p-2">
                  <ObjectCell kind="系列" name={problem.campaign_id} />
                </td>
                <td className="p-2">—</td>
                <td className="break-all p-2">{problem.new_name || "—"}</td>
                <td className="p-2">—</td>
                <td className="p-2 text-amber-700">
                  {renameReasonLabel[problem.reason] ?? problem.reason}
                </td>
              </tr>
            ))}
          </tbody>
        )}
        {groups.map((group) => {
          const rows = items.filter((item) => rowGroup(item) === group)
          if (rows.length === 0) return null
          return (
            <tbody key={group}>
              <tr className="border-b bg-muted/40">
                <td colSpan={columns} className="px-2 py-1 text-xs font-medium">
                  {GROUP_TITLES[group]}（{rows.length}）
                </td>
              </tr>
              {rows.map((item) => {
                const label = objectLabel(item)
                const before = displayValue(item.original_value)
                const after = displayValue(item.final_value)
                return (
                  <tr
                    key={`${refKey(item.ref)}:${item.material_use?.platform_material_id ?? ""}:${item.reason ?? ""}`}
                    className="border-b align-top"
                  >
                    <td className="p-2">
                      {rename ? (
                        <ObjectCell
                          kind={label.kind}
                          name={item.ref.remote_id}
                          detail={`账户 ${item.ref.advertiser_id}`}
                        />
                      ) : (
                        <ObjectCell {...label} />
                      )}
                    </td>
                    {rename ? (
                      <>
                        <td className="break-all p-2">{before ?? "—"}</td>
                        <td className="break-all p-2">{after ?? "—"}</td>
                        <td className="p-2">
                          <DramaCell item={item} />
                        </td>
                      </>
                    ) : (
                      <td className="whitespace-nowrap p-2">
                        {group === "change" || after
                          ? `${before ?? "—"} → ${after ?? "—"}`
                          : "—"}
                      </td>
                    )}
                    <td
                      className={
                        group === "blocked"
                          ? "p-2 text-amber-700"
                          : "p-2 text-muted-foreground"
                      }
                    >
                      {reasonText(item, action)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          )
        })}
      </table>
    </div>
  )
}

export function ManagementPreviewSheet({
  tenantId,
  bcId,
  selection,
  action,
  dimension,
  open,
  onOpenChange,
  onSubmitted,
}: Props) {
  const [mode, setMode] = useState<MutationMode>("set")
  // input 是输入框里的原文，value 是防抖后用于生成预览的数值。
  const [input, setInput] = useState("")
  const [value, setValue] = useState("")
  const [excludedRefs, setExcludedRefs] = useState<EntityRef[]>([])
  // 暂停中的上级：首次预览（不含上级）列出的 parent_disabled 项。
  // 勾选后它们会变成待修改项，所以候选列表要单独保存，不能每次从预览里重新取。
  const [blockedParents, setBlockedParents] = useState<ManagementItemPublic[]>(
    [],
  )
  const [includeParents, setIncludeParents] = useState(false)
  const renaming = action?.field === "name"
  // 改名草稿：勾选入口默认查找替换，表格入口默认表格粘贴。
  const [renameDraft, setRenameDraft] = useState<RenameDraft>(() =>
    emptyRenameDraft(
      action?.field === "name" && action.entry === "paste"
        ? "explicit"
        : "replace",
    ),
  )
  // 点击“生成预览”时冻结的改名规则；之后修改草稿不会悄悄改动已生成的预览。
  const [renameRule, setRenameRule] = useState<RenameRule | null>(null)
  // 表格粘贴按 ID 解析出的冻结选择与无法识别的行；其它规则作用于列表勾选的选择。
  const [pasted, setPasted] = useState<{
    selection: FrozenSelection | null
    problems: RenameRowProblem[]
  } | null>(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!open) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [open])
  // 面板状态不在这里手动清空：调用方按"冻结选择 + 动作"给面板换 key，
  // 每次重新打开都是全新挂载，旧的方式、数值、排除项和上级勾选不可能被提交。
  useEffect(() => {
    if (input === value) return
    const timer = window.setTimeout(() => setValue(input), VALUE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [input, value])
  const numeric = action?.field === "roas" || action?.field === "budget"
  const enabling = action?.field === "status" && action.value === "ENABLE"
  const mutation = useMemo<MutationSpec | null>(() => {
    if (!action) return null
    // 只有启用时才会把暂停中的上级一起带上；暂停、改预算和 ROAS 不涉及上级。
    const includes =
      enabling && includeParents ? blockedParents.map((item) => item.ref) : []
    if (action.field === "status")
      return {
        field: "status",
        mode: "set",
        value: action.value,
        include_parents: includes,
        excluded_refs: excludedRefs,
      }
    // 改名没有数值，名称由规则在预览时逐条算出并冻结。
    if (action.field === "name")
      return renameRule
        ? {
            field: "name",
            mode: "set",
            rename: renameRule,
            include_parents: [],
            excluded_refs: excludedRefs,
          }
        : null
    // 数值为空或非法时不生成预览，避免用默认值误改真实广告。
    if (!validNumber(value)) return null
    return {
      field: action.field,
      mode,
      value: value.trim(),
      include_parents: includes,
      excluded_refs: excludedRefs,
    }
  }, [
    action,
    enabling,
    mode,
    value,
    includeParents,
    blockedParents,
    excludedRefs,
    renameRule,
  ])
  // 表格粘贴的预览作用于解析出的选择，其余动作作用于列表冻结的选择。
  const target =
    renameRule?.kind === "explicit" ? (pasted?.selection ?? null) : selection
  const resolveRows = useMutation({
    mutationFn: async (rows: Array<{ campaignId: string; newName: string }>) =>
      (
        await AdManagementService.resolveRenameSelection({
          path: { tenant_id: tenantId },
          body: {
            bc_id: bcId,
            rows: rows.map((row) => ({
              campaign_id: row.campaignId,
              new_name: row.newName,
            })),
          },
        })
      ).data,
  })
  // 表格最多 500 行，按草稿缓存解析结果，避免每秒倒计时重渲染时重复解析。
  const draftRule = useMemo(
    () => (renaming ? renameRuleFrom(renameDraft) : null),
    [renaming, renameDraft],
  )
  // 草稿与已生成预览的规则不一致时必须重新生成预览才能提交。
  const ruleStale =
    renaming && JSON.stringify(draftRule) !== JSON.stringify(renameRule)
  // 查找替换和前缀后缀只能作用于列表里勾选的推广系列。
  const needsSelection =
    renaming && renameDraft.kind !== "explicit" && !selection
  const generateRenamePreview = () => {
    if (!draftRule) return
    // 新规则可能产生不同的无法修改项，之前的“跳过”勾选不能沿用。
    setExcludedRefs([])
    if (draftRule.kind !== "explicit") {
      setRenameRule(draftRule)
      return
    }
    resolveRows.mutate(parsePastedRows(renameDraft.text).rows, {
      onSuccess: (result) => {
        setPasted(result)
        setRenameRule(draftRule)
      },
    })
  }
  const preview = useQuery<ManagementPreviewPublic>({
    queryKey: [
      "tenant",
      tenantId,
      "ad-management-preview",
      target?.selection_id,
      bcId,
      mutation,
    ],
    enabled: open && !!target && !!mutation,
    queryFn: async ({ signal }) => {
      if (!target || !mutation) throw new Error("请先冻结广告选择")
      return (
        await AdManagementService.preparePreview({
          path: { tenant_id: tenantId },
          body: {
            selection_id: target.selection_id,
            bc_id: bcId,
            mutation,
          },
          signal,
        })
      ).data
    },
    // 调整数值或勾选项时保留上一份预览，避免整块内容闪烁；刷新期间禁止提交。
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: false,
  })
  // 表格粘贴的行全部无法识别时没有可预览的选择，不能展示上一份预览的残留数据。
  const previewData = target ? preview.data : undefined
  const items = useMemo(() => previewData?.items ?? [], [previewData])
  const fresh = !!previewData && !preview.isPlaceholderData
  useEffect(() => {
    if (!fresh || includeParents) return
    setBlockedParents(items.filter((item) => item.reason === "parent_disabled"))
  }, [fresh, includeParents, items])
  const blocking = items.filter(isBlocking)
  const skipping = excludedRefs.length > 0
  // 已选择“跳过”后，若调整数值又产生新的无法修改项，同样跳过，保持勾选语义一致。
  useEffect(() => {
    if (!fresh || !skipping || blocking.length === 0) return
    setExcludedRefs((current) => {
      const known = new Set(current.map(refKey))
      const extra = blocking
        .map((item) => item.ref)
        .filter((ref) => !known.has(refKey(ref)))
      return extra.length ? [...current, ...extra] : current
    })
  }, [fresh, skipping, blocking])
  const submit = useMutation({
    mutationFn: async () => {
      const data = previewData
      if (!data) throw new Error("预览尚未完成")
      return (
        await AdManagementService.submitTask({
          path: { tenant_id: tenantId },
          body: {
            preview_id: data.preview_id,
            preview_digest: data.digest,
            idempotency_key: crypto.randomUUID(),
          },
        })
      ).data
    },
    onSuccess: (task) => {
      onOpenChange(false)
      // 提交是异步受理，回到当前报表继续操作；由工作区提供主动查看记录的入口。
      onSubmitted(task)
    },
  })
  const changes = items.filter((item) => rowGroup(item) === "change")
  const expiresAt = previewData?.expires_at
  const expired = expiresAt ? Date.parse(expiresAt) <= now : false
  // 输入还在防抖期内时，屏幕上的预览对应的是旧数值，不能提交。
  const settling = numeric && input !== value
  const needsValue = numeric && !validNumber(input)
  const percent = mode === "increase_percent" || mode === "decrease_percent"
  const selectedCount = previewData?.counts?.selected ?? 0
  const scope = actionScope(action, dimension)
  const problems =
    renameRule?.kind === "explicit" ? (pasted?.problems ?? []) : []
  // 改名汇总：跳过 = 名称不变等不修改项；异常 = 无法修改项（含已勾选跳过的）加表格无法识别的行。
  const skippedCount = items.filter((item) => rowGroup(item) === "info").length
  const anomalyCount =
    items.filter((item) => rowGroup(item) === "blocked").length +
    problems.length
  const canSubmit =
    !!mutation &&
    !ruleStale &&
    fresh &&
    !preview.isFetching &&
    !settling &&
    !submit.isPending &&
    !expired &&
    blocking.length === 0 &&
    changes.length > 0
  const submitHint = !previewData
    ? null
    : ruleStale
      ? "改名规则已修改，请重新生成预览。"
      : expired
        ? "预览已过期，请关闭后重新发起。"
        : blocking.length > 0
          ? `还有 ${blocking.length} 项无法修改，勾选“跳过”后才能提交。`
          : fresh && changes.length === 0
            ? "没有需要修改的对象。"
            : null
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className={`w-full overflow-y-auto ${renaming ? "sm:max-w-3xl" : "sm:max-w-xl"}`}
      >
        <SheetHeader>
          <SheetTitle>{actionTitle(action)}</SheetTitle>
          <p className="text-sm text-muted-foreground">
            BC：{bcId} · 先预览影响范围，确认后提交，提交前不会改动任何广告。
          </p>
        </SheetHeader>
        <div className="flex flex-col gap-4 px-4 pb-4">
          {scope.length > 0 && (
            <Alert>
              <Info />
              <AlertTitle>这次会改哪里</AlertTitle>
              <AlertDescription>
                <ul className="list-disc space-y-1 pl-4">
                  {scope.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
          {numeric && (
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div className="flex flex-col gap-1">
                <span>调整方式</span>
                <Select
                  value={mode}
                  onValueChange={(next) => setMode(next as MutationMode)}
                >
                  <SelectTrigger aria-label="调整方式" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {MODE_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="management-value">
                  {action?.field === "roas" ? "目标 ROAS" : "日预算"}
                </label>
                <div className="flex items-center gap-2">
                  <Input
                    id="management-value"
                    aria-label="管理新值"
                    inputMode="decimal"
                    placeholder={
                      percent
                        ? "例如 10"
                        : action?.field === "roas"
                          ? "例如 1.2"
                          : "例如 100"
                    }
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                  />
                  {percent && <span className="text-muted-foreground">%</span>}
                </div>
                {needsValue && (
                  <p className="text-xs text-muted-foreground">
                    请输入大于 0 的数值
                  </p>
                )}
              </div>
            </div>
          )}
          {renaming && (
            <div className="flex flex-col gap-3">
              <RenameFields value={renameDraft} onChange={setRenameDraft} />
              {needsSelection && (
                <p className="text-xs text-muted-foreground">
                  查找替换和前缀后缀需要先在列表中勾选推广系列；不勾选时请使用表格粘贴。
                </p>
              )}
              <div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={
                    !draftRule || needsSelection || resolveRows.isPending
                  }
                  onClick={generateRenamePreview}
                >
                  {resolveRows.isPending ? "正在识别表格…" : "生成预览"}
                </Button>
              </div>
              {resolveRows.error && (
                <Alert variant="destructive">
                  <TriangleAlert />
                  <AlertDescription>
                    {previewErrorMessage(resolveRows.error)}
                  </AlertDescription>
                </Alert>
              )}
            </div>
          )}
          {preview.isLoading && (
            <p role="status" aria-label="正在生成管理预览">
              正在生成管理预览…
            </p>
          )}
          {preview.error && (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertDescription>
                {previewErrorMessage(preview.error)}
              </AlertDescription>
            </Alert>
          )}
          {enabling && blockedParents.length > 0 && (
            <Alert className="border-amber-300 bg-amber-50">
              <TriangleAlert />
              <AlertTitle>
                有 {blockedParents.length} 个上级处于暂停状态
              </AlertTitle>
              <AlertDescription>
                <p>只启用下级不会开始投放：</p>
                <ul className="list-disc pl-4">
                  {blockedParents.map((item) => {
                    const label = objectLabel(item)
                    return (
                      <li key={refKey(item.ref)}>
                        {label.kind} · {label.name}
                      </li>
                    )
                  })}
                </ul>
                <div className="mt-2 flex items-center gap-2">
                  <Checkbox
                    id="include-parents"
                    checked={includeParents}
                    onCheckedChange={(checked) =>
                      setIncludeParents(checked === true)
                    }
                    aria-label="同时启用暂停中的上级"
                  />
                  <label htmlFor="include-parents" className="text-foreground">
                    同时启用这 {blockedParents.length} 个上级
                  </label>
                </div>
              </AlertDescription>
            </Alert>
          )}
          {(previewData || problems.length > 0) && (
            <div
              className={
                preview.isFetching
                  ? "flex flex-col gap-3 opacity-60"
                  : "flex flex-col gap-3"
              }
            >
              <div className="rounded-md border p-3 text-sm">
                {renaming ? (
                  <p className="font-medium">
                    {`共 ${items.length + problems.length} 条：修改 ${changes.length}、跳过 ${skippedCount}、异常 ${anomalyCount}`}
                  </p>
                ) : (
                  <p className="font-medium">
                    已选 {selectedCount} 个{DIMENSION_LABELS[dimension]} →
                    将修改 {changes.length} 个对象
                    {changes.length > 0 && `（${kindSummary(changes)}）`}
                    {blocking.length > 0 && `，${blocking.length} 项无法修改`}
                  </p>
                )}
                <p className="mt-1 text-xs text-muted-foreground">
                  账户：
                  {[
                    ...new Set(
                      (target?.refs ?? []).map((ref) => ref.advertiser_id),
                    ),
                  ].join("、") || "—"}{" "}
                  · {remaining(expiresAt, now)}
                </p>
              </div>
              {(blocking.length > 0 || skipping) && (
                <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
                  <Checkbox
                    id="exclude-unsupported"
                    checked={skipping}
                    onCheckedChange={(checked) =>
                      setExcludedRefs(
                        checked === true
                          ? blocking.map((item) => item.ref)
                          : [],
                      )
                    }
                    aria-label="跳过无法修改的对象"
                  />
                  <label htmlFor="exclude-unsupported">
                    {skipping
                      ? "已跳过无法修改的对象，只提交其余对象"
                      : `跳过无法修改的 ${blocking.length} 项，只提交其余对象（原因见下表）`}
                  </label>
                </div>
              )}
              <PreviewRows items={items} action={action} problems={problems} />
            </div>
          )}
        </div>
        <SheetFooter>
          {renaming && (
            <p className="text-sm text-amber-700">
              改名会同步修改 TikTok
              后台的推广系列名称，并影响本工具按剧名的报表归类。
            </p>
          )}
          {submitHint && (
            <p className="text-sm text-muted-foreground">{submitHint}</p>
          )}
          <Button disabled={!canSubmit} onClick={() => submit.mutate()}>
            {submit.isPending
              ? "提交中…"
              : renaming && anomalyCount > 0
                ? "仅提交可修改的行"
                : changes.length > 0
                  ? `提交，修改 ${changes.length} 个对象`
                  : "提交管理任务"}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
