import { Copy } from "lucide-react"
import type { AccountPublic, ConnectionPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export const availabilityLabels: Record<AccountPublic["availability"], string> =
  {
    AVAILABLE: "可用",
    PERMISSION_UNKNOWN: "权限待核实",
    NO_ACCESS: "无权限",
    OWNERSHIP_CONFLICT: "归属冲突",
    METADATA_INCOMPLETE: "资料待完善",
  }
export const connectionLabels: Record<ConnectionPublic["status"], string> = {
  PENDING_AUTH: "等待授权",
  DISCOVERING: "正在发现账户",
  ACTIVE: "可用",
  REAUTH_REQUIRED: "需要重新授权",
  ERROR: "连接错误",
  DISABLED: "已停用",
}
export function displayTime(value?: string | null) {
  if (!value) return "尚无记录"
  const time = new Date(value)
  return Number.isNaN(time.getTime())
    ? "时间待核实"
    : time.toLocaleString("zh-CN", { hour12: false })
}
export function Identifier({ value }: { value: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="font-mono text-xs">{value}</span>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={`复制 ${value}`}
        onClick={() => {
          void navigator.clipboard.writeText(value)
        }}
      >
        <Copy />
      </Button>
    </span>
  )
}
export function FilterSelect({
  label,
  value,
  onChange,
  choices,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  choices: Record<string, string>
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectItem value="all">全部{label}</SelectItem>
          {Object.entries(choices).map(([key, text]) => (
            <SelectItem key={key} value={key}>
              {text}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
