import { AxiosError } from "axios"
import { Copy } from "lucide-react"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
export const namingHint =
  "请在素材文件名中包含完整剧目名称。广告搭建时，系统会根据剧名自动匹配素材。"
export const stages: Record<string, string> = {
  receiving: "等待原文件接收",
  stored: "原文件已接收，等待平台上传",
  uploading: "正在上传 TikTok",
  verifying: "正在核实素材",
  available: "账户素材可用",
  blocked: "需要处理",
  result_unknown: "上传结果待核实",
  pending: "等待上传",
  failed: "上传失败",
  unavailable: "账户素材不可用",
}
export function Stage({ status }: { status: string }) {
  return (
    <Badge variant={status === "available" ? "secondary" : "outline"}>
      {stages[status] || "状态待核实"}
    </Badge>
  )
}
export function bytes(value: number) {
  return value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KB`
      : value < 1024 ** 3
        ? `${(value / 1024 ** 2).toFixed(1)} MB`
        : `${(value / 1024 ** 3).toFixed(2)} GB`
}
export function CopyValue({ value, label }: { value: string; label?: string }) {
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className="min-w-0 break-all font-mono text-xs">{value}</span>
      <Button
        size="icon"
        variant="ghost"
        aria-label={`复制${label || ""} ${value}`}
        onClick={() =>
          void navigator.clipboard
            .writeText(value)
            .then(() => toast.success("已复制"))
            .catch(() => toast.error("复制失败，请选择完整文本复制"))
        }
      >
        <Copy />
      </Button>
    </span>
  )
}
const errors: Record<string, string> = {
  no_upload_account:
    "当前 BC 暂无可上传账户，原文件保留，请联系管理员处理授权。",
  no_available_upload_account:
    "当前 BC 暂无可上传账户，原文件保留，请联系管理员处理授权。",
  object_storage_unavailable: "原文件存储暂不可用，请稍后查看进度。",
  object_storage_unconfigured: "原文件存储尚未配置，请联系管理员。",
  no_upload_capable_account:
    "当前 BC 暂无可上传账户，原文件保留，请联系管理员处理授权。",
  storage_unavailable: "原文件存储暂不可用，请稍后查看进度。",
  upload_not_retryable: "该步骤当前不能重试，请刷新核实进度。",
  object_result_unknown: "原文件接收结果待核实，请刷新进度。",
  invalid_file: "文件不符合要求，请查看文件信息。",
  permission_denied: "上传权限已失效，请联系管理员。",
  blocked_auth: "平台账户授权失效，原文件保留。",
}
export function issue(error: unknown): string {
  if (error instanceof Error && error.message === "resume_storage_unavailable")
    return "浏览器无法保存上传恢复信息，请释放当前站点存储空间后重试。尚未创建批次。"
  if (typeof error === "string")
    return errors[error] || "该步骤需要处理，请刷新进度或联系管理员。"
  if (error instanceof AxiosError) {
    if (error.response?.status === 403)
      return "当前角色没有上传权限，请联系管理员。"
    const code = error.response?.data?.code
    return errors[code] || "请求未完成，请检查网络并刷新状态。"
  }
  return "传输未完成，请检查网络后继续。"
}
export const isUnknown = (e: unknown) =>
  !(e instanceof AxiosError) ||
  !e.response ||
  e.response.status >= 500 ||
  e.response.status === 408
export const materialKey = (tenantId: string, bcId: string) =>
  ["tenant", tenantId, "materials", bcId] as const
