import { X } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

export type ScopeKind = "account" | "campaign" | "adgroup" | "ad"

/** 下钻或点上级路径时记下的筛选对象名称，只用于标签显示，筛选本身仍以 ID 为准。 */
export type ScopeLabels = {
  accountName?: string
  parent?: { kind: ScopeKind; id: string; name: string }
}

const KIND_LABEL: Record<ScopeKind, string> = {
  account: "账户",
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
}

function Chip({ text, onRemove }: { text: string; onRemove: () => void }) {
  return (
    <Badge variant="secondary" className="max-w-80 gap-1 pr-1">
      <span className="truncate" title={text}>
        {text}
      </span>
      <button
        type="button"
        className="rounded-sm p-0.5 hover:bg-muted-foreground/20"
        aria-label={`取消筛选 ${text}`}
        onClick={onRemove}
      >
        <X className="size-3" aria-hidden="true" />
      </button>
    </Badge>
  )
}

/**
 * 当前层级筛选的可见标签：下钻后能看到"正在看哪个账户/系列下的数据"，
 * 并能逐级取消回到上层或全部。取消账户会连同下级对象一起取消。
 */
export function AdsScopeChips({
  advertiserIds,
  ids,
  labels,
  onClearAccount,
  onClearParent,
}: {
  advertiserIds?: string[] | null
  ids?: string[] | null
  labels: ScopeLabels
  onClearAccount: () => void
  onClearParent: () => void
}) {
  const accountId = advertiserIds?.[0]
  const parentId = ids?.[0]
  if (!accountId && !parentId) return null
  const parent =
    labels.parent && labels.parent.id === parentId ? labels.parent : undefined
  // 按账户下钻时 ids 就是账户自身，只显示一个账户标签。
  const parentIsAccount = parent?.kind === "account" || parentId === accountId
  const accountText = `账户：${
    (parentIsAccount ? parent?.name : labels.accountName) ?? accountId
  }`
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-muted-foreground">当前筛选</span>
      {accountId && <Chip text={accountText} onRemove={onClearAccount} />}
      {parentId && !parentIsAccount && (
        <Chip
          text={`${parent ? KIND_LABEL[parent.kind] : "对象"}：${parent?.name ?? parentId}`}
          onRemove={onClearParent}
        />
      )}
      <Button
        type="button"
        variant="link"
        size="sm"
        className="h-auto p-0 text-xs"
        onClick={onClearAccount}
      >
        清除全部
      </Button>
    </div>
  )
}
